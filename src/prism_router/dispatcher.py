"""Per-call worker dispatcher (main MCP process).

The main process spawns one ephemeral Python worker per tool call, wraps it
in a Windows Job Object (KILL_ON_JOB_CLOSE), reads a single Content-Length
framed RunOutcome from worker stdout, and owns all wall-clock watchdogs.
On any kill the main process calls `CloseHandle(job)` — the OS atomically
terminates worker + codex.exe + every grandchild.

Hard invariants (codex round 5 SHIP):
1. Main process never blocks on a codex pipe.
2. Main owns the Job Object handle; CloseHandle(job) is the universal kill.
3. No awaits between Popen and AssignProcessToJobObject (sync critical section).
4. All pipe reads inside asyncio.wait([..., kill_event], FIRST_COMPLETED).
5. Cancel re-raises CancelledError; all other kills synthesize envelope.
6. AssignProcessToJobObject failure is fail-closed: terminate worker, return
   spawn_failed; stdin is never written to a worker outside a Job.

Watchdogs in main only:
- overall_timeout (7200 s default) — global ceiling. Bounds chatty hangs
  (codex keeps emitting on stdout but never delivers a terminal event).
- pre_terminal_idle (1200 s = 20 min default) — armed on `first_codex_output`,
  refreshed by non-transient stdout activity (`stdout_tick`; runner
  withholds the tick for `Reconnecting... N/M` chatter, so an upstream retry
  storm cannot mask a stalled turn — docs/bugs/codex-transient-retry-storm.md)
  and by observed st_size growth of codex's own rollout file (stat-only
  rollout-liveness tick: under a proxy that returns empty reasoning summaries,
  a legitimate pure-reasoning turn is stdout-silent for its WHOLE duration
  while still appending reasoning to the rollout; retry storms are not
  persisted to the rollout, so the storm guard holds —
  docs/bugs/rollout-liveness-tick.md), disarmed by `terminal_event_seen`.
  Catches the upstream codex-CLI bug where
  task_complete is written to the local rollout but `turn.completed`
  never reaches stdout and the process refuses to exit. Empirical data
  (1210 historical codex MCP calls, 35% missing-result rate) showed the
  former "NO pre-terminal idle watchdog" stance left ~half the hangs
  waiting on the 2h overall_timeout. Env override
  `PRISM_PRE_TERMINAL_IDLE` for workloads with known long silent
  reasoning (set to 2700/3600 if false-kills occur). See
  `docs/bugs/pre-terminal-idle-watchdog.md` for the data and reasoning.
- post_terminal_grace (120 s) — armed by stderr `terminal_event_seen` milestone.
- MCP cancel — propagates -32001.

Worker has ZERO wall-clock timers. ONE event-triggered coroutine
(`codex_exit_drain=5 s`) in runner.run, anchored to codex process exit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from . import _job_object as _jo
from . import _rollout_recovery
from .backend import SpawnConfig
from .errors import ErrorEnvelope, make_error
from .parser import ParseResult
from .runner import RunOutcome

_LOG = logging.getLogger("prism_router.dispatcher")

# Defaults
OVERALL_TIMEOUT_S = 7200.0
POST_TERMINAL_GRACE_S = 120.0


def _env_float(name: str, default: float) -> float:
    """Parse `name` from os.environ as a non-negative finite float; fall back
    to default on missing or malformed, with a single WARNING log line.

    Rejects:
    - missing or empty (silent — use default)
    - unparseable strings (warn — use default)
    - NaN (would silently disable the watchdog since `nan > 0` is False)
    - infinity (would create a watchdog that never fires)
    - negative values (semantic mismatch — 0 is the "disabled" sentinel)
    """
    import math  # local import keeps module-load surface minimal
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        _LOG.warning(
            "ignoring invalid %s=%r (expected float); using default %.1f",
            name, raw, default,
        )
        return default
    if not math.isfinite(value) or value < 0:
        _LOG.warning(
            "ignoring out-of-range %s=%r (expected finite, >=0); using default %.1f",
            name, raw, default,
        )
        return default
    return value


# Pre-terminal stdout-idle watchdog. Armed on `first_codex_output`,
# refreshed by `stdout_tick` and by rollout-file growth, disarmed by
# `terminal_event_seen`. Codex-only: the upstream bug is specific to the
# codex CLI's stdout enum behavior; this watchdog is gated off for other
# backends in `_invoke_inner`. 1200s (20 min) was chosen from empirical data
# on 1210 historical codex MCP calls — see
# docs/bugs/pre-terminal-idle-watchdog.md.
PRE_TERMINAL_IDLE_S = _env_float("PRISM_PRE_TERMINAL_IDLE", 1200.0)
# Rollout-liveness polling inside the pre_terminal_idle watchdog
# (docs/bugs/rollout-liveness-tick.md). The cap is the production cadence;
# sub-second test thresholds scale down via threshold/8. All filesystem work
# (path glob + stat) runs in a single never-awaited worker thread per call.
ROLLOUT_POLL_CAP_S = 30.0
SPAWN_ASSIGN_TIMEOUT_S = 5.0
REQUEST_WRITE_TIMEOUT_S = 30.0
PROTOCOL_READ_TIMEOUT_S = 10.0
BODY_IDLE_TIMEOUT_S = 10.0
MAX_CONCURRENT = 4
STDIN_PREFLIGHT_CAP = 4 * 1024 * 1024
STDOUT_FRAME_CAP = 4 * 1024 * 1024
HEADER_CAP = 256
STDERR_MAX_LINE = 16 * 1024
STDERR_TAIL_BYTES = 16 * 1024
TEMP_PREFIX = "prism-router-"
JANITOR_AGE_S = 3600.0

# Env allowlist: codex/openai/proxy/cert/process essentials.
_ENV_ALLOWLIST = frozenset({
    "CODEX_HOME", "CODEX_LOG_LEVEL",
    "PATH", "USERPROFILE", "USERNAME", "HOME",
    "TEMP", "TMP", "APPDATA", "LOCALAPPDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA",
    "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "WINDIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS", "CURL_CA_BUNDLE",
})
_ENV_PREFIXES = ("OPENAI_", "AZURE_", "ANTHROPIC_")

# Process-lifetime guards
_semaphore: asyncio.Semaphore | None = None
_janitor_started = False
_janitor_task: asyncio.Task | None = None


WorkerSpawner = Callable[..., Awaitable[asyncio.subprocess.Process]]
JobAssigner = Callable[[int], _jo.JobHandle]
# Observer fed at the two liveness-evidence sites. Plain sync callable in,
# nothing out — the dispatcher owns no egress lifecycle and imports no MCP
# machinery. docs/bugs/host-idle-progress-egress.md
LivenessCallback = Callable[[str], None]
# Observer fed the child session id when the `thread_id` milestone lands and
# again when the result frame is parsed (the frame covers a missed milestone),
# so one dispatch may report the same id twice; lineage writes at most one
# `child` row per call (docs/lineage.md).
ChildCallback = Callable[[str], None]


def _emit_liveness(cb: LivenessCallback | None, source: str) -> None:
    """Feed the liveness observer with the failure contained AT the call site.

    The egress promises not to raise; the dispatcher does not trust that
    promise. An escaping callback exception would kill `_drain_stderr` or the
    pre_terminal_idle watchdog — an observer must never do that.
    """
    _notify(cb, "on_liveness", source)


def _notify(cb: Callable[[str], None] | None, name: str, arg: str) -> None:
    if cb is None:
        return
    try:
        cb(arg)
    except asyncio.CancelledError:
        # `cb` is SYNCHRONOUS: it has no suspension point, so a real
        # cancellation can never be delivered inside it and this is
        # callback-originated. `CancelledError` is not an `Exception`, and
        # letting it out would exit the watchdog through its own cancellation
        # handler — silently disarming the kill until overall_timeout — or end
        # the stderr drain.
        _LOG.warning("%s(%s) raised CancelledError", name, arg)
    except Exception as exc:  # noqa: BLE001 — observer failure is not a kill signal
        _LOG.warning("%s(%s) raised: %r", name, arg, exc)


@dataclass
class DispatchResult:
    """Returned to server.py for envelope assembly.

    On hard pre-flight reject (request_too_large / disk_unavailable / spawn_failed),
    `error` is set and outcome is None.
    On transport failure (worker crash / malformed frame / no outcome), `outcome`
    is best-effort partial (may be None) and `error` is set to transport_failure.
    On success / main-side kill (overall_timeout / post_terminal_grace),
    `outcome` is set and downstream classify() interprets `kill_chain`.
    Cancel never produces a DispatchResult — CancelledError propagates up.
    """

    outcome: RunOutcome | None
    error: ErrorEnvelope | None = None
    diagnostics: dict | None = None


def _snapshot_env() -> dict[str, str]:
    snap = {
        "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    for k, v in os.environ.items():
        if k in _ENV_ALLOWLIST or k.startswith(_ENV_PREFIXES):
            snap[k] = v
    return snap


async def _janitor_sweep(temp_root: Path) -> None:
    cutoff = time.time() - JANITOR_AGE_S
    try:
        for entry in temp_root.glob(f"{TEMP_PREFIX}*"):
            try:
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue
    except OSError:
        return


def _ensure_janitor(temp_root: Path) -> None:
    global _janitor_started, _janitor_task
    if _janitor_started:
        return
    _janitor_started = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _janitor_task = loop.create_task(_janitor_sweep(temp_root))
    _janitor_task.add_done_callback(_swallow_task_exception)


def _swallow_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _LOG.warning("janitor task failed: %r", exc)


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    return _semaphore


async def _default_worker_spawn(env: dict[str, str]) -> asyncio.subprocess.Process:
    args = [sys.executable, "-u", "-m", "prism_router.worker"]
    kwargs: dict = {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "env": env,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return await asyncio.create_subprocess_exec(*args, **kwargs)


def _default_job_assign(pid: int) -> _jo.JobHandle:
    return _jo.assign_worker(pid)


def _parse_runoutcome_dict(d: dict, sidecar_dir: Path | None) -> RunOutcome:
    """Reconstitute RunOutcome from JSON dict (with optional sidecar deref)."""
    if "runoutcome_ref" in d and sidecar_dir is not None:
        ref = d["runoutcome_ref"]
        path = (sidecar_dir / ref).resolve()
        # Path-traversal: must resolve under sidecar_dir.
        if sidecar_dir.resolve() not in path.parents and path != sidecar_dir.resolve():
            raise ValueError(f"runoutcome_ref outside sidecar_dir: {path}")
        raw = path.read_bytes()
        if d.get("size") is not None and len(raw) != d["size"]:
            raise ValueError(f"sidecar size mismatch: got {len(raw)}, want {d['size']}")
        import hashlib
        sha = hashlib.sha256(raw).hexdigest()
        if d.get("sha256") and sha != d["sha256"]:
            raise ValueError("sidecar sha256 mismatch")
        d = json.loads(raw.decode("utf-8"))
    pr_d = d.get("parse_result")
    if pr_d is None:
        # Surface the worker's real exception. Without this the root cause
        # (e.g. "[WinError 267] ERROR_DIRECTORY" from a bad/transient spawn
        # cwd) is dropped and every backend-spawn abort reads as an opaque
        # transport_failure. worker_exception is set by worker.main's handler.
        wexc = d.get("worker_exception")
        if wexc:
            raise ValueError(f"worker aborted before outcome: {str(wexc).strip()}")
        raise ValueError("RunOutcome missing parse_result")
    pr = ParseResult(
        session_id=pr_d.get("session_id"),
        agent_messages=pr_d.get("agent_messages", ""),
        all_messages=list(pr_d.get("all_messages", [])),
        noise_log=list(pr_d.get("noise_log", [])),
        transient_lines=list(pr_d.get("transient_lines", [])),
        saw_terminal=bool(pr_d.get("saw_terminal", False)),
        error_events=list(pr_d.get("error_events", [])),
    )
    return RunOutcome(
        parse_result=pr,
        stderr_tail=d.get("stderr_tail", ""),
        kill_chain=[],
        returncode=d.get("returncode"),
        timed_out=bool(d.get("timed_out", False)),
    )


@dataclass
class _WorkerState:
    """Per-invoke mutable state tracked while worker is alive."""

    last_any_activity_ts: float
    terminal_seen_ts: float | None = None
    sidecar_published: dict | None = None
    stderr_buf: bytearray = field(default_factory=bytearray)
    milestones_seen: list[str] = field(default_factory=list)
    # Pre-terminal stdout-idle watchdog state.
    # `last_stdout_activity_ts` is set on the first `first_codex_output`
    # milestone and refreshed on each `stdout_tick`. The watchdog reads it
    # in a sleep loop; coalescing in the runner means the refresh cadence
    # is ≥2s or ≥200 lines per pipe — fine for an idle threshold measured
    # in minutes.
    last_stdout_activity_ts: float | None = None
    # Diagnostics: max observed gap between stdout activity ticks (or
    # between first_codex_output and terminal_event_seen if no ticks fired).
    # Recorded for both successful and killed calls so the threshold can be
    # tightened with real data over time.
    max_stdout_silence_s: float = 0.0
    # Codex thread_id captured from the parser via the `thread_id` milestone
    # (runner emits once after first thread.started admits). Used to locate
    # codex's rollout file for recovery on pre_terminal_idle.
    # docs/bugs/rollout-recovery.md
    codex_thread_id: str | None = None
    # Wall clock (epoch seconds) taken before the worker spawns. Fences rollout
    # recovery off from earlier turns of a resumed session — they all live in
    # one file. docs/bugs/rollout-recovery-cross-turn-leak.md
    spawn_wall_ts: float = 0.0
    # Rollout-liveness tick (docs/bugs/rollout-liveness-tick.md): resolved
    # rollout path, last observed st_size (first stat sets the baseline —
    # growth counts only against a prior observation, so a resumed session's
    # pre-existing content never reads as liveness), monotonic ts of the
    # first successful stat (how long the file was actually watched — a late
    # baseline must not be over-read as threshold-long rollout silence), ts
    # of the last observed growth, and growth count for diagnostics.
    rollout_path: Path | None = None
    last_rollout_size: int | None = None
    rollout_baseline_ts: float | None = None
    last_rollout_growth_ts: float | None = None
    rollout_ticks: int = 0
    # Observation health: ts of the last probe that returned evidence,
    # consecutive evidence-less probes since then (scale-free dark-channel
    # signal), and the kill-time verdict "probes had gone dark" (set by the
    # watchdog just before killing). A dark observation channel must not be
    # read as a confirmed rollout stall — it blocks the stall claim in the
    # kill raw and flags the diagnostics for B-trigger audits.
    last_probe_ok_ts: float | None = None
    probe_misses_since_ok: int = 0
    rollout_observation_stale: bool = False


class Dispatcher:
    """One instance per MCP tool call. Holds per-call state. No sharing."""

    def __init__(
        self,
        *,
        worker_spawner: WorkerSpawner | None = None,
        job_assigner: JobAssigner | None = None,
        temp_root: Path | None = None,
        overall_timeout: float = OVERALL_TIMEOUT_S,
        post_terminal_grace: float = POST_TERMINAL_GRACE_S,
        pre_terminal_idle: float = PRE_TERMINAL_IDLE_S,
        on_liveness: LivenessCallback | None = None,
        on_child: ChildCallback | None = None,
    ):
        self._spawn = worker_spawner or _default_worker_spawn
        self._assign = job_assigner or _default_job_assign
        self._temp_root = temp_root or Path(tempfile.gettempdir())
        self._overall_timeout = overall_timeout
        self._post_terminal_grace = post_terminal_grace
        self._pre_terminal_idle = pre_terminal_idle
        self._on_liveness = on_liveness
        self._on_child = on_child

    async def invoke(
        self,
        *,
        backend_name: str,
        prompt: str,
        cfg: SpawnConfig,
    ) -> DispatchResult:
        _ensure_janitor(self._temp_root)
        acquired = False
        sem = _get_semaphore()
        try:
            await sem.acquire()
            acquired = True
            return await self._invoke_inner(backend_name, prompt, cfg)
        finally:
            if acquired:
                sem.release()

    async def _invoke_inner(
        self, backend_name: str, prompt: str, cfg: SpawnConfig,
    ) -> DispatchResult:
        # Build request and pre-flight size check.
        env_snap = _snapshot_env()
        per_call_dir = self._temp_root / f"{TEMP_PREFIX}{uuid.uuid4().hex}"
        try:
            per_call_dir.mkdir(parents=True, exist_ok=False)
            probe = per_call_dir / ".probe"
            probe.write_bytes(b"\x00")
            probe.unlink()
        except OSError as exc:
            shutil.rmtree(per_call_dir, ignore_errors=True)
            return DispatchResult(
                outcome=None,
                error=make_error(
                    "disk_unavailable", "pre_flight", retryable=True,
                    raw=f"temp dir probe failed: {exc}",
                ),
            )

        request = {
            "backend": backend_name,
            "prompt": prompt,
            "spawn_config": {
                "cwd": str(cfg.cwd),
                "sandbox": cfg.sandbox,
                "session_id": cfg.session_id,
                "model": cfg.model,
                "profile": cfg.profile,
                "skip_git_repo_check": cfg.skip_git_repo_check,
                "yolo": cfg.yolo,
                "images": [str(p) for p in cfg.images],
            },
            "sidecar_dir": str(per_call_dir),
        }
        body = json.dumps(request, ensure_ascii=True).encode("utf-8")
        if len(body) > STDIN_PREFLIGHT_CAP:
            shutil.rmtree(per_call_dir, ignore_errors=True)
            return DispatchResult(
                outcome=None,
                error=make_error(
                    "request_too_large", "pre_flight", retryable=False,
                    raw=f"request {len(body)} bytes exceeds {STDIN_PREFLIGHT_CAP}",
                ),
            )

        # Codex-only gating for pre_terminal_idle: the upstream bug
        # (rollout records task_complete but stdout never emits
        # turn.completed) is specific to the codex CLI. Other backends
        # (gemini, future) have different terminal semantics and silence
        # patterns — applying the same threshold without empirical data
        # would risk false kills. Effective threshold of 0 disables the
        # watchdog inside _drain_stderr.
        effective_pre_terminal_idle = (
            self._pre_terminal_idle if backend_name == "codex" else 0.0
        )

        try:
            return await self._run_worker(
                env_snap, body, per_call_dir, effective_pre_terminal_idle,
            )
        finally:
            shutil.rmtree(per_call_dir, ignore_errors=True)

    async def _run_worker(
        self,
        env: dict[str, str],
        body: bytes,
        per_call_dir: Path,
        pre_terminal_idle: float,
    ) -> DispatchResult:
        # Baseline for rollout recovery, taken before anything of this
        # invocation can reach codex's rollout file.
        spawn_wall_ts = time.time()
        # Spawn worker + assign to Job. Sync critical section after spawn:
        # NO awaits between create_subprocess_exec return and assign_worker.
        try:
            proc = await asyncio.wait_for(
                self._spawn(env), timeout=SPAWN_ASSIGN_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return DispatchResult(
                outcome=None,
                error=make_error(
                    "spawn_failed", "spawn", retryable=True,
                    raw=f"worker spawn exceeded {SPAWN_ASSIGN_TIMEOUT_S}s",
                ),
            )
        except OSError as exc:
            return DispatchResult(
                outcome=None,
                error=make_error(
                    "spawn_failed", "spawn", retryable=False,
                    raw=f"create_subprocess_exec: {exc}",
                ),
            )

        try:
            job = self._assign(proc.pid)
        except _jo.JobAssignError as exc:
            # Fail-closed: terminate worker; stdin never written.
            with _safe_terminate(proc):
                pass
            return DispatchResult(
                outcome=None,
                error=make_error(
                    "spawn_failed", "spawn", retryable=False,
                    raw=f"{exc.phase} GetLastError={exc.errno}",
                ),
            )

        state = _WorkerState(
            last_any_activity_ts=time.monotonic(), spawn_wall_ts=spawn_wall_ts,
        )
        kill_event = asyncio.Event()
        kill_reason: list[str] = []  # one-element list as mutable closure cell

        # Stderr drain (always-on while worker alive). Continues even on parse errors.
        # `_drain_stderr` / `_arm_pre_terminal_idle` are module-level, so the
        # observers travel as explicit parameters from here.
        stderr_task = asyncio.create_task(
            _drain_stderr(
                proc, state, kill_event,
                self._post_terminal_grace, pre_terminal_idle,
                kill_reason, self._on_liveness, self._on_child,
            ),
        )

        try:
            # Write framed stdin.
            try:
                await asyncio.wait_for(
                    _write_framed_stdin(proc, body),
                    timeout=REQUEST_WRITE_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                kill_reason.append("transport_failure_write")
                kill_event.set()
            except (BrokenPipeError, ConnectionResetError) as exc:
                kill_reason.append("transport_failure_write")
                kill_event.set()
                _LOG.warning("stdin write failed: %r", exc)

            # Read framed stdout, racing against kill_event / overall_timeout.
            stdout_task = asyncio.create_task(
                _read_framed_stdout(proc, state, self._overall_timeout),
            )
            timeout_task = asyncio.create_task(
                _arm_overall_timeout(self._overall_timeout, kill_event, kill_reason),
            )
            kill_wait = asyncio.create_task(kill_event.wait())

            try:
                done, _pending = await asyncio.wait(
                    {stdout_task, kill_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                # MCP cancel: cleanup, re-raise.
                kill_reason.append("cancelled")
                job.close()
                with _safe_cancel(stdout_task, stderr_task, timeout_task, kill_wait):
                    pass
                raise

            # Prefer valid stdout result unless cancel won.
            outcome: RunOutcome | None = None
            transport_error: str | None = None
            if stdout_task.done() and not stdout_task.cancelled():
                exc = stdout_task.exception()
                if exc is None:
                    frame = stdout_task.result()
                    try:
                        outcome = _parse_runoutcome_dict(frame, per_call_dir)
                    except Exception as parse_exc:
                        transport_error = f"frame parse: {parse_exc}"
                else:
                    transport_error = f"stdout read: {exc}"

            # If we have a valid outcome, deliver it (kill_event may have fired but
            # the read won the race).
            if outcome is not None:
                # With the stderr `thread_id` milestone missed, the frame is where
                # the child id is learned: report it before reaping the worker, an
                # await a cancel can interrupt.
                if outcome.parse_result.session_id:
                    _notify(self._on_child, "on_child", outcome.parse_result.session_id)
                job.close()
                with _safe_cancel(stderr_task, timeout_task, kill_wait, stdout_task):
                    pass
                await proc_wait_bounded(proc)
                # Record observability data for successful calls. This is
                # how we learn the empirical distribution of legitimate
                # max stdout silence — needed to tighten the
                # PRE_TERMINAL_IDLE_S default below 1200s in a future
                # release without speculation.
                _LOG.info(
                    "codex.call.success max_stdout_silence_s=%.1f "
                    "saw_terminal=%s milestones=%d",
                    state.max_stdout_silence_s,
                    state.terminal_seen_ts is not None,
                    len(state.milestones_seen),
                )
                return DispatchResult(
                    outcome=outcome,
                    diagnostics={
                        "max_stdout_silence_s": state.max_stdout_silence_s,
                        "saw_terminal_event": state.terminal_seen_ts is not None,
                        **_rollout_liveness_diag(state),
                    },
                )

            # No valid outcome — drive kill path. CloseHandle(job) reaps tree.
            job.close()
            # Try sidecar recovery if we saw sidecar_published milestone.
            if state.sidecar_published is not None:
                try:
                    outcome = _parse_runoutcome_dict(
                        state.sidecar_published, per_call_dir,
                    )
                except Exception as exc:
                    _LOG.warning("sidecar recovery failed: %r", exc)
                    outcome = None

            with _safe_cancel(stderr_task, timeout_task, kill_wait, stdout_task):
                pass
            await proc_wait_bounded(proc)

            # Rollout recovery: bound the filesystem work with wait_for so a
            # slow glob (huge ~/.codex/sessions/ tree) cannot stall the event
            # loop. Cancellation propagates — recovery is best-effort, not
            # shielded. See docs/bugs/rollout-recovery.md.
            rollout_recovered: _rollout_recovery.Recovered | None = None
            rollout_recovery_timed_out = False
            if (
                kill_reason
                and kill_reason[0] == "pre_terminal_idle"
                and state.codex_thread_id is not None
            ):
                try:
                    rollout_recovered = await asyncio.wait_for(
                        asyncio.to_thread(
                            _rollout_recovery.recover, state.codex_thread_id,
                            not_before=state.spawn_wall_ts,
                        ),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    rollout_recovery_timed_out = True
                except Exception as exc:  # noqa: BLE001 — log + degrade
                    _LOG.warning("rollout recovery raised: %r", exc)

            return self._build_kill_result(
                state, kill_reason, proc.returncode, outcome, transport_error,
                rollout_recovered=rollout_recovered,
                rollout_recovery_timed_out=rollout_recovery_timed_out,
            )
        finally:
            # Always-on cleanup: ensure job closed (idempotent).
            try:
                job.close()
            except Exception:
                pass

    def _build_kill_result(
        self,
        state: _WorkerState,
        kill_reason: list[str],
        worker_rc: int | None,
        outcome: RunOutcome | None,
        transport_error: str | None,
        *,
        rollout_recovered: _rollout_recovery.Recovered | None = None,
        rollout_recovery_timed_out: bool = False,
    ) -> DispatchResult:
        reason = kill_reason[0] if kill_reason else "transport_failure"
        last_stdout_milestone = next(
            (m for m in reversed(state.milestones_seen)
             if m in ("stdout_tick", "first_codex_output", "terminal_event_seen")),
            None,
        )
        diagnostics: dict = {
            "worker_rc": worker_rc,
            "kill_reason": reason,
            "stderr_tail": bytes(state.stderr_buf[-STDERR_TAIL_BYTES:]).decode(
                "utf-8", "replace",
            ),
            "milestones_seen": list(state.milestones_seen),
            "transport_error": transport_error,
            # Pre-terminal idle diagnostics — useful for both observability
            # and tightening the threshold over time based on real data.
            "max_stdout_silence_s": state.max_stdout_silence_s,
            "last_stdout_milestone": last_stdout_milestone,
            "saw_terminal_event": state.terminal_seen_ts is not None,
            **_rollout_liveness_diag(state),
        }
        if outcome is not None and reason != "pre_terminal_idle":
            # Recovery (sidecar) or late delivery: the user got their data. Do NOT
            # poison kill_chain — that would route classify() into shutdown error.
            # Diagnostics carries the kill info for observability.
            # EXCEPTION: pre_terminal_idle is handled below — sidecar wins only
            # when the worker truly saw terminal; rollout recovery otherwise.
            return DispatchResult(outcome=outcome, diagnostics=diagnostics)

        if reason == "overall_timeout":
            err = make_error(
                "timeout", "shutdown", retryable=True,
                raw=f"overall timeout exceeded (rc={worker_rc})",
                stderr_tail=diagnostics["stderr_tail"] or None,
            )
        elif reason == "post_terminal_grace":
            err = make_error(
                "post_terminal_grace", "shutdown", retryable=True,
                raw=f"no outcome after terminal_event_seen + {self._post_terminal_grace}s",
                stderr_tail=diagnostics["stderr_tail"] or None,
            )
        elif reason == "pre_terminal_idle":
            # Sidecar preference: defensive against a late-delivery race
            # where the worker parsed terminal and wrote a large sidecar
            # before dispatcher could read the reference frame. Only wins
            # when the worker LEGITIMATELY saw terminal; partial sidecar
            # without terminal stays an error.
            if outcome is not None and outcome.parse_result.saw_terminal:
                diagnostics["recovered_from_sidecar"] = True
                # Rebuild RunOutcome with rc=0; the kill rc is dispatcher-
                # induced and would re-fire classify's spawn_failed branch.
                recovered_outcome = RunOutcome(
                    parse_result=outcome.parse_result,
                    stderr_tail=outcome.stderr_tail,
                    kill_chain=outcome.kill_chain,
                    returncode=0,
                    timed_out=outcome.timed_out,
                )
                _LOG.info(
                    "codex.call.success max_stdout_silence_s=%.1f "
                    "saw_terminal=%s milestones=%d recovered_from_sidecar=True",
                    state.max_stdout_silence_s,
                    True,
                    len(state.milestones_seen),
                )
                return DispatchResult(
                    outcome=recovered_outcome, diagnostics=diagnostics,
                )
            # Rollout recovery: when codex's stdout died but its own JSONL
            # ledger still carries the final agent_message (the upstream
            # 0.130.0 stdout-flush bug pattern).
            if rollout_recovered is not None and rollout_recovered.kind == "complete":
                diagnostics["recovered_from_rollout"] = True
                diagnostics["rollout_path"] = str(rollout_recovered.path)
                synth = _synthesize_recovered_outcome(state, rollout_recovered)
                _LOG.info(
                    "codex.call.success max_stdout_silence_s=%.1f "
                    "saw_terminal=False milestones=%d recovered_from_rollout=True "
                    "rollout_path=%s",
                    state.max_stdout_silence_s,
                    len(state.milestones_seen),
                    rollout_recovered.path,
                )
                return DispatchResult(outcome=synth, diagnostics=diagnostics)
            if rollout_recovered is not None and rollout_recovered.kind == "failed":
                diagnostics["recovered_from_rollout"] = True
                diagnostics["rollout_path"] = str(rollout_recovered.path)
                err = make_error(
                    "codex_error", "shutdown", retryable=False,
                    raw=(
                        f"codex turn failed (recovered from rollout): "
                        f"{rollout_recovered.error}"
                    ),
                    stderr_tail=diagnostics["stderr_tail"] or None,
                )
                return DispatchResult(
                    outcome=None, error=err, diagnostics=diagnostics,
                )
            if rollout_recovery_timed_out:
                diagnostics["rollout_recovery_timed_out"] = True
            # Rollout-liveness verdict for the transcript. Present only when
            # the file was actually watched — untracked kills keep the
            # pre-liveness raw byte-identical. Tiered for attribution honesty
            # (docs/bugs/rollout-liveness-tick.md): a dark observation channel
            # forfeits any stall claim; only observed growth proves a
            # threshold-long rollout stall (the growth clock IS the liveness
            # deadline); a never-grew watch reports its window instead of
            # claiming a stall, so a late baseline cannot masquerade as a
            # B-class fingerprint.
            rollout_note = ""
            if state.rollout_baseline_ts is not None:
                if state.rollout_observation_stale:
                    rollout_note = (
                        f"; rollout observation lost "
                        f"({time.monotonic() - state.last_probe_ok_ts:.0f}s "
                        f"since last successful stat — no stall verdict)"
                    )
                elif state.last_rollout_growth_ts is not None:
                    rollout_note = (
                        f"; rollout file stalled too "
                        f"({time.monotonic() - state.last_rollout_growth_ts:.0f}s "
                        f"since last observed growth, "
                        f"{state.rollout_ticks} growth ticks)"
                    )
                else:
                    rollout_note = (
                        f"; no rollout growth observed in the "
                        f"{time.monotonic() - state.rollout_baseline_ts:.0f}s watched"
                    )
            err = make_error(
                "pre_terminal_idle", "shutdown", retryable=True,
                raw=(
                    f"no tick-eligible stdout for {self._pre_terminal_idle:.0f}s "
                    f"after first_codex_output (rc={worker_rc}; recognized "
                    f"reconnect chatter excluded); upstream codex CLI appears "
                    f"wedged or stuck reconnecting{rollout_note}. Retry with "
                    f"PRISM_PRE_TERMINAL_IDLE=3600 if this is a known "
                    f"long-silence task."
                ),
                stderr_tail=diagnostics["stderr_tail"] or None,
            )
        else:
            err = make_error(
                "transport_failure", "shutdown", retryable=True,
                raw=f"reason={reason} rc={worker_rc} err={transport_error}",
                stderr_tail=diagnostics["stderr_tail"] or None,
            )
        return DispatchResult(outcome=None, error=err, diagnostics=diagnostics)


def _synthesize_recovered_outcome(
    state: _WorkerState, recovered: _rollout_recovery.Recovered,
) -> RunOutcome:
    """Build a RunOutcome from rollout-recovered content. `returncode=0`
    so classify treats it as success; `saw_terminal=False` is honest —
    stdout never carried the terminal event. Empty stderr_tail to avoid
    spurious proxy/auth pattern reclassification."""
    if state.codex_thread_id is None:
        # Caller must have checked this; localize the invariant to prevent
        # future reorder bugs. Real raise (not assert) — `python -O` strips
        # asserts.
        raise RuntimeError(
            "_synthesize_recovered_outcome called without codex_thread_id"
        )
    return RunOutcome(
        parse_result=ParseResult(
            session_id=state.codex_thread_id,
            agent_messages=recovered.agent_message or "",
            all_messages=[],
            noise_log=[],
            transient_lines=[],
            saw_terminal=False,
            error_events=[],
        ),
        stderr_tail="",
        kill_chain=[],
        returncode=0,
        timed_out=False,
    )


# ───── helpers ─────


import contextlib


@contextlib.contextmanager
def _safe_terminate(proc: asyncio.subprocess.Process):
    """Best-effort terminate (used on assign failure before Job exists)."""
    try:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.terminate()
        yield
    finally:
        pass


@contextlib.contextmanager
def _safe_cancel(*tasks: asyncio.Task):
    for t in tasks:
        if not t.done():
            t.cancel()
    yield


async def proc_wait_bounded(proc: asyncio.subprocess.Process, *, timeout: float = 2.0) -> None:
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()


async def _write_framed_stdin(
    proc: asyncio.subprocess.Process, body: bytes,
) -> None:
    if proc.stdin is None:
        raise BrokenPipeError("worker stdin is None")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    proc.stdin.write(header)
    proc.stdin.write(body)
    await proc.stdin.drain()
    # stdin stays OPEN (per D1 R3). Cancel uses CloseHandle(job); not EOF.


async def _arm_overall_timeout(
    timeout: float, kill_event: asyncio.Event, kill_reason: list[str],
) -> None:
    try:
        await asyncio.sleep(timeout)
        if not kill_event.is_set():
            kill_reason.append("overall_timeout")
            kill_event.set()
    except asyncio.CancelledError:
        return


async def _read_framed_stdout(
    proc: asyncio.subprocess.Process,
    state: _WorkerState,
    overall_timeout: float,
) -> dict:
    """Phased timeouts: no Phase 1 timeout (governed by overall + kill_event);
    Phase 2 (header) abs 10s after first byte; Phase 3 (body) idle 10s."""
    if proc.stdout is None:
        raise EOFError("worker stdout is None")
    reader = proc.stdout

    # Phase 1: wait first byte. Governed by overall_timeout + kill_event upstream.
    first = await reader.read(1)
    if not first:
        raise EOFError("worker stdout EOF before any data")
    state.last_any_activity_ts = time.monotonic()

    # Phase 2: read remainder of header (abs 10s cap after first byte).
    header = bytearray(first)
    deadline = time.monotonic() + PROTOCOL_READ_TIMEOUT_S
    while not bytes(header).endswith(b"\r\n\r\n"):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or len(header) > HEADER_CAP:
            raise asyncio.TimeoutError(
                f"protocol header read timeout (got {len(header)} bytes)"
            )
        try:
            b = await asyncio.wait_for(reader.read(1), timeout=remaining)
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError("protocol header read timeout")
        if not b:
            raise EOFError("worker stdout EOF during header")
        header.extend(b)

    text = bytes(header).decode("ascii", "replace").strip()
    n: int | None = None
    for part in text.split("\r\n"):
        k, _, v = part.partition(":")
        if k.strip().lower() == "content-length":
            n = int(v.strip())
            break
    if n is None or n < 0 or n > STDOUT_FRAME_CAP:
        raise ValueError(f"invalid Content-Length: {text!r}")

    # Phase 3: idle-based 10s body read.
    body = bytearray()
    last_byte_ts = time.monotonic()
    while len(body) < n:
        idle_remaining = BODY_IDLE_TIMEOUT_S - (time.monotonic() - last_byte_ts)
        if idle_remaining <= 0:
            raise asyncio.TimeoutError(
                f"body idle timeout (got {len(body)} of {n})"
            )
        chunk = await asyncio.wait_for(
            reader.read(min(64 * 1024, n - len(body))),
            timeout=idle_remaining,
        )
        if not chunk:
            raise EOFError(f"worker stdout EOF mid-body ({len(body)} of {n})")
        body.extend(chunk)
        last_byte_ts = time.monotonic()
        state.last_any_activity_ts = last_byte_ts

    return json.loads(bytes(body).decode("utf-8"))


async def _drain_stderr(
    proc: asyncio.subprocess.Process,
    state: _WorkerState,
    kill_event: asyncio.Event,
    post_terminal_grace: float,
    pre_terminal_idle: float,
    kill_reason: list[str],
    on_liveness: LivenessCallback | None = None,
    on_child: ChildCallback | None = None,
) -> None:
    """Continuously drains worker stderr. Parses {"ev":...} milestones to update
    state. Bounded 4 KiB chunked reads; lines beyond 16 KiB truncated.
    Parse failures do NOT halt the loop.

    Milestone wiring:
    - `first_codex_output` → set `last_stdout_activity_ts` and arm the
      pre_terminal_idle watchdog (if threshold > 0). NOT forwarded to
      `on_liveness`: the runner emits it before transient classification, so a
      first `Reconnecting...` line would reset the host timer on non-evidence.
    - `stdout_tick` → refresh `last_stdout_activity_ts` and update
      `max_stdout_silence_s` for diagnostics. Does NOT consider `stderr_tick`
      — stderr chatter must not mask a true stdout stall. The runner also
      withholds this tick for recognized reconnect chatter, so the threshold
      measures silence excluding retry storms rather than raw byte silence.
      NOTE: that also changed `max_stdout_silence_s` semantics — values are
      not comparable with samples recorded before 2026-08-11.
    - `terminal_event_seen` → arm post_terminal_grace AND cancel the
      pre_terminal_idle watchdog (we've reached the controlled-shutdown
      window).
    - `sidecar_published` → capture for late-delivery recovery.
    """
    if proc.stderr is None:
        return
    reader = proc.stderr
    line_buf = bytearray()
    grace_task: asyncio.Task | None = None
    idle_task: asyncio.Task | None = None

    try:
        while True:
            try:
                chunk = await reader.read(4096)
            except asyncio.CancelledError:
                raise
            except Exception:
                return
            if not chunk:
                return
            state.stderr_buf.extend(chunk)
            line_buf.extend(chunk)
            state.last_any_activity_ts = time.monotonic()
            while True:
                idx = line_buf.find(b"\n")
                if idx < 0:
                    if len(line_buf) > STDERR_MAX_LINE:
                        # Drop over-long line, keep tail as fresh buffer.
                        del line_buf[: -STDERR_MAX_LINE // 2]
                    break
                line = bytes(line_buf[:idx]).rstrip(b"\r")
                del line_buf[: idx + 1]
                try:
                    obj = json.loads(line.decode("utf-8", "replace"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                ev = obj.get("ev")
                if not isinstance(ev, str):
                    continue
                state.milestones_seen.append(ev)
                now = time.monotonic()
                if ev == "first_codex_output":
                    # First stdout line — arm pre_terminal_idle watchdog.
                    # (runner emits this after the first `readline()`
                    # returns, not byte-level.) Idempotent: only the first
                    # emission arms; codex emits this once per run.
                    if state.last_stdout_activity_ts is None:
                        state.last_stdout_activity_ts = now
                        if pre_terminal_idle > 0 and idle_task is None:
                            idle_task = asyncio.create_task(
                                _arm_pre_terminal_idle(
                                    pre_terminal_idle, state,
                                    kill_event, kill_reason, on_liveness,
                                ),
                            )
                elif ev == "stdout_tick":
                    # Refresh idle deadline. Track max silence between ticks
                    # for diagnostics (lets us tighten threshold later).
                    _record_silence(state, now)
                    state.last_stdout_activity_ts = now
                    # Backend-agnostic evidence tier: this branch runs even
                    # when the idle watchdog is disabled (gemini,
                    # PRISM_PRE_TERMINAL_IDLE=0).
                    _emit_liveness(on_liveness, "stdout")
                elif ev == "terminal_event_seen" and state.terminal_seen_ts is None:
                    # Record final pre-terminal silence: between last stdout
                    # activity and the terminal milestone. Without this the
                    # diagnostic stays at 0 for successful calls whose
                    # silence happened entirely between first_codex_output
                    # (or last stdout_tick) and terminal_event_seen — exactly
                    # the data we need to tighten the threshold later.
                    _record_silence(state, now)
                    state.terminal_seen_ts = now
                    # We've entered the controlled-shutdown window. Cancel
                    # the pre_terminal_idle watchdog — its job is done —
                    # and hand off to post_terminal_grace.
                    if idle_task is not None and not idle_task.done():
                        idle_task.cancel()
                        idle_task = None
                    grace_task = asyncio.create_task(
                        _arm_post_terminal_grace(
                            post_terminal_grace, kill_event, kill_reason,
                        ),
                    )
                elif ev == "sidecar_published":
                    state.sidecar_published = obj
                elif ev == "thread_id":
                    # Runner emits this once when the parser captures the
                    # session id from the first `thread.started` (codex) /
                    # `init` (gemini) event. Stored for rollout recovery on
                    # pre_terminal_idle (docs/bugs/rollout-recovery.md) and
                    # fed to the lineage observer.
                    tid = obj.get("thread_id")
                    if (
                        isinstance(tid, str) and tid
                        and state.codex_thread_id is None
                    ):
                        state.codex_thread_id = tid
                        _notify(on_child, "on_child", tid)
    finally:
        for t in (grace_task, idle_task):
            if t is not None and not t.done():
                t.cancel()


async def _arm_post_terminal_grace(
    grace: float, kill_event: asyncio.Event, kill_reason: list[str],
) -> None:
    try:
        await asyncio.sleep(grace)
        if not kill_event.is_set():
            kill_reason.append("post_terminal_grace")
            kill_event.set()
    except asyncio.CancelledError:
        return


def _record_silence(state: _WorkerState, now: float) -> None:
    """Update `state.max_stdout_silence_s` if the current gap since
    `last_stdout_activity_ts` is the longest seen so far. Idempotent; no-op
    when `last_stdout_activity_ts is None`."""
    if state.last_stdout_activity_ts is None:
        return
    gap = now - state.last_stdout_activity_ts
    if gap > state.max_stdout_silence_s:
        state.max_stdout_silence_s = gap


async def _arm_pre_terminal_idle(
    threshold: float,
    state: _WorkerState,
    kill_event: asyncio.Event,
    kill_reason: list[str],
    on_liveness: LivenessCallback | None = None,
) -> None:
    """Force a kill if BOTH stdout and codex's rollout file stay silent past
    `threshold` after `first_codex_output`. Loops so that `stdout_tick`
    refreshes (which only update `last_stdout_activity_ts`) are honored
    without restarting the task.

    Rollout-liveness tick (docs/bugs/rollout-liveness-tick.md): under a proxy
    that returns empty reasoning summaries, `codex exec --json` filters ALL
    reasoning off stdout, so a legitimate pure-reasoning turn is stdout-silent
    for its whole duration while still appending to its rollout file. Observed
    st_size growth of that file refreshes the same deadline as `stdout_tick`.
    Stat-only: contents are never read and no handle is held while the turn
    runs. All filesystem work happens in `_probe_rollout` on a worker thread
    that is NEVER awaited inline — at most one probe is in flight and its
    result is applied on a later iteration, so a hung or slow filesystem
    cannot delay the kill decision, block the event loop, or stack threads
    (one leaked pool slot per call worst-case, same exposure as recovery's
    to_thread). Every failure mode (missing thread_id, unresolved path, stat
    error, a future buffered rollout writer) leaves `last_rollout_growth_ts`
    unset and degrades to the pre-liveness behavior — the watchdog can only
    get LESS aggressive than stdout-only, never more.

    Cancellation paths:
    - `terminal_event_seen` arrives → drain_stderr cancels us (job done).
    - Any other kill_event source fires → we exit the loop without appending
      a competing kill_reason; the original reason wins.
    - MCP cancel propagates CancelledError → we return cleanly.

    Poll floor: `min(1.0, threshold/4)`. Tests use small thresholds (≤0.5s)
    and need quick reaction; production threshold (1200s) easily tolerates a
    1s overshoot.
    """
    poll_floor = min(1.0, max(0.05, threshold / 4))
    rollout_poll = min(ROLLOUT_POLL_CAP_S, max(0.05, threshold / 8))
    next_rollout_poll = 0.0  # monotonic gate; first pass probes immediately
    next_deadline_check = 0.0  # first pass establishes the check schedule
    probe: asyncio.Task | None = None
    final_beat_used = False
    try:
        while not kill_event.is_set():
            if state.last_stdout_activity_ts is None:
                # Defensive: arm-on-first_codex_output should set this before
                # we run, but the milestone could in theory race the task
                # creation. Sleep small and re-check.
                await asyncio.sleep(poll_floor)
                continue
            now = time.monotonic()
            if probe is not None and probe.done():
                if not probe.cancelled():
                    exc = probe.exception()
                    if exc is not None:
                        state.probe_misses_since_ok += 1
                        _LOG.warning("rollout liveness probe raised: %r", exc)
                    else:
                        _apply_rollout_probe(
                            state, probe.result(), now, on_liveness,
                        )
                probe = None
            # Captured between consume and launch: a probe still unresolved
            # from an EARLIER wake (a fresh cadence launch below must not
            # count). If it survives the final beat, the observation channel
            # was unavailable at the kill verdict — even when the last
            # successful stat is recent (hung probe at production scale:
            # misses stay 0 and the age bound has not expired yet).
            probe_unresolved_at_wake = probe is not None
            if (
                probe is None
                and state.codex_thread_id is not None
                and now >= next_rollout_poll
            ):
                # Codex materializes the rollout on its first flush, which can
                # land after first_codex_output — the probe re-globs at poll
                # cadence until the path resolves, then stats it.
                next_rollout_poll = now + rollout_poll
                probe = asyncio.create_task(
                    asyncio.to_thread(
                        _probe_rollout, state.codex_thread_id, state.rollout_path,
                    ),
                )
            last_liveness = state.last_stdout_activity_ts
            if (
                state.last_rollout_growth_ts is not None
                and state.last_rollout_growth_ts > last_liveness
            ):
                last_liveness = state.last_rollout_growth_ts
            elapsed = now - last_liveness
            # The kill is evaluated ONLY on the pre-liveness loop's own check
            # schedule (`now + max(poll_floor, threshold - elapsed)` from the
            # previous check). The finer probe/thread_id wakes in between
            # must not evaluate it: they would land closer to the true
            # deadline than the old loop's coarse wakes and thus kill EARLIER
            # than pre-liveness on some inputs. With this gate the kill
            # instant is bit-identical to the old loop whenever liveness
            # contributes nothing (no thread_id, no file, no growth), and can
            # only move later when it does (`elapsed` here is ≤ the old
            # loop's stdout-only elapsed, so each check reschedules at or
            # past the old loop's next wake).
            if now >= next_deadline_check:
                if elapsed >= threshold:
                    if state.rollout_baseline_ts is not None and not final_beat_used:
                        # Final beat: give the in-flight stat one poll_floor
                        # to land (forcing a probe if none is in flight) so
                        # growth in the last cadence window is not missed —
                        # narrows the deadline blind window from one cadence
                        # to probe latency without ever awaiting the
                        # filesystem. One beat only; a hung probe then
                        # forfeits it. Skipped when the file was never
                        # tracked, keeping untracked kill timing exactly
                        # pre-liveness.
                        final_beat_used = True
                        if probe is None and state.codex_thread_id is not None:
                            probe = asyncio.create_task(
                                asyncio.to_thread(
                                    _probe_rollout,
                                    state.codex_thread_id, state.rollout_path,
                                ),
                            )
                        await asyncio.sleep(poll_floor)
                        continue
                    if state.rollout_baseline_ts is not None:
                        # Dark observation channel must not be read as a
                        # confirmed rollout stall downstream. Three signals:
                        # a probe still unresolved after the final beat, ≥2
                        # consecutive evidence-less probes (scale-free —
                        # catches real stat/glob failures at any threshold),
                        # or no successful stat well past the cadence.
                        state.rollout_observation_stale = (
                            probe_unresolved_at_wake
                            or state.probe_misses_since_ok >= 2
                            or state.last_probe_ok_ts is None
                            or now - state.last_probe_ok_ts
                            > 2 * (rollout_poll + poll_floor)
                        )
                    # Record the final stdout silence as a diagnostic before
                    # we kill, so post-mortem `max_stdout_silence_s` reflects
                    # the actual observed gap rather than the threshold value.
                    _record_silence(state, now)
                    if not kill_event.is_set():
                        kill_reason.append("pre_terminal_idle")
                        kill_event.set()
                    return
                final_beat_used = False
                next_deadline_check = now + max(poll_floor, threshold - elapsed)
            # Sleep to the next scheduled check, waking earlier only for the
            # probe cadence (thread_id known) or to notice a late `thread_id`
            # milestone (a separate stderr write that can trail
            # `first_codex_output` by a chunk gap; a full-deadline sleep
            # taken in that gap would strand the call with liveness
            # disengaged).
            sleep_s = next_deadline_check - now
            if state.codex_thread_id is not None:
                sleep_s = min(sleep_s, rollout_poll)
            else:
                sleep_s = min(sleep_s, poll_floor)
            await asyncio.sleep(max(sleep_s, 0.0))
    except asyncio.CancelledError:
        return
    finally:
        if probe is not None:
            if probe.done():
                # Retrieve a completed probe's exception so a loop exit via
                # cancel/terminal never leaves an unretrieved-task warning.
                if not probe.cancelled() and probe.exception() is not None:
                    _LOG.warning(
                        "rollout liveness probe raised: %r", probe.exception(),
                    )
            else:
                probe.cancel()


def _probe_rollout(
    thread_id: str, known_path: Path | None,
) -> tuple[Path, int] | None:
    """Thread-side rollout probe: resolve the path (UUID glob spans all date
    dirs — a resumed thread's file lives under its ORIGINAL date) unless
    already known, then stat it. Returns (path, st_size) or None on any
    failure; the caller treats None as "no liveness evidence"."""
    path = known_path or _rollout_recovery.find_rollout_path(thread_id)
    if path is None:
        return None
    try:
        return path, os.stat(path).st_size
    except OSError:
        return None


def _apply_rollout_probe(
    state: _WorkerState, result: tuple[Path, int] | None, now: float,
    on_liveness: LivenessCallback | None = None,
) -> None:
    """Fold a probe result into liveness state. st_size growth since the
    previous observation counts as liveness. The first successful stat only
    sets the baseline — a resumed session's pre-existing content must not
    read as growth. Shrink (external truncation; codex itself only appends)
    rebases without liveness credit. Evidence-less probes count toward the
    dark-channel signal."""
    if result is None:
        state.probe_misses_since_ok += 1
        return
    path, size = result
    state.rollout_path = path
    state.last_probe_ok_ts = now
    state.probe_misses_since_ok = 0
    prev = state.last_rollout_size
    state.last_rollout_size = size
    if prev is None:
        state.rollout_baseline_ts = now
    elif size > prev:
        state.last_rollout_growth_ts = now
        state.rollout_ticks += 1
        # Codex-only evidence tier: present iff the idle watchdog is armed.
        _emit_liveness(on_liveness, "rollout")


def _rollout_liveness_diag(state: _WorkerState) -> dict:
    """Rollout-liveness observability for both the success and kill paths.
    Empty when the watchdog never tracked the file (gemini, no thread_id,
    unresolved path) so those envelopes keep their pre-liveness shape; key
    absence itself marks "liveness unavailable" for transcript audits.
    `rollout_watched_s` guards against over-reading a late baseline: only a
    kill with observed growth (or a watch window ≥ threshold) proves the
    rollout itself was silent for the threshold."""
    if state.rollout_baseline_ts is None:
        return {}
    now = time.monotonic()
    diag = {
        "rollout_ticks": state.rollout_ticks,
        "rollout_watched_s": round(now - state.rollout_baseline_ts, 1),
        "last_rollout_growth_age_s": (
            round(now - state.last_rollout_growth_ts, 1)
            if state.last_rollout_growth_ts is not None
            else None
        ),
    }
    if state.rollout_observation_stale:
        diag["rollout_observation_stale"] = True
    return diag
