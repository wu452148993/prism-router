"""`prompt_file` ingestion — wrapper-side evidence injection, pre-spawn.

The wrapper runs OUTSIDE the model sandbox, so it can read evidence files
the backends cannot (gemini-cli blocks `.git/`-internal paths) and append
their content to the stdin payload; models never need filesystem access.

Fail-closed contract: every failure returns a `prompt_file_error` envelope
at stage `pre_flight` and the backend is never spawned — a failed call
consumes no review round and produces no SESSION_ID.

Anti-exfiltration boundary: the realpath-resolved file must lie under the
resolved `cd` or under `cd`'s git dir (`git rev-parse --absolute-git-dir`,
covering `.git/acf/**` in linked-worktree layouts). The check is
separator-aware (`Path.is_relative_to`), never string prefix.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import ErrorEnvelope, PromptFileReceipt, make_error

_LOG = logging.getLogger("prism_router")

_DEFAULT_MAX_BYTES = 1 << 20  # 1 MiB
_GIT_DIR_TIMEOUT_S = 5.0
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class LoadedPromptFile:
    """Exactly one side set: (`content`, `receipt`) on success, `error` on failure."""

    content: str | None = None
    receipt: PromptFileReceipt | None = None
    error: ErrorEnvelope | None = None


def _err(reason: str) -> LoadedPromptFile:
    return LoadedPromptFile(
        error=make_error("prompt_file_error", "pre_flight", retryable=False, raw=reason),
    )


def _max_bytes() -> int:
    raw = os.environ.get("PRISM_PROMPT_FILE_MAX_BYTES")
    if raw is None:
        return _DEFAULT_MAX_BYTES
    try:
        val = int(raw)
    except ValueError:
        _LOG.warning(
            "PRISM_PROMPT_FILE_MAX_BYTES=%r is not an int; using default %d",
            raw, _DEFAULT_MAX_BYTES,
        )
        return _DEFAULT_MAX_BYTES
    if val <= 0:
        _LOG.warning(
            "PRISM_PROMPT_FILE_MAX_BYTES=%r must be > 0; using default %d",
            raw, _DEFAULT_MAX_BYTES,
        )
        return _DEFAULT_MAX_BYTES
    return val


# Inherited git-discovery vars would silently redirect the boundary root:
# GIT_DIR=<anywhere> makes `git -C cwd rev-parse` report <anywhere> instead
# of cwd's own git dir. Discovery must be filesystem-only.
_GIT_ENV_OVERRIDES = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE")


def _git_dir(cwd: Path) -> Path | None:
    """Resolved git dir of `cwd`, or None when not a repo / git unavailable."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_OVERRIDES}
    # stdin=DEVNULL: under MCP stdio the inherited stdin is the JSON-RPC pipe,
    # parked in a synchronous read by the transport thread; on Windows git
    # stalls behind that read (docs/bugs/prompt-file-git-dir-stdin-hang.md).
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--absolute-git-dir"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
            timeout=_GIT_DIR_TIMEOUT_S, creationflags=flags, env=env,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None  # ValueError covers stdout decode errors (fail closed)
    if proc.returncode != 0:
        return None
    # Newline-only: a git-dir path may legally start/end with spaces (POSIX).
    out = proc.stdout.rstrip("\r\n")
    if not out:
        return None
    try:
        return Path(out).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def load_prompt_file(
    path_arg: str, *, cwd: Path, expected_sha256: str = "",
) -> LoadedPromptFile:
    """Validate and read `path_arg` (absolute, or relative to `cwd`).

    Ordered checks, first failure wins: sha256 pin format → boundary →
    exists/readable → regular file → size cap → strict UTF-8 → sha256
    match. Content is returned verbatim — no transcoding, no
    normalization, no truncation.
    """
    if expected_sha256 and not _SHA256_HEX.fullmatch(expected_sha256):
        return _err(
            f"prompt_file_sha256 must be 64 lowercase hex chars: {expected_sha256!r}"
        )

    if "\x00" in path_arg:
        # Windows realpath TRUNCATES at NUL — "ev.md\0junk" would silently
        # load "ev.md". Reject before any Path construction.
        return _err(f"prompt_file path contains NUL: {path_arg!r}")
    target = Path(path_arg)
    if target.drive and not target.is_absolute():
        # "D:foo" resolves against the per-drive process cwd, not `cd`.
        return _err(f"prompt_file drive-relative path is ambiguous: {path_arg}")
    if not target.is_absolute():
        target = cwd / target
    try:
        resolved = target.resolve()  # symlinks fully resolved BEFORE boundary check
        root_cd = cwd.resolve()
    except (OSError, RuntimeError, ValueError) as e:
        return _err(f"prompt_file path unresolvable: {path_arg}: {e}")

    if not resolved.is_relative_to(root_cd):
        git_dir = _git_dir(cwd)
        if git_dir is None or not resolved.is_relative_to(git_dir):
            return _err(
                f"prompt_file outside allowed roots (cd or its git dir): {resolved}"
            )

    try:
        st = os.stat(resolved)
    except FileNotFoundError:
        return _err(f"prompt_file not found: {resolved}")
    except OSError as e:
        return _err(f"prompt_file unreadable: {resolved}: {e}")
    if not stat.S_ISREG(st.st_mode):
        return _err(f"prompt_file is not a regular file: {resolved}")

    cap = _max_bytes()
    if st.st_size > cap:
        return _err(
            f"prompt_file exceeds max {cap} bytes ({st.st_size} bytes): {resolved}"
        )
    try:
        with open(resolved, "rb") as f:
            # st_size+1 probe: allocation stays file-sized (read(n) preallocates
            # n bytes), and one surplus byte proves concurrent growth — the
            # receipt must never attest a torn read.
            raw = f.read(st.st_size + 1)
    except (OSError, MemoryError, OverflowError) as e:
        # OverflowError: st_size near sys.maxsize makes the probe length
        # unrepresentable as Py_ssize_t.
        return _err(f"prompt_file unreadable: {resolved}: {e}")
    if len(raw) > st.st_size:
        return _err(f"prompt_file changed during read: {resolved}")

    try:
        content = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as e:
        return _err(f"prompt_file is not valid UTF-8: {resolved}: {e}")
    except MemoryError as e:
        return _err(f"prompt_file unreadable: {resolved}: {e}")

    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and expected_sha256 != digest:
        return _err(
            f"prompt_file sha256 mismatch: expected {expected_sha256}, "
            f"computed {digest}: {resolved}"
        )

    return LoadedPromptFile(
        content=content,
        receipt=PromptFileReceipt(path=str(resolved), sha256=digest, bytes=len(raw)),
    )
