"""Dispatcher tests with injected worker spawner + job assigner."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from prism_router import _job_object as _jo
from prism_router.backend import SpawnConfig
from prism_router.dispatcher import (
    STDIN_PREFLIGHT_CAP,
    DispatchResult,
    Dispatcher,
    _env_float,
)


def _cfg(cwd: Path) -> SpawnConfig:
    return SpawnConfig(
        cwd=cwd, sandbox="read-only", session_id=None,
        model="", profile="", skip_git_repo_check=True, yolo=False, images=(),
    )


def _frame(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


class _FakeReader:
    def __init__(self, data: bytes):
        self._buf = bytearray(data)
        self._closed = False
        self._extend_event = asyncio.Event()
        if data:
            self._extend_event.set()

    async def read(self, n: int = -1) -> bytes:
        while not self._buf and not self._closed:
            self._extend_event.clear()
            await self._extend_event.wait()
        if not self._buf:
            return b""
        if n < 0 or n > len(self._buf):
            n = len(self._buf)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def extend(self, more: bytes) -> None:
        self._buf.extend(more)
        self._extend_event.set()

    def close(self) -> None:
        self._closed = True
        self._extend_event.set()


class _FakeWriter:
    def __init__(self):
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)


class _FakeWorkerProc:
    """asyncio.subprocess.Process surface for dispatcher tests."""

    def __init__(self, *, stdout: bytes, stderr: bytes = b"", rc: int = 0):
        self.pid = 9999
        self.stdin = _FakeWriter()
        self.stdout = _FakeReader(stdout)
        self.stderr = _FakeReader(stderr)
        self._rc = rc
        self._exit = asyncio.Event()
        self._exit.set()  # default: instant

    @property
    def returncode(self) -> int | None:
        return self._rc

    async def wait(self) -> int:
        await self._exit.wait()
        return self._rc

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


class _FakeJob:
    def __init__(self):
        self.closed = False
        self.handle = 0xCAFE

    def close(self):
        self.closed = True


def _success_frame() -> bytes:
    return _frame({
        "parse_result": {
            "session_id": "sid-1", "agent_messages": "hi",
            "all_messages": [], "noise_log": [], "transient_lines": [],
            "saw_terminal": True, "error_events": [],
        },
        "stderr_tail": "", "kill_chain": [], "returncode": 0, "timed_out": False,
    })


# Byte-identical contract for kills where rollout liveness never tracked the
# file (docs/bugs/rollout-liveness-tick.md): threshold 0.2s, worker rc 0.
_UNTRACKED_KILL_RAW_02S = (
    "no tick-eligible stdout for 0s after first_codex_output (rc=0; "
    "recognized reconnect chatter excluded); upstream codex CLI appears "
    "wedged or stuck reconnecting. Retry with PRISM_PRE_TERMINAL_IDLE=3600 "
    "if this is a known long-silence task."
)


@pytest.mark.asyncio
async def test_dispatch_success_returns_outcome(tmp_path):
    proc = _FakeWorkerProc(stdout=_success_frame())
    fake_job = _FakeJob()

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign,
        temp_root=tmp_path, overall_timeout=2.0, post_terminal_grace=2.0,
    )
    result = await d.invoke(backend_name="codex", prompt="hi", cfg=_cfg(tmp_path))
    assert isinstance(result, DispatchResult)
    assert result.error is None
    assert result.outcome is not None
    assert result.outcome.parse_result.session_id == "sid-1"
    assert fake_job.closed is True


@pytest.mark.asyncio
async def test_dispatch_preflight_request_too_large(tmp_path):
    spawn_called = {"n": 0}

    async def spawn(env):
        spawn_called["n"] += 1
        return _FakeWorkerProc(stdout=b"")

    def assign(pid):
        return _FakeJob()

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
    )
    huge_prompt = "x" * (STDIN_PREFLIGHT_CAP + 100)
    result = await d.invoke(
        backend_name="codex", prompt=huge_prompt, cfg=_cfg(tmp_path),
    )
    assert result.outcome is None
    assert result.error is not None
    assert result.error["code"] == "request_too_large"
    assert spawn_called["n"] == 0  # worker never spawned


@pytest.mark.asyncio
async def test_dispatch_job_assign_failure_returns_spawn_failed(tmp_path):
    proc = _FakeWorkerProc(stdout=_success_frame())

    async def spawn(env):
        return proc

    def assign(pid):
        raise _jo.JobAssignError("AssignProcessToJobObject", errno=5)

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=2.0,
    )
    result = await d.invoke(backend_name="codex", prompt="hi", cfg=_cfg(tmp_path))
    assert result.outcome is None
    assert result.error is not None
    assert result.error["code"] == "spawn_failed"
    assert "AssignProcessToJobObject" in result.error["raw"]


@pytest.mark.asyncio
async def test_dispatch_cancel_propagates(tmp_path):
    """MCP cancel must re-raise CancelledError (not synthesize envelope)."""
    proc = _FakeWorkerProc(stdout=b"")  # never delivers anything
    fake_job = _FakeJob()

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=10.0, post_terminal_grace=10.0,
    )
    task = asyncio.create_task(
        d.invoke(backend_name="codex", prompt="hi", cfg=_cfg(tmp_path))
    )
    await asyncio.sleep(0.05)  # let dispatch enter wait
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake_job.closed is True


@pytest.mark.asyncio
async def test_dispatch_overall_timeout_kills_returns_timeout(tmp_path):
    proc = _FakeWorkerProc(stdout=b"")
    fake_job = _FakeJob()

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=0.1, post_terminal_grace=10.0,
    )

    # When dispatcher decides timeout, it CloseHandles job and synthesizes envelope.
    # In real life the OS reaps the worker; here proc never delivers stdout.
    # We need to unblock _read_framed_stdout's first-byte wait. Job close in
    # production kills worker → stdout pipe gets EOF naturally. In fake, we
    # close the reader when job closes.
    real_close = fake_job.close

    def _close_and_eof():
        proc.stdout.close()
        proc.stderr.close()
        proc._exit.set()
        real_close()
    fake_job.close = _close_and_eof  # type: ignore

    result = await d.invoke(backend_name="codex", prompt="hi", cfg=_cfg(tmp_path))
    assert result.outcome is None
    assert result.error is not None
    assert result.error["code"] == "timeout"


@pytest.mark.asyncio
async def test_pre_terminal_idle_watchdog_kills_on_stdout_silence(tmp_path):
    """Worker emits `first_codex_output` then stays silent past threshold.
    Dispatcher's pre_terminal_idle watchdog must fire and Job-close."""
    # stderr: only first_codex_output milestone, then silent.
    first_output = b'{"ev":"first_codex_output","ts":1.0}\n'
    proc = _FakeWorkerProc(stdout=b"", stderr=first_output)
    proc._exit.clear()  # don't auto-exit
    fake_job = _FakeJob()

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    real_close = fake_job.close

    def _close_and_eof():
        proc.stdout.close()
        proc.stderr.close()
        proc._exit.set()
        real_close()
    fake_job.close = _close_and_eof  # type: ignore[method-assign]

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=10.0,        # well above test threshold
        post_terminal_grace=10.0,    # not armed in this scenario
        pre_terminal_idle=0.2,       # 200ms — fast for tests
    )
    result = await d.invoke(backend_name="codex", prompt="hi", cfg=_cfg(tmp_path))
    assert result.outcome is None
    assert result.error is not None
    assert result.error["code"] == "pre_terminal_idle"
    assert result.error["retryable"] is True
    assert "PRISM_PRE_TERMINAL_IDLE" in result.error["raw"]
    assert fake_job.closed is True
    # Diagnostics must include the observability fields.
    assert result.diagnostics is not None
    assert "max_stdout_silence_s" in result.diagnostics
    assert result.diagnostics["saw_terminal_event"] is False
    # No thread_id ⇒ rollout liveness never tracked ⇒ raw and diagnostics
    # keep their pre-liveness shape (docs/bugs/rollout-liveness-tick.md).
    # Full-equality pin: this exact text is the byte-identical contract for
    # untracked kills.
    assert result.error["raw"] == _UNTRACKED_KILL_RAW_02S
    assert "rollout_ticks" not in result.diagnostics


@pytest.mark.asyncio
async def test_pre_terminal_idle_not_armed_without_first_output(tmp_path):
    """If `first_codex_output` never arrives (e.g., codex stalls pre-stream),
    the pre_terminal_idle watchdog must NOT arm — Phase 1 of
    `_read_framed_stdout` is governed by overall_timeout + kill_event upstream."""
    proc = _FakeWorkerProc(stdout=b"", stderr=b"")  # no milestones at all
    proc._exit.clear()
    fake_job = _FakeJob()

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    real_close = fake_job.close

    def _close_and_eof():
        proc.stdout.close()
        proc.stderr.close()
        proc._exit.set()
        real_close()
    fake_job.close = _close_and_eof  # type: ignore[method-assign]

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=0.3,         # this is what should fire
        post_terminal_grace=10.0,
        pre_terminal_idle=0.1,       # would fire IF armed
    )
    result = await d.invoke(backend_name="codex", prompt="hi", cfg=_cfg(tmp_path))
    assert result.error is not None
    # overall_timeout, not pre_terminal_idle — watchdog stayed disarmed.
    assert result.error["code"] == "timeout"


@pytest.mark.asyncio
async def test_pre_terminal_idle_cancelled_by_terminal_event(tmp_path):
    """When `terminal_event_seen` arrives, pre_terminal_idle must be
    cancelled — post_terminal_grace takes over (or worker exits cleanly)."""
    # Stderr emits both milestones in quick succession, then worker
    # delivers a successful outcome via stdout.
    stderr_blob = (
        b'{"ev":"first_codex_output","ts":1.0}\n'
        b'{"ev":"terminal_event_seen","ts":1.05}\n'
    )
    proc = _FakeWorkerProc(stdout=_success_frame(), stderr=stderr_blob)
    fake_job = _FakeJob()

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=2.0,
        post_terminal_grace=2.0,
        pre_terminal_idle=0.2,  # short — would kill if not cancelled
    )
    # Even though pre_terminal_idle is 200ms, terminal_event_seen disarms it
    # immediately, and the worker delivers an outcome → success path.
    result = await d.invoke(backend_name="codex", prompt="hi", cfg=_cfg(tmp_path))
    assert result.error is None
    assert result.outcome is not None
    assert result.outcome.parse_result.saw_terminal is True


@pytest.mark.asyncio
async def test_pre_terminal_idle_silence_recorded_on_terminal_event(tmp_path):
    """`max_stdout_silence_s` must reflect the gap between
    `first_codex_output` and `terminal_event_seen` even when no
    `stdout_tick` fires in between (e.g., quick prompt completes before the
    2s tick interval expires). Without this, a successful call's
    diagnostic would always report 0.0 and we'd never learn the real silence
    distribution to tighten the threshold."""
    # Stdout and stderr both start empty; the driver injects events with
    # controlled timing so terminal_event_seen reaches _drain_stderr BEFORE
    # the success frame reaches _read_framed_stdout.
    proc = _FakeWorkerProc(stdout=b"", stderr=b"")
    proc._exit.clear()
    fake_job = _FakeJob()

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=5.0, post_terminal_grace=2.0,
        pre_terminal_idle=10.0,
    )

    async def driver():
        # Phase 1: arm watchdog via first_codex_output.
        await asyncio.sleep(0.01)
        proc.stderr.extend(b'{"ev":"first_codex_output","ts":1.0}\n')
        # Phase 2: simulate silent reasoning for 0.3s.
        await asyncio.sleep(0.3)
        # Phase 3: terminal milestone + outcome arrive together.
        proc.stderr.extend(b'{"ev":"terminal_event_seen","ts":1.3}\n')
        # Give _drain_stderr a tick to handle terminal_event_seen before
        # the outcome shows up on stdout.
        await asyncio.sleep(0.01)
        proc.stdout.extend(_success_frame())
        proc._exit.set()

    asyncio.create_task(driver())
    result = await d.invoke(backend_name="codex", prompt="hi", cfg=_cfg(tmp_path))
    assert result.error is None
    assert result.outcome is not None
    assert result.diagnostics is not None
    silence = result.diagnostics["max_stdout_silence_s"]
    # ~0.3s gap; allow slack for scheduling.
    assert 0.2 <= silence < 2.0, silence
    assert result.diagnostics["saw_terminal_event"] is True


@pytest.mark.asyncio
async def test_pre_terminal_idle_silence_recorded_on_kill(tmp_path):
    """When the watchdog kills, diagnostics must include the actual observed
    silence (≈threshold), not the initial 0.0 sentinel."""
    first_output = b'{"ev":"first_codex_output","ts":1.0}\n'
    proc = _FakeWorkerProc(stdout=b"", stderr=first_output)
    proc._exit.clear()
    fake_job = _FakeJob()

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    real_close = fake_job.close

    def _close_and_eof():
        proc.stdout.close()
        proc.stderr.close()
        proc._exit.set()
        real_close()
    fake_job.close = _close_and_eof  # type: ignore[method-assign]

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=10.0, post_terminal_grace=10.0,
        pre_terminal_idle=0.2,
    )
    result = await d.invoke(backend_name="codex", prompt="hi", cfg=_cfg(tmp_path))
    assert result.error is not None
    assert result.error["code"] == "pre_terminal_idle"
    assert result.diagnostics is not None
    silence = result.diagnostics["max_stdout_silence_s"]
    # Watchdog poll floor for tiny threshold is 0.05s; observed silence
    # at trigger should be very close to threshold (0.2s).
    assert 0.15 <= silence < 1.5, silence


@pytest.mark.asyncio
async def test_pre_terminal_idle_gated_off_for_gemini(tmp_path):
    """The watchdog is codex-specific: gemini calls must bypass it even
    when the dispatcher's `pre_terminal_idle` is set short. Gemini stalls
    have their own failure mode (no_session, parse_error, etc.) and the
    codex-rollout-based threshold doesn't apply."""
    first_output = b'{"ev":"first_codex_output","ts":1.0}\n'
    proc = _FakeWorkerProc(stdout=b"", stderr=first_output)
    proc._exit.clear()
    fake_job = _FakeJob()

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    real_close = fake_job.close

    def _close_and_eof():
        proc.stdout.close()
        proc.stderr.close()
        proc._exit.set()
        real_close()
    fake_job.close = _close_and_eof  # type: ignore[method-assign]

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=0.5,         # this should fire instead
        post_terminal_grace=10.0,
        pre_terminal_idle=0.1,       # would fire if armed
    )
    result = await d.invoke(backend_name="gemini", prompt="hi", cfg=_cfg(tmp_path))
    assert result.error is not None
    # Gemini calls bypass pre_terminal_idle; overall_timeout fires instead.
    assert result.error["code"] == "timeout"


@pytest.mark.asyncio
async def test_pre_terminal_idle_zero_disables_watchdog(tmp_path):
    """pre_terminal_idle=0 must disable the watchdog (no arm at all)."""
    first_output = b'{"ev":"first_codex_output","ts":1.0}\n'
    proc = _FakeWorkerProc(stdout=_success_frame(), stderr=first_output)
    fake_job = _FakeJob()

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=2.0,
        post_terminal_grace=2.0,
        pre_terminal_idle=0.0,
    )
    result = await d.invoke(backend_name="codex", prompt="hi", cfg=_cfg(tmp_path))
    assert result.error is None
    assert result.outcome is not None


# ──────────────── rollout recovery tests ────────────────
# See docs/bugs/rollout-recovery.md. Each test sets up a fake CODEX_HOME so
# `_rollout_recovery.recover()` (called inside `_run_worker` via asyncio.to_thread)
# locates the fixture by UUID-suffix glob.


def _install_fake_rollout(
    codex_home: Path, fixture_name: str, *, thread_id: str,
) -> Path:
    import shutil as _sh
    fixtures = Path(__file__).parent / "fixtures" / "rollouts"
    dir_ = codex_home / "sessions" / "2026" / "05" / "18"
    dir_.mkdir(parents=True, exist_ok=True)
    target = dir_ / f"rollout-2026-05-18T02-16-36-{thread_id}.jsonl"
    _sh.copy(fixtures / fixture_name, target)
    return target


def _restamp_now(path: Path) -> None:
    """Rewrite every record's timestamp to the current UTC instant. Called from
    the fake spawner so the rollout is (re)written AFTER the dispatcher takes
    its recovery baseline, exactly as codex does in production — recovery
    fences payloads that predate the invocation
    (docs/bugs/rollout-recovery-cross-turn-leak.md), and the fixtures ship with
    2026-05-18 timestamps."""
    import datetime as _dt
    import re as _re
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    path.write_text(
        _re.sub(
            r'"timestamp":"[^"]*"', f'"timestamp":"{now}Z"',
            path.read_text(encoding="utf-8"),
        ),
        encoding="utf-8",
    )


def _pre_terminal_idle_stderr(thread_id: str) -> bytes:
    """Worker stderr feed: first_codex_output + thread_id milestone, then
    silence. Mirrors the production stream up to the watchdog trigger."""
    return (
        b'{"ev":"first_codex_output","ts":1.0}\n'
        + json.dumps({"ev": "thread_id", "ts": 1.1, "thread_id": thread_id}).encode()
        + b"\n"
    )


def _wedged_proc(thread_id: str) -> "_FakeWorkerProc":
    proc = _FakeWorkerProc(stdout=b"", stderr=_pre_terminal_idle_stderr(thread_id))
    proc._exit.clear()
    return proc


def _wire_job_eof(proc: "_FakeWorkerProc", fake_job: "_FakeJob") -> None:
    real_close = fake_job.close

    def _close_and_eof():
        proc.stdout.close()
        proc.stderr.close()
        proc._exit.set()
        real_close()
    fake_job.close = _close_and_eof  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_rollout_recovery_converts_pre_terminal_idle_to_success(
    tmp_path, monkeypatch,
):
    """Default `return_all_messages=False` recovered-success path: codex
    rollout has task_complete but stdout never delivered turn.completed.
    Dispatcher returns success outcome with recovered agent_message."""
    tid = "019e38de-0000-7000-aaaa-recoveredcomp"
    codex_home = tmp_path / "codex_home"
    rollout = _install_fake_rollout(codex_home, "complete.jsonl", thread_id=tid)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("PRISM_ROLLOUT_RECOVERY", raising=False)

    proc = _wedged_proc(tid)
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        _restamp_now(rollout)
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=10.0, post_terminal_grace=10.0,
        pre_terminal_idle=0.2,
    )
    result = await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))
    assert result.error is None, result.error
    assert result.outcome is not None
    assert result.outcome.parse_result.session_id == tid
    assert result.outcome.returncode == 0  # forced — kill rc would re-fire classify
    assert result.outcome.parse_result.saw_terminal is False  # honest
    assert "Critical Findings" in result.outcome.parse_result.agent_messages
    assert result.diagnostics is not None
    assert result.diagnostics["recovered_from_rollout"] is True
    assert tid in result.diagnostics["rollout_path"]


@pytest.mark.asyncio
async def test_rollout_recovery_no_thread_id_falls_back_to_error(
    tmp_path, monkeypatch,
):
    """No `thread_id` milestone ⇒ codex never emitted thread.started cleanly.
    Recovery cannot locate the rollout; must surface the original
    pre_terminal_idle error."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home"))

    # stderr has first_codex_output but NO thread_id milestone.
    proc = _FakeWorkerProc(
        stdout=b"",
        stderr=b'{"ev":"first_codex_output","ts":1.0}\n',
    )
    proc._exit.clear()
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=10.0, post_terminal_grace=10.0,
        pre_terminal_idle=0.2,
    )
    result = await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))
    assert result.error is not None
    assert result.error["code"] == "pre_terminal_idle"
    assert result.diagnostics is not None
    assert "recovered_from_rollout" not in result.diagnostics


@pytest.mark.asyncio
async def test_rollout_recovery_file_missing_falls_back_to_error(
    tmp_path, monkeypatch,
):
    """thread_id captured but no rollout file on disk ⇒ pre_terminal_idle."""
    tid = "019e38de-1111-7000-aaaa-nofile00aaaa"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home"))
    # No fixture installed.

    proc = _wedged_proc(tid)
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=10.0, post_terminal_grace=10.0,
        pre_terminal_idle=0.2,
    )
    result = await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))
    assert result.error is not None
    assert result.error["code"] == "pre_terminal_idle"
    # thread_id known but the file never resolved ⇒ liveness untracked ⇒
    # raw and diagnostics keep their pre-liveness shape (full-equality pin).
    assert result.error["raw"] == _UNTRACKED_KILL_RAW_02S
    assert result.diagnostics is not None
    assert "rollout_ticks" not in result.diagnostics


@pytest.mark.asyncio
async def test_rollout_recovery_task_failed_surfaces_codex_error(
    tmp_path, monkeypatch,
):
    """rollout terminal payload is task_failed ⇒ codex_error envelope."""
    tid = "019e38de-2222-7000-aaaa-recoveredfail"
    codex_home = tmp_path / "codex_home"
    rollout = _install_fake_rollout(codex_home, "failed.jsonl", thread_id=tid)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    proc = _wedged_proc(tid)
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        _restamp_now(rollout)
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=10.0, post_terminal_grace=10.0,
        pre_terminal_idle=0.2,
    )
    result = await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))
    assert result.outcome is None
    assert result.error is not None
    assert result.error["code"] == "codex_error"
    assert "rate_limit" in result.error["raw"]
    assert result.diagnostics is not None
    assert result.diagnostics["recovered_from_rollout"] is True


@pytest.mark.asyncio
async def test_rollout_recovery_disabled_by_env(tmp_path, monkeypatch):
    """PRISM_ROLLOUT_RECOVERY=0 suppresses recovery even when fixture present."""
    tid = "019e38de-3333-7000-aaaa-disabled0000"
    codex_home = tmp_path / "codex_home"
    rollout = _install_fake_rollout(codex_home, "complete.jsonl", thread_id=tid)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("PRISM_ROLLOUT_RECOVERY", "0")

    proc = _wedged_proc(tid)
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        _restamp_now(rollout)
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=10.0, post_terminal_grace=10.0,
        pre_terminal_idle=0.2,
    )
    result = await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))
    assert result.error is not None
    assert result.error["code"] == "pre_terminal_idle"


@pytest.mark.asyncio
async def test_rollout_recovery_empty_message_falls_back_to_error(
    tmp_path, monkeypatch,
):
    """task_complete with empty last_agent_message ⇒ refuse to claim
    success; surface pre_terminal_idle. Better than empty agent_messages."""
    tid = "019e38de-4444-7000-aaaa-emptymsg0000"
    codex_home = tmp_path / "codex_home"
    rollout = _install_fake_rollout(codex_home, "empty_message.jsonl", thread_id=tid)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    proc = _wedged_proc(tid)
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        _restamp_now(rollout)
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=10.0, post_terminal_grace=10.0,
        pre_terminal_idle=0.2,
    )
    result = await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))
    assert result.error is not None
    assert result.error["code"] == "pre_terminal_idle"


def test_synthesize_recovered_outcome_raises_without_thread_id():
    """Localized invariant — caller must check thread_id presence; localize
    so `python -O` (which strips asserts) does not regress to silent bug."""
    from prism_router._rollout_recovery import Recovered
    from prism_router.dispatcher import _WorkerState, _synthesize_recovered_outcome

    state = _WorkerState(last_any_activity_ts=0.0)
    rec = Recovered(
        kind="complete", path=Path("/tmp/x"),
        agent_message="x", error=None,
    )
    with pytest.raises(RuntimeError):
        _synthesize_recovered_outcome(state, rec)


# ──────────────── rollout-liveness tick tests ────────────────
# See docs/bugs/rollout-liveness-tick.md. The watchdog refreshes its idle
# deadline on observed st_size growth of codex's rollout file, so a
# stdout-silent pure-reasoning turn survives as long as codex keeps appending;
# a stalled rollout still kills at threshold, recovery unchanged.


def _install_growing_rollout(codex_home: Path, thread_id: str) -> Path:
    dir_ = codex_home / "sessions" / "2026" / "08" / "19"
    dir_.mkdir(parents=True, exist_ok=True)
    target = dir_ / f"rollout-2026-08-19T03-41-00-{thread_id}.jsonl"
    target.write_bytes(b'{"type":"session_meta","payload":{}}\n')
    return target


def _append_reasoning(path: Path) -> None:
    """Non-terminal rollout record: liveness must come from st_size growth
    alone — recovery's bottom-up scan skips these (not event_msg)."""
    with path.open("ab") as fh:
        fh.write(b'{"type":"response_item","payload":{"type":"reasoning"}}\n')


@pytest.mark.asyncio
async def test_rollout_growth_defers_pre_terminal_idle_kill(
    tmp_path, monkeypatch,
):
    """Scenario 1 (core): stdout fully silent past several threshold periods,
    but the rollout keeps growing — the watchdog must NOT kill; the turn then
    completes normally and success diagnostics carry the growth ticks."""
    tid = "019f0000-0000-7000-aaaa-liveness0001"
    codex_home = tmp_path / "codex_home"
    rollout = _install_growing_rollout(codex_home, tid)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    proc = _wedged_proc(tid)
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=30.0, post_terminal_grace=10.0,
        pre_terminal_idle=0.6,
    )

    async def driver():
        # >3 threshold periods of stdout silence under steady rollout growth
        # (append cadence 0.05s ≪ threshold 0.6s; stat cadence scales to
        # threshold/8 = 0.075s).
        for _ in range(40):
            await asyncio.sleep(0.05)
            _append_reasoning(rollout)
        proc.stderr.extend(b'{"ev":"terminal_event_seen","ts":9.0}\n')
        await asyncio.sleep(0.01)
        proc.stdout.extend(_success_frame())
        proc._exit.set()

    asyncio.create_task(driver())
    result = await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))
    assert result.error is None, result.error
    assert result.outcome is not None
    assert result.diagnostics is not None
    assert result.diagnostics["rollout_ticks"] > 0
    assert "last_rollout_growth_age_s" in result.diagnostics


@pytest.mark.asyncio
async def test_rollout_stall_after_task_complete_kills_then_recovers(
    tmp_path, monkeypatch,
):
    """Scenario 2, B-class fingerprint: rollout grows, writes task_complete,
    then stalls — the kill fires `threshold` after the last observed growth
    and recovery converts it to a recovered success. Post-liveness, this
    envelope (recovered_from_rollout + rollout_ticks) is scheme B's trigger
    signal: codex wrote the answer, stdout stayed dead, rollout went quiet."""
    tid = "019f0000-0000-7000-aaaa-liveness0002"
    codex_home = tmp_path / "codex_home"
    rollout = _install_growing_rollout(codex_home, tid)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("PRISM_ROLLOUT_RECOVERY", raising=False)

    proc = _wedged_proc(tid)
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=30.0, post_terminal_grace=10.0,
        pre_terminal_idle=0.4,
    )

    async def driver():
        import datetime as _dt
        for _ in range(10):
            await asyncio.sleep(0.05)
            _append_reasoning(rollout)
        now = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f",
        )[:-3]
        with rollout.open("ab") as fh:
            fh.write(json.dumps({
                "timestamp": f"{now}Z", "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": "recovered final answer",
                },
            }).encode() + b"\n")
        # Total stall from here — the watchdog must fire.

    asyncio.create_task(driver())
    result = await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))
    assert result.error is None, result.error
    assert result.outcome is not None
    assert result.outcome.parse_result.agent_messages == "recovered final answer"
    assert result.outcome.parse_result.saw_terminal is False
    assert result.diagnostics is not None
    assert result.diagnostics["recovered_from_rollout"] is True
    assert result.diagnostics["rollout_ticks"] > 0
    assert result.diagnostics["last_rollout_growth_age_s"] >= 0.2


@pytest.mark.asyncio
async def test_rollout_stall_without_terminal_kills_with_stall_note(
    tmp_path, monkeypatch,
):
    """Scenario 2, plain: rollout grows then stalls with NO terminal payload —
    pre_terminal_idle error as before, raw noting the rollout stalled too."""
    tid = "019f0000-0000-7000-aaaa-liveness0003"
    codex_home = tmp_path / "codex_home"
    rollout = _install_growing_rollout(codex_home, tid)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    proc = _wedged_proc(tid)
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=30.0, post_terminal_grace=10.0,
        pre_terminal_idle=0.4,
    )

    async def driver():
        for _ in range(8):
            await asyncio.sleep(0.05)
            _append_reasoning(rollout)

    asyncio.create_task(driver())
    result = await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))
    assert result.outcome is None
    assert result.error is not None
    assert result.error["code"] == "pre_terminal_idle"
    assert "rollout file stalled too" in result.error["raw"]
    assert result.diagnostics is not None
    assert result.diagnostics["rollout_ticks"] > 0
    assert result.diagnostics["last_rollout_growth_age_s"] >= 0.2


@pytest.mark.asyncio
async def test_rollout_created_after_arming_still_defers_kill(
    tmp_path, monkeypatch,
):
    """Deferred creation: codex materializes the rollout on its first flush,
    which can land after first_codex_output. The probe must keep re-globbing
    at poll cadence, pick the file up late, and growth must then defer the
    kill exactly as if the file had existed at arming."""
    tid = "019f0000-0000-7000-aaaa-liveness0004"
    codex_home = tmp_path / "codex_home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    proc = _wedged_proc(tid)
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=30.0, post_terminal_grace=10.0,
        pre_terminal_idle=0.6,
    )

    async def driver():
        await asyncio.sleep(0.2)  # file appears well after arming
        rollout = _install_growing_rollout(codex_home, tid)
        for _ in range(30):
            await asyncio.sleep(0.05)
            _append_reasoning(rollout)
        proc.stderr.extend(b'{"ev":"terminal_event_seen","ts":9.0}\n')
        await asyncio.sleep(0.01)
        proc.stdout.extend(_success_frame())
        proc._exit.set()

    asyncio.create_task(driver())
    result = await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))
    assert result.error is None, result.error
    assert result.outcome is not None
    assert result.diagnostics is not None
    assert result.diagnostics["rollout_ticks"] > 0


@pytest.mark.asyncio
async def test_late_single_append_extends_deadline_then_kills(
    tmp_path, monkeypatch,
):
    """A single rollout append late in the silence window must be credited
    (deadline extends by a full threshold from its observation) — including
    when it lands inside the final cadence window, where the deadline's
    final-beat probe picks it up. The stall then kills with the stall note."""
    tid = "019f0000-0000-7000-aaaa-liveness0005"
    codex_home = tmp_path / "codex_home"
    rollout = _install_growing_rollout(codex_home, tid)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    proc = _wedged_proc(tid)
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=30.0, post_terminal_grace=10.0,
        pre_terminal_idle=0.45,
    )

    async def driver():
        await asyncio.sleep(0.3)  # deep into the silence window
        _append_reasoning(rollout)

    asyncio.create_task(driver())
    start = time.monotonic()
    result = await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))
    killed_after = time.monotonic() - start
    assert result.error is not None
    assert result.error["code"] == "pre_terminal_idle"
    # The late append pushed the kill past one bare threshold.
    assert killed_after >= 0.6, killed_after
    assert "rollout file stalled too" in result.error["raw"]
    assert result.diagnostics is not None
    assert result.diagnostics["rollout_ticks"] == 1
    assert "rollout_observation_stale" not in result.diagnostics


@pytest.mark.asyncio
async def test_probe_failure_after_growth_reports_observation_lost(
    tmp_path, monkeypatch,
):
    """Probes going dark after a growth tick must NOT read as a confirmed
    rollout stall: the kill still fires (no liveness evidence = stdout-only
    behavior), but raw reports the lost observation instead of a stall and
    diagnostics carry rollout_observation_stale — a B-trigger audit must
    skip such kills."""
    import prism_router.dispatcher as dispatcher_mod

    tid = "019f0000-0000-7000-aaaa-liveness0006"
    codex_home = tmp_path / "codex_home"
    rollout = _install_growing_rollout(codex_home, tid)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    real_probe = dispatcher_mod._probe_rollout
    calls = {"n": 0}

    def flaky_probe(thread_id, known_path):
        calls["n"] += 1
        if calls["n"] > 2:
            return None  # simulated filesystem failure from the 3rd probe on
        return real_probe(thread_id, known_path)

    monkeypatch.setattr(dispatcher_mod, "_probe_rollout", flaky_probe)

    proc = _wedged_proc(tid)
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=30.0, post_terminal_grace=10.0,
        pre_terminal_idle=0.3,
    )

    async def driver():
        # Appends land between probes #1 and #2 so growth is likely observed
        # once (the asserts hold either way), then the file KEEPS growing
        # while every later probe fails — growth becomes unobservable.
        await asyncio.sleep(0.02)
        _append_reasoning(rollout)
        _append_reasoning(rollout)
        while True:
            await asyncio.sleep(0.05)
            _append_reasoning(rollout)

    driver_task = asyncio.create_task(driver())
    try:
        result = await d.invoke(
            backend_name="codex", prompt="p", cfg=_cfg(tmp_path),
        )
    finally:
        driver_task.cancel()
    assert result.error is not None
    assert result.error["code"] == "pre_terminal_idle"
    assert "rollout observation lost" in result.error["raw"]
    assert "stalled too" not in result.error["raw"]
    assert result.diagnostics is not None
    assert result.diagnostics["rollout_observation_stale"] is True


@pytest.mark.asyncio
async def test_hung_final_beat_probe_marks_observation_stale(
    tmp_path, monkeypatch,
):
    """A probe that HANGS (never completes) across the final beat leaves
    misses at 0 and the last successful stat recent — only the
    unresolved-probe signal can mark the channel dark. Drives the watchdog
    directly with a pre-expired state so the age bound stays fresh at the
    verdict and the flag is the deciding signal."""
    import threading

    import prism_router.dispatcher as dispatcher_mod

    release = threading.Event()

    def hung_probe(_thread_id, _known_path):
        release.wait(timeout=2.0)
        return None

    monkeypatch.setattr(dispatcher_mod, "_probe_rollout", hung_probe)
    now = time.monotonic()
    state = dispatcher_mod._WorkerState(
        last_any_activity_ts=now,
        last_stdout_activity_ts=now - 1.0,
        codex_thread_id="hung-final-probe",
        rollout_path=tmp_path / "rollout.jsonl",
        last_rollout_size=1,
        rollout_baseline_ts=now - 1.0,
        last_rollout_growth_ts=now - 0.5,  # exactly threshold → expires now
        rollout_ticks=1,
        last_probe_ok_ts=now,
    )
    kill_event = asyncio.Event()
    kill_reason: list[str] = []
    try:
        await dispatcher_mod._arm_pre_terminal_idle(
            0.5, state, kill_event, kill_reason,
        )
    finally:
        release.set()

    assert kill_reason == ["pre_terminal_idle"]
    assert state.rollout_observation_stale is True


@pytest.mark.asyncio
async def test_thread_id_arriving_after_arming_still_engages_liveness(
    tmp_path, monkeypatch,
):
    """`first_codex_output` and `thread_id` are separate stderr writes; a
    chunk gap between them must not strand the watchdog in a full-deadline
    sleep with liveness disengaged. The loop wakes at the poll floor while
    the thread_id is unknown and engages the probe on arrival."""
    tid = "019f0000-0000-7000-aaaa-liveness0007"
    codex_home = tmp_path / "codex_home"
    rollout = _install_growing_rollout(codex_home, tid)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    # stderr starts with ONLY first_codex_output — thread_id arrives later.
    proc = _FakeWorkerProc(
        stdout=b"", stderr=b'{"ev":"first_codex_output","ts":1.0}\n',
    )
    proc._exit.clear()
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=30.0, post_terminal_grace=10.0,
        pre_terminal_idle=0.4,
    )

    async def driver():
        await asyncio.sleep(0.15)  # inside what would be the long sleep
        proc.stderr.extend(
            json.dumps({"ev": "thread_id", "ts": 1.1, "thread_id": tid}).encode()
            + b"\n",
        )
        for _ in range(25):
            await asyncio.sleep(0.05)
            _append_reasoning(rollout)
        proc.stderr.extend(b'{"ev":"terminal_event_seen","ts":9.0}\n')
        await asyncio.sleep(0.01)
        proc.stdout.extend(_success_frame())
        proc._exit.set()

    asyncio.create_task(driver())
    result = await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))
    assert result.error is None, result.error
    assert result.outcome is not None
    assert result.diagnostics is not None
    assert result.diagnostics["rollout_ticks"] > 0


def test_probe_and_apply_rollout_growth(tmp_path, monkeypatch):
    """Probe: unresolved path → None; known path skips the glob; stat failure
    → None. Apply: first stat = baseline (no tick); growth = tick; external
    shrink rebases without a tick; None = no-op."""
    from prism_router.dispatcher import (
        _WorkerState,
        _apply_rollout_probe,
        _probe_rollout,
    )

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home"))
    state = _WorkerState(last_any_activity_ts=0.0)
    assert _probe_rollout("019f-none", None) is None  # no sessions dir

    f = tmp_path / "rollout-x.jsonl"
    f.write_bytes(b"abc")
    r = _probe_rollout("ignored", f)
    assert r == (f, 3)
    _apply_rollout_probe(state, r, 1.0)
    assert state.rollout_path == f and state.last_rollout_size == 3
    assert state.rollout_baseline_ts == 1.0
    assert state.rollout_ticks == 0 and state.last_rollout_growth_ts is None

    f.write_bytes(b"abcdef")
    _apply_rollout_probe(state, _probe_rollout("ignored", f), 2.0)
    assert state.rollout_ticks == 1 and state.last_rollout_growth_ts == 2.0
    assert state.rollout_baseline_ts == 1.0  # baseline is first-stat only

    f.write_bytes(b"a")
    _apply_rollout_probe(state, _probe_rollout("ignored", f), 3.0)
    assert state.rollout_ticks == 1 and state.last_rollout_size == 1

    f.write_bytes(b"ab")
    _apply_rollout_probe(state, _probe_rollout("ignored", f), 4.0)
    assert state.rollout_ticks == 2

    f.unlink()
    assert _probe_rollout("ignored", f) is None  # stat OSError → None
    _apply_rollout_probe(state, None, 5.0)
    assert state.rollout_ticks == 2 and state.last_rollout_growth_ts == 4.0


def test_env_float_valid_returns_parsed():
    """Sanity: well-formed env value parses to float."""
    import os
    os.environ["__TEST_PRISM_ENV_FLOAT__"] = "3600.5"
    try:
        assert _env_float("__TEST_PRISM_ENV_FLOAT__", 1200.0) == 3600.5
    finally:
        del os.environ["__TEST_PRISM_ENV_FLOAT__"]


def test_env_float_invalid_falls_back_to_default(caplog):
    """Malformed env must not crash module import — warn and use default."""
    import logging
    import os
    os.environ["__TEST_PRISM_ENV_FLOAT__"] = "not-a-number"
    try:
        with caplog.at_level(logging.WARNING, logger="prism_router.dispatcher"):
            v = _env_float("__TEST_PRISM_ENV_FLOAT__", 1200.0)
        assert v == 1200.0
        assert any("invalid" in r.message.lower() for r in caplog.records)
    finally:
        del os.environ["__TEST_PRISM_ENV_FLOAT__"]


def test_env_float_missing_uses_default():
    assert _env_float("__NO_SUCH_PRISM_ENV__", 42.0) == 42.0


def test_env_float_rejects_nan_and_inf_and_negative(caplog):
    """NaN would silently disable the watchdog (nan > 0 is False),
    inf would create a never-firing watchdog, negative is a semantic
    mismatch (0 is the documented 'disabled' sentinel). All three
    must fall back to default with a warning."""
    import logging
    import os
    for bad in ("nan", "inf", "-1"):
        os.environ["__TEST_PRISM_ENV_FLOAT__"] = bad
        try:
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="prism_router.dispatcher"):
                v = _env_float("__TEST_PRISM_ENV_FLOAT__", 1200.0)
            assert v == 1200.0, (bad, v)
            assert any(
                "out-of-range" in r.message.lower() for r in caplog.records
            ), (bad, [r.message for r in caplog.records])
        finally:
            del os.environ["__TEST_PRISM_ENV_FLOAT__"]


@pytest.mark.asyncio
async def test_dispatch_disk_unavailable(tmp_path, monkeypatch):
    """temp dir probe failure → disk_unavailable envelope, worker never spawned."""
    spawn_called = {"n": 0}

    async def spawn(env):
        spawn_called["n"] += 1
        return _FakeWorkerProc(stdout=b"")

    def assign(pid):
        return _FakeJob()

    # Make Path.mkdir raise.
    real_mkdir = Path.mkdir

    def _failing_mkdir(self, *a, **kw):
        if str(self).find("prism-router-") >= 0:
            raise OSError("simulated disk full")
        return real_mkdir(self, *a, **kw)
    monkeypatch.setattr(Path, "mkdir", _failing_mkdir)

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
    )
    result = await d.invoke(backend_name="codex", prompt="hi", cfg=_cfg(tmp_path))
    assert result.outcome is None
    assert result.error is not None
    assert result.error["code"] == "disk_unavailable"
    assert spawn_called["n"] == 0


# ───── liveness progress egress (docs/bugs/host-idle-progress-egress.md) ─────
#
# Dispatcher-side gates. The dispatcher is an OBSERVER host: it feeds the two
# evidence sites and owns no egress lifecycle.


def _liveness_proc(stderr: bytes = b'{"ev":"first_codex_output","ts":1.0}\n'):
    proc = _FakeWorkerProc(stdout=b"", stderr=stderr)
    proc._exit.clear()
    return proc


@pytest.mark.asyncio
async def test_stdout_tick_is_the_only_forwarded_stderr_milestone(tmp_path):
    """Claim: `stdout_tick` is forwarded; `first_codex_output` (emitted BEFORE
    transient classification) and `terminal_event_seen` are not."""
    seen: list[str] = []
    proc = _liveness_proc()
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    async def driver():
        for ts in (2.0, 3.0):
            await asyncio.sleep(0.05)
            proc.stderr.extend(
                json.dumps({"ev": "stdout_tick", "ts": ts}).encode() + b"\n",
            )
        await asyncio.sleep(0.05)
        proc.stderr.extend(b'{"ev":"terminal_event_seen","ts":4.0}\n')
        await asyncio.sleep(0.05)
        proc.stdout.extend(_success_frame())
        proc._exit.set()

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=10.0, post_terminal_grace=10.0, pre_terminal_idle=5.0,
        on_liveness=seen.append,
    )
    asyncio.create_task(driver())
    result = await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))

    assert result.error is None, result.error
    # Both non-forwarded milestones were nonetheless consumed by the drain.
    assert result.diagnostics["saw_terminal_event"] is True
    assert seen == ["stdout", "stdout"]


@pytest.mark.asyncio
async def test_the_drain_stays_prompt_under_a_burst_of_evidence(tmp_path):
    """The stdout milestone branch must not accumulate enough per-tick delay
    for the concurrently available success frame to win and cancel the drain
    before the already-buffered chunk has been consumed.

    Narrow by design: this proves absence of a material cumulative stall, not
    absence of every await — a costless `sleep(0)` preserves the ordering, and
    an await AFTER the inner parsing loop also passes. A single-tick scenario
    cannot expose the ordering at all, since the watchdog anchor is assigned
    before the emit."""
    ticks = 50
    seen: list[str] = []
    stderr = b'{"ev":"first_codex_output","ts":1.0}\n' + b"".join(
        json.dumps({"ev": "stdout_tick", "ts": float(i)}).encode() + b"\n"
        for i in range(ticks)
    )
    proc = _FakeWorkerProc(stdout=_success_frame(), stderr=stderr)
    fake_job = _FakeJob()

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=30.0, post_terminal_grace=10.0, pre_terminal_idle=10.0,
        on_liveness=seen.append,
    )
    result = await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))

    assert result.error is None, result.error
    # Deterministic ordering check — no wall clock, so host load cannot fail
    # it. Measured: a per-tick await of 1 ms already trips this, while a
    # costless `sleep(0)` does not, which is right (a bare yield is not a
    # stall). The tick count is load-bearing: `_read_framed_stdout` reads its
    # 23-byte header ONE BYTE at a time through `wait_for`, so it needs ~30
    # scheduler turns to win the race — at 20 ticks a 10 ms per-tick await
    # still passes; at 50 a 1 ms one fails.
    assert len(seen) == ticks, "every tick must have been consumed"


@pytest.mark.asyncio
async def test_observed_rollout_growth_drives_on_liveness(tmp_path, monkeypatch):
    """Claim: the rollout evidence site fires once per CREDITED growth tick —
    the baseline stat (which earns no liveness) must not forward."""
    seen: list[str] = []
    tid = "019f0000-0000-7000-aaaa-egress000001"
    codex_home = tmp_path / "codex_home"
    rollout = _install_growing_rollout(codex_home, tid)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    proc = _wedged_proc(tid)
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    async def driver():
        for _ in range(20):
            await asyncio.sleep(0.05)
            _append_reasoning(rollout)
        proc.stderr.extend(b'{"ev":"terminal_event_seen","ts":9.0}\n')
        await asyncio.sleep(0.01)
        proc.stdout.extend(_success_frame())
        proc._exit.set()

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=30.0, post_terminal_grace=10.0, pre_terminal_idle=0.6,
        on_liveness=seen.append,
    )
    asyncio.create_task(driver())
    result = await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))

    assert result.error is None, result.error
    ticks = result.diagnostics["rollout_ticks"]
    assert ticks > 0
    assert set(seen) == {"rollout"}
    assert seen.count("rollout") == ticks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend_name", "pre_terminal_idle"),
    [("gemini", 5.0), ("codex", 0.0)],
    ids=["gemini", "codex-watchdog-disabled"],
)
async def test_stdout_tier_forwards_with_the_idle_watchdog_disabled(
    tmp_path, backend_name: str, pre_terminal_idle: float,
):
    """Claim: the stdout_tick branch is backend-agnostic and runs even when the
    idle watchdog is off (gemini, or PRISM_PRE_TERMINAL_IDLE=0)."""
    seen: list[str] = []
    proc = _liveness_proc()
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    async def driver():
        await asyncio.sleep(0.05)
        proc.stderr.extend(b'{"ev":"stdout_tick","ts":2.0}\n')
        await asyncio.sleep(0.05)
        proc.stdout.extend(_success_frame())
        proc._exit.set()

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=10.0, post_terminal_grace=10.0,
        pre_terminal_idle=pre_terminal_idle,
        on_liveness=seen.append,
    )
    asyncio.create_task(driver())
    result = await d.invoke(
        backend_name=backend_name, prompt="p", cfg=_cfg(tmp_path),
    )

    assert result.error is None, result.error
    assert seen == ["stdout"]
    # No rollout tier without an armed watchdog.
    assert "rollout_ticks" not in result.diagnostics


async def _kill_envelope(tmp_path, on_liveness) -> DispatchResult:
    """One stdout_tick, then total silence past a 0.2 s threshold."""
    proc = _liveness_proc(
        b'{"ev":"first_codex_output","ts":1.0}\n'
        b'{"ev":"stdout_tick","ts":1.1}\n',
    )
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=10.0, post_terminal_grace=10.0, pre_terminal_idle=0.2,
        on_liveness=on_liveness,
    )
    return await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))


# Diagnostics whose value is a measured duration: compared for closeness
# rather than equality, so an observer cannot smuggle a change through them.
_TIMING_DIAG_KEYS = frozenset({
    "max_stdout_silence_s", "rollout_watched_s", "last_rollout_growth_age_s",
})


def _envelope_fingerprint(result: DispatchResult) -> tuple:
    """Everything caller-visible except measured durations — including the
    diagnostics KEY SET, so an added or dropped key is caught too."""
    diag = result.diagnostics
    return (
        result.error, result.outcome,
        tuple(sorted(diag)),
        tuple(sorted(
            (k, repr(v)) for k, v in diag.items() if k not in _TIMING_DIAG_KEYS
        )),
    )


@pytest.mark.asyncio
async def test_on_liveness_none_and_an_active_observer_yield_the_same_envelope(
    tmp_path,
):
    """Claim: `on_liveness=None` skips the sites entirely, and an ACTIVE
    observer still changes nothing the caller can see."""
    seen: list[str] = []
    silent = await _kill_envelope(tmp_path, None)
    observed = await _kill_envelope(tmp_path, seen.append)

    assert seen == ["stdout"], "the observer must actually have fired"
    assert _envelope_fingerprint(silent) == _envelope_fingerprint(observed)
    # The measured-duration keys are excluded from the fingerprint, so pin them
    # by closeness — otherwise an observer could move one freely.
    for key in _TIMING_DIAG_KEYS & set(silent.diagnostics):
        a, b = silent.diagnostics[key], observed.diagnostics[key]
        assert (a is None) == (b is None), key
        if a is not None:
            assert abs(a - b) < 0.2, f"{key}: {a} vs {b}"
    assert silent.error["raw"] == _UNTRACKED_KILL_RAW_02S


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [RuntimeError, asyncio.CancelledError], ids=["exc", "cancel"])
async def test_raising_observer_at_the_stdout_site_cannot_stop_the_drain(
    tmp_path, exc: type[BaseException],
):
    """G1: the callback raises on EVERY stdout tick; stderr must keep draining
    (later milestones still land) and the watchdog must still kill.

    `CancelledError` is the dangerous case — it is not an `Exception`, so a
    containment helper that only catches `Exception` lets it end the drain.
    """
    calls: list[str] = []

    def boom(source: str) -> None:
        calls.append(source)
        raise exc("observer exploded")

    proc = _liveness_proc()
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    async def driver():
        for ts in (2.0, 3.0):
            await asyncio.sleep(0.05)
            proc.stderr.extend(
                json.dumps({"ev": "stdout_tick", "ts": ts}).encode() + b"\n",
            )

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=10.0, post_terminal_grace=10.0, pre_terminal_idle=0.3,
        on_liveness=boom,
    )
    asyncio.create_task(driver())
    result = await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))

    assert calls == ["stdout", "stdout"]
    # The second tick was parsed AFTER the first raise ⇒ the drain survived.
    assert result.diagnostics["milestones_seen"] == [
        "first_codex_output", "stdout_tick", "stdout_tick",
    ]
    assert result.error["code"] == "pre_terminal_idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [RuntimeError, asyncio.CancelledError], ids=["exc", "cancel"])
async def test_raising_observer_at_the_rollout_site_cannot_stop_the_watchdog(
    tmp_path, monkeypatch, exc: type[BaseException],
):
    """G1: the callback raises on every credited growth tick; the watchdog must
    keep polling, keep its bookkeeping, and still kill on the stall.

    An escaping `CancelledError` would leave the watchdog through its OWN
    cancellation handler — no kill_event, no kill — and the call would hang to
    overall_timeout instead. `error["code"]` is what separates the two.
    """
    calls: list[str] = []

    def boom(source: str) -> None:
        calls.append(source)
        raise exc("observer exploded")

    tid = "019f0000-0000-7000-aaaa-egress000002"
    codex_home = tmp_path / "codex_home"
    rollout = _install_growing_rollout(codex_home, tid)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("PRISM_ROLLOUT_RECOVERY", "0")

    proc = _wedged_proc(tid)
    fake_job = _FakeJob()
    _wire_job_eof(proc, fake_job)

    async def spawn(env):
        return proc

    def assign(pid):
        return fake_job

    async def driver():
        for _ in range(6):
            await asyncio.sleep(0.05)
            _append_reasoning(rollout)
        # Total stall from here — the watchdog must still fire.

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
        overall_timeout=30.0, post_terminal_grace=10.0, pre_terminal_idle=0.4,
        on_liveness=boom,
    )
    asyncio.create_task(driver())
    result = await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))

    assert calls and set(calls) == {"rollout"}
    assert result.error["code"] == "pre_terminal_idle"
    # Bookkeeping survived every raise: growth ticks were still credited and
    # the stall verdict still reached the raw.
    assert result.diagnostics["rollout_ticks"] == len(calls)
    assert "rollout file stalled too" in result.error["raw"]


@pytest.mark.asyncio
async def test_kill_timing_is_not_shifted_by_the_liveness_observer(tmp_path):
    """Invariant 1: the observer adds no awaits to the kill path. A sink doing
    synchronous work, a raising callback, and a permanently hung sink must all
    leave the kill inside the pre-egress poll-floor tolerance."""
    import anyio

    from prism_router._liveness import LivenessEgress

    # poll_floor is the watchdog's own wake cadence at this threshold, and the
    # tolerance invariant 1 is stated in. Bounds are set from measurement, not
    # guessed: observed deviations are ~0.03 s, so `slack` is a sanity margin,
    # while the delta-vs-baseline check is what actually encodes the claim.
    threshold, poll_floor, slack = 0.5, 0.125, 0.3

    async def timed(on_liveness) -> float:
        proc = _liveness_proc(
            b'{"ev":"first_codex_output","ts":1.0}\n'
            b'{"ev":"stdout_tick","ts":1.1}\n',
        )
        fake_job = _FakeJob()
        _wire_job_eof(proc, fake_job)

        async def spawn(env):
            return proc

        def assign(pid):
            return fake_job

        d = Dispatcher(
            worker_spawner=spawn, job_assigner=assign, temp_root=tmp_path,
            overall_timeout=30.0, post_terminal_grace=10.0,
            pre_terminal_idle=threshold, on_liveness=on_liveness,
        )
        t0 = time.monotonic()
        result = await d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path))
        elapsed = time.monotonic() - t0
        assert result.error["code"] == "pre_terminal_idle"
        return elapsed

    def sync_work(source: str) -> None:
        sum(range(50_000))

    def raising(source: str) -> None:
        raise RuntimeError("observer exploded")

    async def hung_sink(progress: float, message: str) -> None:
        await asyncio.sleep(300)

    elapsed = {
        "none": await timed(None),
        "sync_work": await timed(sync_work),
        "raising": await timed(raising),
    }
    async with anyio.create_task_group() as tg:
        egress = LivenessEgress(hung_sink, spawn=tg.start_soon, min_interval=0.0)
        elapsed["hung_sink"] = await timed(egress.note)
        assert egress.attempts == 1, "the hung send must park the single-flight slot"
        egress.abort()

    baseline = elapsed["none"]
    for variant, value in elapsed.items():
        assert value >= threshold, f"{variant} killed EARLY: {value:.3f}s"
        assert value <= threshold + poll_floor + slack, (
            f"{variant} killed LATE: {value:.3f}s"
        )
        # The invariant itself, and NOT implied by the bounds above: those
        # alone would admit a spread of poll_floor + slack.
        assert abs(value - baseline) <= poll_floor, (
            f"{variant} shifted the kill by {value - baseline:+.3f}s "
            f"vs the observer-free baseline {baseline:.3f}s"
        )


def test_emit_liveness_contains_a_raising_callback(caplog):
    """The containment helper itself: None is a no-op, a raise is logged and
    swallowed AT the call site."""
    from prism_router.dispatcher import _emit_liveness

    assert _emit_liveness(None, "stdout") is None

    def boom(source: str) -> None:
        raise RuntimeError("observer exploded")

    with caplog.at_level("WARNING"):
        assert _emit_liveness(boom, "rollout") is None
    assert "on_liveness(rollout) raised" in caplog.text

    def cancel(source: str) -> None:
        raise asyncio.CancelledError()

    # Not an `Exception`; a sync callback has no suspension point, so this can
    # only be callback-originated and must be contained too.
    with caplog.at_level("WARNING"):
        assert _emit_liveness(cancel, "stdout") is None
    assert "raised CancelledError" in caplog.text


@pytest.mark.asyncio
async def test_on_child_hears_the_milestone_then_the_parsed_frame(tmp_path):
    """Lineage hops: `Dispatcher(on_child=…)` → `_run_worker` → `_drain_stderr`
    (milestone), then the frame parse. Distinct ids tell the sources apart; in
    production both carry the same id."""
    seen: list[str] = []
    proc = _FakeWorkerProc(
        stdout=_success_frame(),
        stderr=json.dumps({"ev": "thread_id", "ts": 1.0, "thread_id": "from-milestone"}).encode()
        + b"\n",
    )

    async def spawn(env):
        return proc

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=lambda pid: _FakeJob(), temp_root=tmp_path,
        on_child=seen.append,
    )
    result = await d.invoke(backend_name="gemini", prompt="p", cfg=_cfg(tmp_path))
    assert result.error is None
    assert seen == ["from-milestone", "sid-1"]


@pytest.mark.asyncio
async def test_a_parsed_frame_reports_its_id_before_any_cancellable_wait(tmp_path):
    """Missed milestone: the frame is where the id is learned. It must be
    reported before reaping the worker, which a cancel can interrupt."""
    seen: list[str] = []
    proc = _FakeWorkerProc(stdout=_success_frame())
    proc._exit.clear()  # the worker never exits: reaping blocks

    async def spawn(env):
        return proc

    d = Dispatcher(
        worker_spawner=spawn, job_assigner=lambda pid: _FakeJob(), temp_root=tmp_path,
        on_child=seen.append,
    )
    task = asyncio.create_task(d.invoke(backend_name="codex", prompt="p", cfg=_cfg(tmp_path)))
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == ["sid-1"]


@pytest.mark.asyncio
async def test_on_child_fires_once_and_a_raising_observer_never_stops_the_drain(caplog):
    from types import SimpleNamespace

    from prism_router.dispatcher import _WorkerState, _drain_stderr

    stderr = _FakeReader(
        b'{"ev":"thread_id","thread_id":"A"}\n'
        b'{"ev":"thread_id","thread_id":"B"}\n'
        b'{"ev":"sidecar_published","path":"x"}\n'
    )
    stderr.close()
    state = _WorkerState(last_any_activity_ts=0.0)
    seen: list[str] = []

    def observer(sid: str) -> None:
        seen.append(sid)
        raise RuntimeError("observer exploded")

    with caplog.at_level("WARNING"):
        await _drain_stderr(
            SimpleNamespace(stderr=stderr), state, asyncio.Event(), 10.0, 0.0, [],
            None, observer,
        )
    assert seen == ["A"]
    assert state.codex_thread_id == "A"
    assert state.sidecar_published is not None  # later milestones still handled
    assert "on_child(A) raised" in caplog.text
