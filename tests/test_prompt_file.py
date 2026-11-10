"""prompt_file ingestion tests — loader validation + server wiring.

Loader level: boundary/regular/size/UTF-8/sha256 checks in
`prompt_file.load_prompt_file` (pure filesystem, no asyncio).

Server level: payload assembly (`PROMPT + "\\n\\n" + content` under the
AGENT_SIDE prepend), fail-closed pre-spawn gate (Dispatcher never invoked,
no SESSION_ID), receipt emission, and PROMPT-only backward compat.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastmcp import Client

from prism_router import server as server_mod
from prism_router.dispatcher import DispatchResult
from prism_router.parser import ParseResult
from prism_router.prompt_file import LoadedPromptFile, load_prompt_file
from prism_router.protocol import AGENT_SIDE
from prism_router.runner import RunOutcome
from prism_router.server import build_server

_CONTENT = "## Evidence — 证据\r\nline2 with ünïcode\n"
_RAW = _CONTENT.encode("utf-8")
_SHA = hashlib.sha256(_RAW).hexdigest()

_BASE_KEYS = {
    "success", "SESSION_ID", "agent_messages", "truncated",
    "error", "all_messages", "diagnostics",
}

_HAS_GIT = shutil.which("git") is not None
requires_git = pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")


def _evidence(root: Path, name: str = "ev.md", raw: bytes = _RAW) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(raw)
    return p


def _assert_pf_error(res: LoadedPromptFile, reason_marker: str) -> None:
    assert res.content is None
    assert res.receipt is None
    err = res.error
    assert err is not None
    assert err["code"] == "prompt_file_error"
    assert err["stage"] == "pre_flight"
    assert err["retryable"] is False
    assert reason_marker in err["raw"]


# ───── loader: happy paths ─────


def test_load_happy_path_absolute(tmp_path: Path) -> None:
    f = _evidence(tmp_path)
    res = load_prompt_file(str(f), cwd=tmp_path)
    assert res.error is None
    assert res.content == _CONTENT  # verbatim: CRLF + unicode untouched
    assert res.receipt == {"path": str(f.resolve()), "sha256": _SHA, "bytes": len(_RAW)}


def test_load_relative_path_resolves_against_cd(tmp_path: Path) -> None:
    _evidence(tmp_path / "sub")
    res = load_prompt_file("sub/ev.md", cwd=tmp_path)
    assert res.error is None
    assert res.content == _CONTENT


@requires_git
def test_load_file_under_git_dir_of_cd_allowed(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    f = _evidence(tmp_path / ".git" / "acf")
    res = load_prompt_file(str(f), cwd=tmp_path)
    assert res.error is None
    assert res.receipt is not None
    assert res.receipt["sha256"] == _SHA


@requires_git
def test_load_separate_git_dir_allowed_via_root_b(tmp_path: Path) -> None:
    """Git dir physically outside `cd` (linked-worktree analogue): only
    boundary root (b) — `git rev-parse --absolute-git-dir` — admits it."""
    repo, gitdir = tmp_path / "repo", tmp_path / "gitdir"
    subprocess.run(
        ["git", "init", "-q", f"--separate-git-dir={gitdir}", str(repo)],
        check=True, capture_output=True,
    )
    f = _evidence(gitdir / "acf")
    res = load_prompt_file(str(f), cwd=repo)
    assert res.error is None
    assert res.receipt is not None
    assert res.receipt["path"] == str(f.resolve())


@requires_git
def test_load_git_dir_root_immune_to_git_dir_env(tmp_path: Path, monkeypatch) -> None:
    """An inherited GIT_DIR must not redirect boundary root (b): discovery
    is filesystem-only. Empirically `GIT_DIR=<decoy> git -C repo rev-parse
    --absolute-git-dir` reports the decoy — without env stripping the real
    git dir would be rejected and the decoy admitted."""
    repo, gitdir, decoy = tmp_path / "repo", tmp_path / "gitdir", tmp_path / "decoy"
    subprocess.run(
        ["git", "init", "-q", f"--separate-git-dir={gitdir}", str(repo)],
        check=True, capture_output=True,
    )
    monkeypatch.setenv("GIT_DIR", str(decoy))
    f = _evidence(gitdir / "acf")
    assert load_prompt_file(str(f), cwd=repo).error is None
    decoy_file = _evidence(decoy, "leak.md")
    _assert_pf_error(load_prompt_file(str(decoy_file), cwd=repo), "outside allowed roots")


# ───── loader: boundary rejections ─────


@requires_git
def test_load_outside_both_roots_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    f = _evidence(tmp_path)  # sibling of repo: outside cd AND outside git dir
    res = load_prompt_file(str(f), cwd=repo)
    _assert_pf_error(res, "outside allowed roots")


def test_load_outside_rejected_when_cd_not_git_repo(tmp_path: Path) -> None:
    cd = tmp_path / "cd"
    cd.mkdir()
    f = _evidence(tmp_path)
    res = load_prompt_file(str(f), cwd=cd)
    _assert_pf_error(res, "outside allowed roots")


def test_load_traversal_rejected(tmp_path: Path) -> None:
    cd = tmp_path / "cd"
    cd.mkdir()
    _evidence(tmp_path, "secret.md")
    res = load_prompt_file("../secret.md", cwd=cd)
    _assert_pf_error(res, "outside allowed roots")


def test_load_symlink_escape_rejected(tmp_path: Path) -> None:
    """Symlink inside `cd` pointing outside must fail: realpath resolves
    BEFORE the boundary check."""
    cd = tmp_path / "cd"
    cd.mkdir()
    target = _evidence(tmp_path, "outside.md")
    link = cd / "inside.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation requires privilege on this platform")
    res = load_prompt_file(str(link), cwd=cd)
    _assert_pf_error(res, "outside allowed roots")


# ───── loader: file-class / size / encoding / hash rejections ─────


def test_load_directory_rejected(tmp_path: Path) -> None:
    d = tmp_path / "adir"
    d.mkdir()
    _assert_pf_error(load_prompt_file(str(d), cwd=tmp_path), "not a regular file")


def test_load_missing_rejected(tmp_path: Path) -> None:
    _assert_pf_error(load_prompt_file("nope.md", cwd=tmp_path), "not found")


def test_load_oversize_rejected_no_truncation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PRISM_PROMPT_FILE_MAX_BYTES", "16")
    f = _evidence(tmp_path, raw=b"x" * 17)
    _assert_pf_error(load_prompt_file(str(f), cwd=tmp_path), "exceeds max 16 bytes")


def test_load_size_exactly_at_cap_allowed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PRISM_PROMPT_FILE_MAX_BYTES", "16")
    f = _evidence(tmp_path, raw=b"y" * 16)
    res = load_prompt_file(str(f), cwd=tmp_path)
    assert res.error is None
    assert res.receipt is not None
    assert res.receipt["bytes"] == 16


def test_load_bad_env_cap_falls_back_to_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PRISM_PROMPT_FILE_MAX_BYTES", "banana")
    f = _evidence(tmp_path)
    assert load_prompt_file(str(f), cwd=tmp_path).error is None


def test_load_invalid_utf8_rejected(tmp_path: Path) -> None:
    f = _evidence(tmp_path, raw=b"\xff\xfe\x80broken")
    _assert_pf_error(load_prompt_file(str(f), cwd=tmp_path), "not valid UTF-8")


def test_load_sha256_mismatch_rejected(tmp_path: Path) -> None:
    f = _evidence(tmp_path)
    res = load_prompt_file(str(f), cwd=tmp_path, expected_sha256="0" * 64)
    _assert_pf_error(res, "sha256 mismatch")


def test_load_sha256_match_succeeds(tmp_path: Path) -> None:
    f = _evidence(tmp_path)
    res = load_prompt_file(str(f), cwd=tmp_path, expected_sha256=_SHA)
    assert res.error is None
    assert res.receipt is not None
    assert res.receipt["sha256"] == _SHA


@pytest.mark.parametrize(
    "pin",
    [_SHA.upper(), _SHA[:-1], _SHA[:-1] + "g", "0x" + _SHA, f" {_SHA} ", "   "],
    ids=["uppercase", "short", "non-hex", "prefixed", "padded", "whitespace-only"],
)
def test_load_sha256_bad_format_rejected(tmp_path: Path, pin: str) -> None:
    """Contract pins LOWERCASE hex, validated on the raw value; malformed
    pins fail as a format error (never against the file, so no misleading
    'mismatch') and a whitespace-only pin cannot silently disable the check."""
    f = _evidence(tmp_path)
    res = load_prompt_file(str(f), cwd=tmp_path, expected_sha256=pin)
    _assert_pf_error(res, "64 lowercase hex")


# ───── loader: hostile-input and race hardening ─────


@pytest.mark.skipif(sys.platform != "win32", reason="drive-relative is a Windows form")
def test_load_drive_relative_rejected(tmp_path: Path) -> None:
    """Deterministic rejection: "D:foo" would resolve against the per-drive
    process cwd, not `cd`."""
    _evidence(tmp_path)
    res = load_prompt_file(f"{tmp_path.drive}ev.md", cwd=tmp_path)
    _assert_pf_error(res, "drive-relative")


def test_load_nul_in_path_rejected_even_with_existing_prefix(tmp_path: Path) -> None:
    """Windows realpath TRUNCATES at NUL: "ev.md\\0junk" would resolve to
    "ev.md" and load the wrong file. Must be a typed rejection even when
    the truncated prefix exists."""
    _evidence(tmp_path)  # the prefix file EXISTS — truncation would succeed
    res = load_prompt_file("ev.md\x00junk", cwd=tmp_path)
    _assert_pf_error(res, "contains NUL")


def test_load_huge_env_cap_does_not_overflow(tmp_path: Path, monkeypatch) -> None:
    """A sys.maxsize-scale cap is only ever compared, never allocated: a
    normal file must load fine (the probe read is st_size-scaled)."""
    monkeypatch.setenv("PRISM_PROMPT_FILE_MAX_BYTES", str(2**63 - 1))
    f = _evidence(tmp_path)
    res = load_prompt_file(str(f), cwd=tmp_path)
    assert res.error is None
    assert res.content == _CONTENT


def test_load_huge_st_size_overflow_is_typed(tmp_path: Path, monkeypatch) -> None:
    """st_size near sys.maxsize makes the probe length unrepresentable as
    Py_ssize_t — the OverflowError must surface as a typed error."""
    import prism_router.prompt_file as pf_mod

    monkeypatch.setenv("PRISM_PROMPT_FILE_MAX_BYTES", str(2**63 - 1))
    f = _evidence(tmp_path)
    real = f.stat()
    huge = os.stat_result(
        (real.st_mode, real.st_ino, real.st_dev, real.st_nlink, real.st_uid,
         real.st_gid, 2**63 - 1, real.st_atime, real.st_mtime, real.st_ctime),
    )
    real_stat = os.stat
    target = str(f.resolve())  # precomputed: resolve() itself calls os.stat

    def fake_stat(p, *args, **kwargs):
        if str(p) == target:
            return huge
        return real_stat(p, *args, **kwargs)

    monkeypatch.setattr(pf_mod.os, "stat", fake_stat)
    res = load_prompt_file(str(f), cwd=tmp_path)
    _assert_pf_error(res, "unreadable")


def test_load_growth_between_stat_and_read_rejected(
    tmp_path: Path, monkeypatch,
) -> None:
    """File grows after os.stat: the st_size+1 probe read detects it and
    fails closed — the receipt must never attest a torn read."""
    import prism_router.prompt_file as pf_mod

    monkeypatch.setenv("PRISM_PROMPT_FILE_MAX_BYTES", "16")
    f = _evidence(tmp_path, raw=b"z" * 32)  # already larger than the stale stat
    real = f.stat()
    stale = os.stat_result(
        (real.st_mode, real.st_ino, real.st_dev, real.st_nlink, real.st_uid,
         real.st_gid, 16, real.st_atime, real.st_mtime, real.st_ctime),
    )
    real_stat = os.stat
    target = str(f.resolve())  # precomputed: resolve() itself calls os.stat

    def fake_stat(p, *args, **kwargs):
        if str(p) == target:
            return stale
        return real_stat(p, *args, **kwargs)

    monkeypatch.setattr(pf_mod.os, "stat", fake_stat)
    res = load_prompt_file(str(f), cwd=tmp_path)
    _assert_pf_error(res, "changed during read")


def test_load_shrink_between_stat_and_read_accepted(
    tmp_path: Path, monkeypatch,
) -> None:
    """File shrinks after os.stat: EOF yields the complete current content —
    accepted, and the receipt attests the actual (smaller) byte count."""
    import prism_router.prompt_file as pf_mod

    f = _evidence(tmp_path)
    real = f.stat()
    inflated = os.stat_result(
        (real.st_mode, real.st_ino, real.st_dev, real.st_nlink, real.st_uid,
         real.st_gid, len(_RAW) + 16, real.st_atime, real.st_mtime, real.st_ctime),
    )
    real_stat = os.stat
    target = str(f.resolve())  # precomputed: resolve() itself calls os.stat

    def fake_stat(p, *args, **kwargs):
        if str(p) == target:
            return inflated
        return real_stat(p, *args, **kwargs)

    monkeypatch.setattr(pf_mod.os, "stat", fake_stat)
    res = load_prompt_file(str(f), cwd=tmp_path)
    assert res.error is None
    assert res.content == _CONTENT
    assert res.receipt == {"path": target, "sha256": _SHA, "bytes": len(_RAW)}


# ───── server wiring: payload assembly, pre-spawn gate, receipt ─────


def _success_outcome(msg: str = "hello") -> RunOutcome:
    pr = ParseResult(
        session_id="sid-1", agent_messages=msg,
        all_messages=[{"type": "turn.completed"}], noise_log=[],
        transient_lines=[], saw_terminal=True, error_events=[],
    )
    return RunOutcome(
        parse_result=pr, stderr_tail="", kill_chain=[], returncode=0, timed_out=False,
    )


def _wire_dispatcher(monkeypatch, result: DispatchResult | None = None) -> list[dict]:
    """Spy on the spawn layer: records every Dispatcher.invoke call."""
    calls: list[dict] = []
    outcome_result = result or DispatchResult(outcome=_success_outcome())

    class _Fake:
        def __init__(self, **_):
            pass

        async def invoke(self, *, backend_name, prompt, cfg):
            calls.append({"backend_name": backend_name, "prompt": prompt, "cfg": cfg})
            return outcome_result

    monkeypatch.setattr(server_mod, "Dispatcher", _Fake)
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
    return calls


async def _invoke(backend: str, tmp_path: Path, **kw) -> dict:
    if backend == "codex":
        return await server_mod._invoke_codex(
            prompt="task header", cd=tmp_path, sandbox="read-only", session_id="",
            skip_git_repo_check=True, return_all_messages=False,
            model="", profile="", image=None, yolo=False, require_approval=True, **kw,
        )
    return await server_mod._invoke_gemini(
        prompt="task header", cd=tmp_path, session_id="", return_all_messages=False,
        model="", sandbox=False, gemini_default_model="gemini-test", **kw,
    )


@pytest.mark.parametrize("backend", ["codex", "gemini"])
async def test_invoke_appends_file_and_attaches_receipt(
    monkeypatch, tmp_path: Path, backend: str,
) -> None:
    calls = _wire_dispatcher(monkeypatch)
    f = _evidence(tmp_path)
    result = await _invoke(backend, tmp_path, prompt_file="ev.md")
    assert len(calls) == 1
    # Payload: AGENT_SIDE prepend (all calls) + PROMPT + "\n\n" + file verbatim.
    assert calls[0]["prompt"].startswith(AGENT_SIDE)
    assert calls[0]["prompt"].endswith(f"task header\n\n{_CONTENT}")
    assert result["success"] is True
    assert set(result.keys()) == _BASE_KEYS | {"prompt_file_receipt"}
    assert result["prompt_file_receipt"] == {
        "path": str(f.resolve()), "sha256": _SHA, "bytes": len(_RAW),
    }


@pytest.mark.parametrize("backend", ["codex", "gemini"])
@pytest.mark.parametrize(
    ("file_arg", "sha_arg", "reason_marker"),
    [
        ("missing.md", "", "not found"),
        ("../escape.md", "", "outside allowed roots"),
        ("ev.md", "0" * 64, "sha256 mismatch"),
    ],
    ids=["missing", "boundary", "sha-mismatch"],
)
async def test_invoke_prompt_file_error_is_pre_spawn(
    monkeypatch, tmp_path: Path, backend: str,
    file_arg: str, sha_arg: str, reason_marker: str,
) -> None:
    """Rule 6/10: fail-closed BEFORE spawn — Dispatcher untouched, no
    SESSION_ID, no receipt, distinct typed class."""
    calls = _wire_dispatcher(monkeypatch)
    cd = tmp_path / "cd"
    cd.mkdir()
    _evidence(cd)
    _evidence(tmp_path, "escape.md")
    result = await _invoke(
        backend, cd, prompt_file=file_arg, prompt_file_sha256=sha_arg,
    )
    assert calls == []  # backend never spawned
    assert result["success"] is False
    assert result["SESSION_ID"] is None
    assert result["error"]["code"] == "prompt_file_error"
    assert result["error"]["stage"] == "pre_flight"
    assert result["error"]["retryable"] is False
    assert reason_marker in result["error"]["raw"]
    assert "prompt_file_receipt" not in result


@pytest.mark.parametrize("backend", ["codex", "gemini"])
async def test_invoke_without_prompt_file_envelope_unchanged(
    monkeypatch, tmp_path: Path, backend: str,
) -> None:
    """Rule 11: PROMPT-only calls keep the exact pre-feature key set."""
    calls = _wire_dispatcher(monkeypatch)
    result = await _invoke(backend, tmp_path)
    assert len(calls) == 1
    assert calls[0]["prompt"].endswith("task header")  # no separator appended
    assert set(result.keys()) == _BASE_KEYS


async def test_gemini_retry_reuses_identical_injected_payload(
    monkeypatch, tmp_path: Path,
) -> None:
    """The file is loaded ONCE before the gemini retry loop: the internal
    re-dispatch re-sends the byte-identical combined payload and the final
    envelope keeps the receipt alongside the retry indicator."""
    results = [
        DispatchResult(outcome=RunOutcome(
            parse_result=ParseResult(
                session_id="sid-err", agent_messages="", all_messages=[],
                noise_log=[], transient_lines=[], saw_terminal=True,
                error_events=[{"type": "error", "message":
                    "Invalid stream: The model returned an empty response or malformed tool call."}],
            ),
            stderr_tail="", kill_chain=[], returncode=0, timed_out=False,
        )),
        DispatchResult(outcome=_success_outcome("second try")),
    ]
    calls: list[dict] = []

    class _Fake:
        def __init__(self, **_):
            pass

        async def invoke(self, *, backend_name, prompt, cfg):
            calls.append({"backend_name": backend_name, "prompt": prompt, "cfg": cfg})
            return results[len(calls) - 1]

    monkeypatch.setattr(server_mod, "Dispatcher", _Fake)
    monkeypatch.setattr(server_mod, "_GEMINI_TRANSIENT_RETRY_DELAY_S", 0.0)
    loads: list[str] = []
    real_load = server_mod.load_prompt_file
    monkeypatch.setattr(
        server_mod, "load_prompt_file",
        lambda *a, **k: loads.append(a[0]) or real_load(*a, **k),
    )
    _evidence(tmp_path)
    result = await _invoke("gemini", tmp_path, prompt_file="ev.md")
    assert loads == ["ev.md"]  # exactly ONE filesystem load across both dispatches
    assert len(calls) == 2
    assert calls[0]["prompt"] == calls[1]["prompt"]
    assert calls[0]["prompt"].endswith(f"task header\n\n{_CONTENT}")
    assert result["success"] is True
    assert result["prompt_file_receipt"]["sha256"] == _SHA
    assert result["diagnostics"]["internal_retries"] == 1


async def test_receipt_survives_backend_error(monkeypatch, tmp_path: Path) -> None:
    """Receipt attests wrapper-side injection, not backend outcome: a
    post-spawn backend failure keeps it so callers can still prove what
    the backend was given."""
    pr = ParseResult(
        session_id="sid-err", agent_messages="", all_messages=[], noise_log=[],
        transient_lines=[], saw_terminal=True,
        error_events=[{"type": "error", "message": "tool runtime crashed"}],
    )
    outcome = RunOutcome(
        parse_result=pr, stderr_tail="", kill_chain=[], returncode=0, timed_out=False,
    )
    _wire_dispatcher(monkeypatch, DispatchResult(outcome=outcome))
    _evidence(tmp_path)
    result = await _invoke("codex", tmp_path, prompt_file="ev.md")
    assert result["success"] is False
    assert result["error"]["code"] == "codex_error"
    assert result["prompt_file_receipt"]["sha256"] == _SHA


@pytest.mark.parametrize("backend", ["codex", "gemini"])
async def test_slow_load_does_not_block_event_loop(
    monkeypatch, tmp_path: Path, backend: str,
) -> None:
    """A stalled load (git spawn) must leave the event loop responsive.
    docs/bugs/prompt-file-git-dir-stdin-hang.md"""
    _wire_dispatcher(monkeypatch)
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    real_load = server_mod.load_prompt_file

    def stalled_load(*a, **k):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "the event loop never got to release the load"
        return real_load(*a, **k)

    monkeypatch.setattr(server_mod, "load_prompt_file", stalled_load)
    _evidence(tmp_path)
    call = asyncio.create_task(_invoke(backend, tmp_path, prompt_file="ev.md"))
    try:
        await asyncio.wait_for(entered.wait(), 2)  # the load is stalled...
        assert not call.done()  # ...while this coroutine still runs on the loop
    finally:
        release.set()
        result = await asyncio.wait_for(call, 5)
    assert result["success"] is True


# ───── MCP surface: schema + receipt across the FastMCP boundary ─────


async def test_tool_schemas_expose_prompt_file_params(monkeypatch) -> None:
    def fake(cmd: str, *, timeout_sec: float = 5.0) -> bool:
        return True

    monkeypatch.setattr(server_mod, "smoke_probe", fake)
    async with Client(build_server()) as client:
        tools = {t.name: t for t in await client.list_tools()}
    for name in ("codex", "gemini"):
        props = tools[name].inputSchema["properties"]
        assert "prompt_file" in props
        assert "prompt_file_sha256" in props
        assert "prompt_file" in (tools[name].description or "")


async def test_receipt_roundtrips_through_fastmcp(monkeypatch, tmp_path: Path) -> None:
    def fake(cmd: str, *, timeout_sec: float = 5.0) -> bool:
        return True

    monkeypatch.setattr(server_mod, "smoke_probe", fake)
    _wire_dispatcher(monkeypatch)
    _evidence(tmp_path)
    async with Client(build_server()) as client:
        res = await client.call_tool(
            "codex",
            {"PROMPT": "task header", "cd": str(tmp_path), "prompt_file": "ev.md"},
        )
        legacy = await client.call_tool(
            "codex", {"PROMPT": "task header", "cd": str(tmp_path)},
        )
    envelope = res.structured_content
    assert envelope["success"] is True
    assert envelope["prompt_file_receipt"]["sha256"] == _SHA
    assert envelope["prompt_file_receipt"]["bytes"] == len(_RAW)
    # Rule 11 at the wire: PROMPT-only envelope has no receipt key at all.
    assert "prompt_file_receipt" not in legacy.structured_content
