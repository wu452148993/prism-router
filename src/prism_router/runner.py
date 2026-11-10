"""§3.2 backend run loop — pure I/O, no kill chain.

Architecture (post worker-isolation): this module is called INSIDE the worker
subprocess. Main process (dispatcher) wraps the worker in a Windows Job Object
and owns all watchdogs; on any kill it calls CloseHandle(job) and the OS
terminates worker + codex + every grandchild atomically.

This module's only responsibilities:
1. Spawn codex.
2. Write PROMPT to stdin, close stdin.
3. Concurrently feed StreamParser from stdout, ring-buffer stderr.
4. When codex_proc.wait() resolves (the backend process is gone — hard fact),
   give `codex_exit_drain` seconds for trailing buffered stdout (Bug B:
   shell-tool grandchildren can hold the pipe write-end past codex.exe exit).
   Then cancel readers and assemble RunOutcome.

Worker has ZERO wall-clock timers. The `codex_exit_drain` coroutine is
event-triggered (by codex process exit), not anchored to spawn time.

Milestone callback (`on_milestone`) lets worker.py emit L2 milestone events
to stderr so the main dispatcher can arm post_terminal_grace and detect
activity. Events:
- `spawned`
- `first_codex_output`
- `terminal_event_seen`
- `stdout_tick` (stdout-only, coalesced; >=2s elapsed OR >=200 lines)
- `stderr_tick` (stderr-only, coalesced; same thresholds)

The split exists for the dispatcher's `pre_terminal_idle` watchdog: refresh
must be driven by stdout activity only — stderr chatter from the backend
should not suppress the idle kill. Lines the parser siphons as transient
(codex `Reconnecting... N/M`) do not tick either, so what the watchdog
measures is tick-eligible stdout — raw bytes minus recognized reconnect
chatter. See `dispatcher.py` for the watchdog.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .backend import Backend, SpawnConfig
from .parser import NOISE_LOG_CAP, ParseResult, StreamParser

_STDERR_TAIL_LINES = 20
_ACTIVITY_INTERVAL_S = 2.0
_ACTIVITY_LINE_BURST = 200
# StreamReader buffer per pipe. asyncio's default is 64 KiB; codex packs a
# shell tool's entire stdout into one `aggregated_output` JSON line (232 KB
# measured for `rg -n '' uv.lock`), which overran it. Aligned with the
# dispatcher's STDOUT_FRAME_CAP. docs/bugs/codex-stdout-bigline-truncation.md
_STREAM_LIMIT = 4 * 1024 * 1024
_OVERSIZED_LINE_NOTE = "prism: dropped oversized {stream} line ({detail})"
# Traces per unbroken run of dropped lines. Past NOISE_LOG_CAP the trace is no
# longer recorded anyway, and the cap bounds the milestone flood a
# non-consuming reader could produce.
_MAX_CONSECUTIVE_DROPS = NOISE_LOG_CAP


@dataclass(frozen=True)
class RunTimeouts:
    codex_exit_drain: float = 5.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "RunTimeouts":
        e = os.environ if env is None else env
        return cls(
            codex_exit_drain=float(e.get("PRISM_CODEX_EXIT_DRAIN", 5.0)),
        )


@dataclass(frozen=True)
class KillStep:
    """Retained for downstream classify() compatibility. Worker no longer
    populates kill_chain; the main dispatcher fills it from its own
    watchdogs (post_terminal_grace / overall_timeout / cancelled / ...)."""

    action: str
    reason: str
    elapsed: float


@dataclass
class RunOutcome:
    parse_result: ParseResult
    stderr_tail: str
    kill_chain: list[KillStep] = field(default_factory=list)
    returncode: int | None = None
    timed_out: bool = False


Spawner = Callable[..., Awaitable[asyncio.subprocess.Process]]
# Kwargs allowed so callers can attach payload (e.g. `thread_id="<uuid>"`)
# alongside the milestone tag. Worker `_emit_milestone(ev, **payload)` already
# supports this — used by `sidecar_published` for size/sha/path and by
# `thread_id` for dispatcher's rollout-recovery path.
MilestoneCallback = Callable[..., None]


def _resolve_command(command: str) -> str:
    """PATH-resolve `command` to an absolute executable path.

    `asyncio.create_subprocess_exec` on Windows calls `CreateProcessW` directly,
    which does NOT consult PATHEXT. npm CLIs ship as `foo.cmd`; bare `"codex"`
    yields WinError 2. shutil.which resolves the same path a shell would.
    """
    resolved = shutil.which(command)
    if resolved is None:
        raise FileNotFoundError(
            f"command {command!r} not found on PATH; install the CLI or fix PATH "
            f"(Windows: CreateProcessW ignores PATHEXT — npm CLIs land as .cmd; "
            f"verify with `where.exe {command}`)"
        )
    return resolved


async def _default_spawn(*args: str, **kwargs) -> asyncio.subprocess.Process:
    resolved = (_resolve_command(args[0]),) + tuple(args[1:])
    return await asyncio.create_subprocess_exec(*resolved, limit=_STREAM_LIMIT, **kwargs)


async def _iter_lines(reader, on_drop: Callable[[str], None]):
    """`readline()` loop that survives lines exceeding the StreamReader limit.

    On overrun asyncio raises `ValueError` and discards its buffer. Uncaught,
    the reader task died and `run()`'s `gather(..., return_exceptions=True)`
    swallowed it: stdout went silent for the rest of the session while codex
    kept working, so a healthy run either lost its terminal event or returned
    a truncated preamble as if complete.

    Recovery is: note it, keep reading. Nothing is skipped past the raise —
    asyncio drops the whole line when the separator was already buffered, so
    the next readline() is usually a real event. When it isn't (separator not
    yet seen, buffer cleared mid-line), the leftover tail surfaces as one
    unparsable line and the parser files it as noise. `on_drop` fires per
    discarded buffer, not per logical line: the two overrun variants are only
    distinguishable by message text, and under-reporting would let a stream
    still producing oversized lines look idle to the watchdog.
    """
    consecutive_drops = 0
    while True:
        try:
            line = await reader.readline()
        except ValueError as exc:
            consecutive_drops += 1
            # A reader that raises without consuming would spin: the yield point
            # keeps it cancellable, and past the cap it stops claiming activity
            # so a wedged stream cannot refresh `pre_terminal_idle` forever.
            if consecutive_drops <= _MAX_CONSECUTIVE_DROPS:
                on_drop(str(exc))
            await asyncio.sleep(0)
            continue
        if not line:
            return
        consecutive_drops = 0
        yield line


class _ActivityCoalescer:
    """Coalesces pipe activity into named milestones.

    Emits at most one tick per `interval_s` seconds OR per `line_burst`
    lines, whichever comes first. One instance per pipe so the dispatcher
    can distinguish stdout activity (which refreshes the pre_terminal_idle
    watchdog) from stderr chatter (which must not).
    """

    def __init__(
        self,
        on_milestone: MilestoneCallback | None,
        loop_time: Callable[[], float],
        *,
        tick_name: str,
        interval_s: float = _ACTIVITY_INTERVAL_S,
        line_burst: int = _ACTIVITY_LINE_BURST,
    ):
        self._cb = on_milestone
        self._now = loop_time
        self._last_tick = loop_time()
        self._lines_since = 0
        self._tick_name = tick_name
        self._interval_s = interval_s
        self._line_burst = line_burst

    def bump(self) -> None:
        if self._cb is None:
            return
        self._lines_since += 1
        now = self._now()
        if self._lines_since >= self._line_burst or (now - self._last_tick) >= self._interval_s:
            self._cb(self._tick_name)
            self._last_tick = now
            self._lines_since = 0


async def run(
    backend: Backend,
    prompt: str,
    cfg: SpawnConfig,
    *,
    timeouts: RunTimeouts | None = None,
    spawner: Spawner | None = None,
    on_milestone: MilestoneCallback | None = None,
) -> RunOutcome:
    t = timeouts or RunTimeouts.from_env()
    args = backend.build_args(cfg)
    parser = StreamParser(backend.spec)
    stderr_buf: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)

    spawn_kwargs: dict = {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "cwd": str(cfg.cwd),
    }
    if sys.platform == "win32":
        spawn_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    spawn = spawner or _default_spawn
    proc = await spawn(*args, **spawn_kwargs)
    loop = asyncio.get_running_loop()
    stdout_activity = _ActivityCoalescer(
        on_milestone, loop.time, tick_name="stdout_tick",
    )
    stderr_activity = _ActivityCoalescer(
        on_milestone, loop.time, tick_name="stderr_tick",
    )

    if on_milestone is not None:
        on_milestone("spawned")

    async def _write_stdin() -> None:
        if proc.stdin is None:
            return
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.close()

    async def _read_stdout() -> None:
        if proc.stdout is None:
            return
        first = True
        terminal_emitted = False
        thread_id_emitted = False

        def on_drop(detail: str) -> None:
            parser.feed_line(_OVERSIZED_LINE_NOTE.format(stream="stdout", detail=detail))
            # A multi-hundred-KB line takes real time to arrive; without this
            # the dispatcher's pre_terminal_idle watchdog sees pure silence.
            stdout_activity.bump()

        async for line in _iter_lines(proc.stdout, on_drop):
            if first and on_milestone is not None:
                on_milestone("first_codex_output")
                first = False
            transient_before = parser.transient_count
            parser.feed_line(line.decode("utf-8", "replace"))
            # Reconnect chatter is not progress. Counting it as activity let a
            # 33-min upstream retry storm refresh the watchdog every ~350s and
            # run to codex's own give-up (2026-08-10 incident).
            # docs/bugs/codex-transient-retry-storm.md
            if parser.transient_count == transient_before:
                stdout_activity.bump()
            # Surface session_id once the parser captures it (first
            # `thread.started` / `init` admit). Dispatcher uses this to
            # locate codex's rollout file when pre_terminal_idle fires
            # (docs/bugs/rollout-recovery.md).
            if (
                not thread_id_emitted
                and on_milestone is not None
                and parser.session_id is not None
            ):
                on_milestone("thread_id", thread_id=parser.session_id)
                thread_id_emitted = True
            if not terminal_emitted and parser.saw_terminal and on_milestone is not None:
                on_milestone("terminal_event_seen")
                terminal_emitted = True

    async def _read_stderr() -> None:
        if proc.stderr is None:
            return

        def on_drop(detail: str) -> None:
            stderr_buf.append(_OVERSIZED_LINE_NOTE.format(stream="stderr", detail=detail))
            stderr_activity.bump()

        async for line in _iter_lines(proc.stderr, on_drop):
            stderr_buf.append(line.decode("utf-8", "replace").rstrip("\r\n"))
            stderr_activity.bump()

    stdout_task = asyncio.create_task(_read_stdout())
    stderr_task = asyncio.create_task(_read_stderr())
    write_task = asyncio.create_task(_write_stdin())
    wait_task = asyncio.create_task(proc.wait())

    try:
        # Wait until the backend process actually exits. The dispatcher in main
        # owns all wall-clock decisions (overall_timeout, post_terminal_grace,
        # cancel); if any of those fire, main CloseHandle(job) kills our entire
        # process tree — wait_task resolves via that path or codex's natural exit.
        await wait_task
    except asyncio.CancelledError:
        # Our worker is being torn down (Job kill). Let finally drain best-effort.
        raise
    finally:
        # codex has exited (or we're being killed); give a bounded window for
        # any trailing stdout the OS pipe still holds. Bug B mechanism:
        # shell-tool grandchildren can outlive codex.exe and keep the write-end
        # open — readline() would otherwise never see EOF.
        try:
            await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task, return_exceptions=True),
                timeout=t.codex_exit_drain,
            )
        except asyncio.TimeoutError:
            for tk in (stdout_task, stderr_task):
                if not tk.done():
                    tk.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

        if not write_task.done():
            write_task.cancel()
        await asyncio.gather(write_task, return_exceptions=True)

    return RunOutcome(
        parse_result=parser.result(),
        stderr_tail="\n".join(stderr_buf),
        returncode=proc.returncode,
    )
