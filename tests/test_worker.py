"""Worker subprocess module — IPC framing + sidecar serialization."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import sys

import pytest

from prism_router import worker as worker_mod
from prism_router.parser import ParseResult
from prism_router.runner import RunOutcome


def _make_runoutcome(msg: str = "hi") -> RunOutcome:
    pr = ParseResult(
        session_id="sid-1",
        agent_messages=msg,
        all_messages=[{"type": "thread.started", "thread_id": "sid-1"}],
        noise_log=[],
        transient_lines=[],
        saw_terminal=True,
        error_events=[],
    )
    return RunOutcome(parse_result=pr, stderr_tail="", returncode=0)


def test_runoutcome_to_dict_roundtrips_parse_result():
    out = _make_runoutcome("hello")
    d = worker_mod._runoutcome_to_dict(out)
    assert d["parse_result"]["session_id"] == "sid-1"
    assert d["parse_result"]["agent_messages"] == "hello"
    assert d["parse_result"]["saw_terminal"] is True
    assert d["returncode"] == 0
    assert d["kill_chain"] == []


def test_read_framed_stdin_parses_content_length():
    payload = json.dumps({"backend": "codex", "prompt": "hi"}).encode("utf-8")
    framed = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload
    result = worker_mod._read_framed_stdin(io.BytesIO(framed))
    assert result == {"backend": "codex", "prompt": "hi"}


def test_read_framed_stdin_rejects_oversize_header():
    huge_header = b"X" * 300 + b"\r\n\r\n"
    with pytest.raises(ValueError, match="header"):
        worker_mod._read_framed_stdin(io.BytesIO(huge_header))


def test_read_framed_stdin_rejects_oversize_body():
    too_big = 5 * 1024 * 1024
    framed = f"Content-Length: {too_big}\r\n\r\n".encode("ascii")
    with pytest.raises(ValueError, match="Content-Length"):
        worker_mod._read_framed_stdin(io.BytesIO(framed))


def test_read_framed_stdin_eof_during_header():
    with pytest.raises(EOFError):
        worker_mod._read_framed_stdin(io.BytesIO(b""))


def test_write_framed_stdout_inline_under_threshold(tmp_path):
    payload = {"parse_result": {"session_id": "s", "agent_messages": "tiny",
                                "all_messages": [], "noise_log": [],
                                "transient_lines": [], "saw_terminal": True,
                                "error_events": []},
               "stderr_tail": "", "kill_chain": [], "returncode": 0,
               "timed_out": False}
    out_buf = io.BytesIO()
    worker_mod._write_framed_stdout(payload, tmp_path, out=out_buf)
    raw = out_buf.getvalue()
    sep = raw.find(b"\r\n\r\n")
    assert sep > 0
    header = raw[:sep].decode("ascii")
    body = raw[sep + 4:]
    assert "Content-Length" in header
    n = int(header.split(":")[1].strip())
    assert len(body) == n
    assert not (tmp_path / "runoutcome.json").exists()


def test_write_framed_stdout_sidecar_over_threshold(tmp_path, capsys):
    big = "z" * (worker_mod._SIDECAR_THRESHOLD + 100)
    payload = {"parse_result": {"session_id": "s", "agent_messages": big,
                                "all_messages": [], "noise_log": [],
                                "transient_lines": [], "saw_terminal": True,
                                "error_events": []},
               "stderr_tail": "", "kill_chain": [], "returncode": 0,
               "timed_out": False}
    out_buf = io.BytesIO()
    worker_mod._write_framed_stdout(payload, tmp_path, out=out_buf)
    raw = out_buf.getvalue()
    sep = raw.find(b"\r\n\r\n")
    body = raw[sep + 4:]
    ref = json.loads(body)
    assert "runoutcome_ref" in ref
    assert "sha256" in ref
    sidecar = tmp_path / "runoutcome.json"
    assert sidecar.exists()
    contents = sidecar.read_bytes()
    assert hashlib.sha256(contents).hexdigest() == ref["sha256"]
    # Milestone emitted to stderr.
    captured = capsys.readouterr()
    assert "sidecar_published" in captured.err


def test_worker_module_import_has_no_side_effects():
    """Importing prism_router.worker MUST NOT spawn any subprocess or open
    files. Reach-here-without-crash is the assertion."""
    if "prism_router.worker" in sys.modules:
        del sys.modules["prism_router.worker"]
    importlib.import_module("prism_router.worker")
