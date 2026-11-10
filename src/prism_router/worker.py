"""Per-call worker subprocess entry.

Reads a Content-Length framed JSON request on stdin, runs the backend via
`runner.run`, emits L2 milestone events to stderr, writes the RunOutcome
(Content-Length framed) to stdout, exits 0. On any uncaught exception, emits
a best-effort transport-error RunOutcome and exits 1 — the dispatcher in main
synthesizes a `transport_failure` envelope from whatever it received.

ZERO top-level side effects: importing `prism_router.worker` MUST NOT spawn
any subprocess, open files, or hit the network. All work is under __main__.

Spawned by `prism_router.dispatcher.Dispatcher` as
`python -u -m prism_router.worker` with stdin/stdout/stderr=PIPE and env
preserving PYTHONNOUSERSITE=1 + PYTHONUNBUFFERED=1 + dispatcher's allowlist.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path

from .backend import SpawnConfig
from .backend_codex import CodexBackend
from .backend_gemini import GeminiBackend
from .runner import RunOutcome, RunTimeouts, run

# >1 MiB → write outcome to sidecar; stdout frame carries a reference.
_SIDECAR_THRESHOLD = 1024 * 1024
_HEADER_CAP = 256
_BODY_CAP = 4 * 1024 * 1024


def _emit_milestone(ev: str, **payload) -> None:
    obj = {"ev": ev, "ts": time.time(), **payload}
    sys.stderr.write(json.dumps(obj, ensure_ascii=True) + "\n")
    sys.stderr.flush()


def _read_framed_stdin(raw=None) -> dict:
    if raw is None:
        raw = sys.stdin.buffer
    header = bytearray()
    while True:
        b = raw.read(1)
        if not b:
            raise EOFError("worker stdin closed during header read")
        header.extend(b)
        if header.endswith(b"\r\n\r\n"):
            break
        if len(header) > _HEADER_CAP:
            raise ValueError("Content-Length header exceeds cap")
    text = bytes(header).decode("ascii", "replace").strip()
    n: int | None = None
    for part in text.split("\r\n"):
        k, _, v = part.partition(":")
        if k.strip().lower() == "content-length":
            n = int(v.strip())
            break
    if n is None or n < 0 or n > _BODY_CAP:
        raise ValueError(f"invalid Content-Length: {text!r}")
    body = raw.read(n)
    if len(body) != n:
        raise EOFError(f"short read: got {len(body)} of {n}")
    return json.loads(body.decode("utf-8"))


def _runoutcome_to_dict(outcome: RunOutcome) -> dict:
    pr = outcome.parse_result
    return {
        "parse_result": {
            "session_id": pr.session_id,
            "agent_messages": pr.agent_messages,
            "all_messages": pr.all_messages,
            "noise_log": pr.noise_log,
            "transient_lines": pr.transient_lines,
            "saw_terminal": pr.saw_terminal,
            "error_events": pr.error_events,
        },
        "stderr_tail": outcome.stderr_tail,
        "kill_chain": [dataclasses.asdict(s) for s in outcome.kill_chain],
        "returncode": outcome.returncode,
        "timed_out": outcome.timed_out,
    }


def _write_framed_stdout(payload: dict, sidecar_dir: Path | None, out=None) -> None:
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    if len(body) > _SIDECAR_THRESHOLD and sidecar_dir is not None:
        sha = hashlib.sha256(body).hexdigest()
        target = sidecar_dir / "runoutcome.json"
        tmp = sidecar_dir / "runoutcome.json.tmp"
        with open(tmp, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        _emit_milestone(
            "sidecar_published",
            size=len(body), sha256=sha, path=target.name,
        )
        body = json.dumps(
            {"runoutcome_ref": target.name, "size": len(body), "sha256": sha},
            ensure_ascii=True,
        ).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    if out is None:
        out = sys.stdout.buffer
    out.write(header)
    out.write(body)
    out.flush()


async def _run_async(request: dict) -> RunOutcome:
    backend_name = request["backend"]
    backend = CodexBackend() if backend_name == "codex" else GeminiBackend()
    sc = request["spawn_config"]
    cfg = SpawnConfig(
        cwd=Path(sc["cwd"]),
        sandbox=sc.get("sandbox", ""),
        session_id=sc.get("session_id") or None,
        model=sc.get("model", ""),
        profile=sc.get("profile", ""),
        skip_git_repo_check=bool(sc.get("skip_git_repo_check", True)),
        yolo=bool(sc.get("yolo", False)),
        images=tuple(Path(p) for p in sc.get("images", [])),
    )
    timeouts = RunTimeouts(
        codex_exit_drain=float(
            request.get("timeouts", {}).get("codex_exit_drain", 5.0)
        ),
    )
    return await run(
        backend, request["prompt"], cfg,
        timeouts=timeouts, on_milestone=_emit_milestone,
    )


def main() -> int:
    try:
        request = _read_framed_stdin()
    except Exception as exc:
        _emit_milestone("terminated", error=f"stdin: {exc}")
        return 1
    sidecar_dir = (
        Path(request["sidecar_dir"]) if request.get("sidecar_dir") else None
    )
    try:
        outcome = asyncio.run(_run_async(request))
    except Exception as exc:
        _emit_milestone("terminating", error=str(exc))
        try:
            _write_framed_stdout(
                {
                    "parse_result": None,
                    "stderr_tail": "",
                    "kill_chain": [],
                    "returncode": None,
                    "timed_out": False,
                    "worker_exception": "".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    ),
                },
                sidecar_dir,
            )
        except Exception:
            pass
        _emit_milestone("terminated", error="exception")
        return 1
    try:
        _emit_milestone("terminating")
        _write_framed_stdout(_runoutcome_to_dict(outcome), sidecar_dir)
        _emit_milestone("terminated")
    except Exception as exc:
        _emit_milestone("terminated", error=f"stdout: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
