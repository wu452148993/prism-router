"""Server-side helper spawns must not inherit the MCP stdin pipe.

The stdio transport keeps a worker thread blocked in a synchronous read on
stdin. On Windows I/O on that synchronous file object is serialized; a child's
startup operation on an inherited duplicate can wait behind the read until a
message or EOF lets it complete.
docs/bugs/prompt-file-git-dir-stdin-hang.md
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from prism_router.prompt_file import _git_dir
from prism_router.server import smoke_probe

# Child = a stand-in MCP server: a daemon thread parks in a stdin read the
# parent never satisfies, then the helper under test spawns its git child.
_PROBE = """
import json, os, sys, threading, time
from pathlib import Path

if sys.argv[1] == "git_dir":
    from prism_router.prompt_file import _git_dir
    call = lambda: _git_dir(Path(sys.argv[2]))
else:
    from prism_router.server import smoke_probe
    call = lambda: smoke_probe("git")
threading.Thread(target=sys.stdin.buffer.read, args=(1,), daemon=True).start()
time.sleep(0.5)
start = time.monotonic()
result = call()
print(json.dumps({"result": str(result), "elapsed": time.monotonic() - start}), flush=True)
os._exit(0)  # skip finalization: the parked reader holds stdin's buffer lock
"""


@pytest.mark.parametrize("mode", ["git_dir", "smoke_probe"])
def test_helper_spawn_holds_no_server_std_handle(
    monkeypatch, tmp_path: Path, mode: str,
) -> None:
    """Platform-independent contract behind the Windows reproduction below."""
    seen: dict = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=str(tmp_path), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    if mode == "git_dir":
        _git_dir(tmp_path)
    else:
        smoke_probe(sys.executable)
    assert seen["stdin"] == subprocess.DEVNULL
    assert seen["capture_output"] is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows synchronous-pipe semantics")
@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
@pytest.mark.parametrize("mode", ["git_dir", "smoke_probe"])
def test_spawn_unaffected_by_pending_stdin_read(tmp_path: Path, mode: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _PROBE, mode, str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        proc.wait(timeout=20)  # stdin stays open meanwhile: no EOF can rescue a stall
    except subprocess.TimeoutExpired:
        pytest.fail("the helper stalled behind the parked stdin read")
    finally:
        proc.stdin.close()  # EOF releases a stall even if taskkill fails
        if proc.poll() is None:  # sys.executable is a venv launcher: kill the tree
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
    got = json.loads(proc.communicate(timeout=10)[0])
    expected = str((tmp_path / ".git").resolve()) if mode == "git_dir" else "True"
    assert got["result"] == expected
    assert got["elapsed"] < 2.5  # half the 5 s timeout: not rescued by it
