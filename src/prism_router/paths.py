"""§3.3 path normalization. Idempotent on `cd` ingress (server.py boundary).

Two normalizations matter (CCG `aae2ad7` + audit H1):
- Git Bash `/c/Users/foo` → `C:/Users/foo` (Windows only; harmless abs path on POSIX)
- `os.path.normcase` so `D:\foo`, `D:/foo`, `d:\\foo` collapse to one canonical
  string. Gemini stores sessions under `sha256(absolute cwd)` — byte-level drift
  in the input means lost session lookup.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_GITBASH_DRIVE = re.compile(r"^/([a-zA-Z])(/|$)")


def normalize_cwd(p: str | os.PathLike[str]) -> Path:
    """Canonical absolute Path; equivalent inputs map to byte-identical str().

    Lexical-only — never touches filesystem (no symlink resolution). Gemini
    sessions are keyed by sha256 of the cwd string the user's shell passes;
    expanding symlinks here would produce a different hash than what the CLI
    actually saw (codex audit ISSUE 2026-05-03).
    """
    raw = os.fspath(p)
    if sys.platform == "win32":
        m = _GITBASH_DRIVE.match(raw)
        if m:
            raw = f"{m.group(1).upper()}:/" + raw[m.end():]
        absolute = os.path.abspath(os.path.normpath(raw))
        return Path(os.path.normcase(absolute))
    return Path(os.path.abspath(os.path.normpath(raw)))
