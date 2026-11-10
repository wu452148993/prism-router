"""runner.run() pure tests — subprocess mocked via injected `spawner`.

Covers (post worker-isolation):
- stdin write + close
- stdout → parser pipeline
- stderr ring buffer
- L2 milestone callback (spawned / first_codex_output / terminal_event_seen / stdout_tick / stderr_tick)
- codex_exit_drain: bounded drain when process exits but stdout pipe stays held
- natural EOF: no drain latency on clean codex exits

NO kill-chain tests: under worker isolation, the main dispatcher kills via
CloseHandle(job). The runner module (run inside the worker) has zero
wall-clock timers.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from prism_router import runner as runner_mod
from prism_router.backend import SpawnConfig
from prism_router.backend_codex import CodexBackend
from prism_router.backend_gemini import GeminiBackend
from prism_router.runner import RunTimeouts, _resolve_command, run


# ──────────────── fake subprocess plumbing ────────────────


class _FakeStdout:
    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)
        self._eof_event = asyncio.Event()

    async def readline(self) -> bytes:
        if self._lines:
            line = self._lines.pop(0)
            await asyncio.sleep(0)
            return line
        await self._eof_event.wait()
        return b""

    def signal_eof(self) -> None:
        self._eof_event.set()


class _FakeStdin:
    def __init__(self):
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True


class FakeProc:
    """Minimal asyncio.subprocess.Process surface for runner injection."""

    def __init__(
        self,
        stdout_lines: list[bytes],
        stderr_lines: list[bytes],
        *,
        signal_eof_on_exit: bool = True,
    ):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(stdout_lines)
        self.stderr = _FakeStdout(stderr_lines)
        self._returncode: int | None = None
        self._exit_event = asyncio.Event()
        # When False, _exit() sets returncode but does NOT signal stdout/stderr
        # EOF — models the production failure mode where descendant processes
        # keep the pipe write-end open after proc exits (Bug B mechanism).
        self._signal_eof_on_exit = signal_eof_on_exit

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def exit_natural(self, rc: int = 0) -> None:
        if self._returncode is None:
            self._returncode = rc
            if self._signal_eof_on_exit:
                self.stdout.signal_eof()
                self.stderr.signal_eof()
            self._exit_event.set()

    async def wait(self) -> int:
        await self._exit_event.wait()
        assert self._returncode is not None
        return self._returncode


def _spawner(proc: FakeProc):
    captured: dict = {}

    async def factory(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return proc

    factory.captured = captured
    return factory


def _cfg(cwd: Path | None = None) -> SpawnConfig:
    return SpawnConfig(
        cwd=cwd or Path("/tmp/work"),
        sandbox="",
        session_id=None,
        model="",
        profile="",
        skip_git_repo_check=True,
        yolo=False,
        images=(),
    )


_FAST = RunTimeouts(codex_exit_drain=0.1)


def _milestone_recorder(seen: list[str]):
    """Callable adapter swallowing kwargs (thread_id milestone carries
    `thread_id=...`). Backward-compat for tests that only inspect the tag."""
    def cb(ev: str, **_kw) -> None:
        seen.append(ev)
    return cb


# ──────────────── tests ────────────────


@pytest.mark.asyncio
async def test_stdin_receives_prompt_and_is_closed():
    init = b'{"type":"thread.started","thread_id":"t-1"}\n'
    done = b'{"type":"turn.completed"}\n'
    proc = FakeProc([init, done], [])
    spawn = _spawner(proc)

    async def driver():
        await asyncio.sleep(0)
        proc.exit_natural(0)

    runner_task = asyncio.create_task(
        run(CodexBackend(), "hello world", _cfg(), timeouts=_FAST, spawner=spawn)
    )
    asyncio.create_task(driver())
    await runner_task
    assert bytes(proc.stdin.written) == b"hello world"
    assert proc.stdin.closed is True


@pytest.mark.asyncio
async def test_natural_exit_assembles_outcome():
    init = b'{"type":"init","session_id":"g-1","model":"gemini-2.5-pro"}\n'
    result = b'{"type":"result","status":"ok"}\n'
    proc = FakeProc([init, result], [])
    spawn = _spawner(proc)

    async def driver():
        await asyncio.sleep(0)
        proc.exit_natural(0)

    runner_task = asyncio.create_task(
        run(GeminiBackend(), "ping", _cfg(), timeouts=_FAST, spawner=spawn)
    )
    asyncio.create_task(driver())
    outcome = await runner_task
    assert outcome.returncode == 0
    assert outcome.timed_out is False
    assert outcome.kill_chain == []
    assert outcome.parse_result.session_id == "g-1"


@pytest.mark.asyncio
async def test_milestone_callback_emits_lifecycle_events():
    init = b'{"type":"thread.started","thread_id":"t-1"}\n'
    msg = b'{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n'
    done = b'{"type":"turn.completed"}\n'
    proc = FakeProc([init, msg, done], [])
    spawn = _spawner(proc)

    seen: list[str] = []

    async def driver():
        await asyncio.sleep(0)
        proc.exit_natural(0)

    runner_task = asyncio.create_task(
        run(
            CodexBackend(), "p", _cfg(), timeouts=_FAST, spawner=spawn,
            on_milestone=_milestone_recorder(seen),
        )
    )
    asyncio.create_task(driver())
    await runner_task

    assert "spawned" in seen
    assert "first_codex_output" in seen
    assert "terminal_event_seen" in seen
    # spawned must come before first_codex_output before terminal_event_seen
    assert seen.index("spawned") < seen.index("first_codex_output")
    assert seen.index("first_codex_output") < seen.index("terminal_event_seen")
    # terminal_event_seen is one-shot
    assert seen.count("terminal_event_seen") == 1


@pytest.mark.asyncio
async def test_thread_id_milestone_emitted_once_with_payload():
    """Parser captures thread_id on first thread.started; runner relays it
    via milestone (with thread_id kwarg) so dispatcher can locate codex's
    rollout file when pre_terminal_idle fires
    (docs/bugs/rollout-recovery.md)."""
    init = b'{"type":"thread.started","thread_id":"t-abc"}\n'
    msg = b'{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n'
    done = b'{"type":"turn.completed"}\n'
    proc = FakeProc([init, msg, done], [])
    spawn = _spawner(proc)

    seen: list[tuple[str, dict]] = []

    def cb(ev: str, **payload) -> None:
        seen.append((ev, payload))

    async def driver():
        await asyncio.sleep(0)
        proc.exit_natural(0)

    runner_task = asyncio.create_task(
        run(
            CodexBackend(), "p", _cfg(), timeouts=_FAST, spawner=spawn,
            on_milestone=cb,
        )
    )
    asyncio.create_task(driver())
    await runner_task

    thread_id_events = [e for e in seen if e[0] == "thread_id"]
    assert len(thread_id_events) == 1, seen
    assert thread_id_events[0][1] == {"thread_id": "t-abc"}
    # thread_id fires AFTER first_codex_output (parser admits the line first)
    tags = [e[0] for e in seen]
    assert tags.index("first_codex_output") < tags.index("thread_id")


@pytest.mark.asyncio
async def test_thread_id_milestone_not_emitted_when_init_event_missing():
    """No `thread.started` ⇒ parser.session_id stays None ⇒ no milestone.
    Recovery path degrades to plain pre_terminal_idle on dispatcher side."""
    # Stream starts with item.completed (no init); parser admits via
    # required-field validation but session_id never captured.
    msg = b'{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n'
    done = b'{"type":"turn.completed"}\n'
    proc = FakeProc([msg, done], [])
    spawn = _spawner(proc)

    seen: list[str] = []

    async def driver():
        await asyncio.sleep(0)
        proc.exit_natural(0)

    runner_task = asyncio.create_task(
        run(
            CodexBackend(), "p", _cfg(), timeouts=_FAST, spawner=spawn,
            on_milestone=_milestone_recorder(seen),
        )
    )
    asyncio.create_task(driver())
    await runner_task

    assert "thread_id" not in seen


@pytest.mark.asyncio
async def test_codex_exit_drain_bounds_pipe_held_by_grandchildren():
    """Bug B mechanism: codex exits but grandchildren hold the stdout
    write-end → readline never sees EOF. codex_exit_drain bounds the wait."""
    init = b'{"type":"thread.started","thread_id":"t-1"}\n'
    done = b'{"type":"turn.completed"}\n'
    # signal_eof_on_exit=False: proc exits but stdout/stderr stay "open"
    proc = FakeProc([init, done], [], signal_eof_on_exit=False)
    spawn = _spawner(proc)

    async def driver():
        await asyncio.sleep(0)
        proc.exit_natural(0)

    runner_task = asyncio.create_task(
        run(CodexBackend(), "p", _cfg(), timeouts=_FAST, spawner=spawn)
    )
    asyncio.create_task(driver())

    # Without bound, this would hang on stdout.readline() forever.
    outcome = await asyncio.wait_for(runner_task, timeout=2.0)
    assert outcome.returncode == 0


@pytest.mark.asyncio
async def test_stdout_tick_coalesced_by_interval():
    """Many lines in tight succession should still produce few ticks
    (>=200 lines OR >=2s gap)."""
    lines = [b'{"type":"item.completed","item":{"type":"agent_message","text":"x"}}\n'] * 50
    proc = FakeProc(lines, [])
    spawn = _spawner(proc)

    seen: list[str] = []

    async def driver():
        await asyncio.sleep(0.01)
        proc.exit_natural(0)

    runner_task = asyncio.create_task(
        run(
            CodexBackend(), "p", _cfg(), timeouts=_FAST, spawner=spawn,
            on_milestone=_milestone_recorder(seen),
        )
    )
    asyncio.create_task(driver())
    await runner_task

    # 50 lines well under burst threshold (200) and elapsed well under interval (2s).
    # Expect ZERO ticks for this scale.
    assert seen.count("stdout_tick") == 0


@pytest.mark.asyncio
async def test_stdout_and_stderr_ticks_emitted_independently():
    """Coalescer split: stdout activity emits `stdout_tick`; stderr activity
    emits `stderr_tick`. Splitting lets the dispatcher's pre_terminal_idle
    watchdog refresh on stdout-only and ignore stderr chatter."""
    # 250 stdout lines (passes 200-line burst) — should emit 1 stdout_tick.
    stdout_lines = (
        [b'{"type":"thread.started","thread_id":"t-1"}\n']
        + [b'{"type":"item.completed","item":{"type":"agent_message","text":"x"}}\n'] * 250
        + [b'{"type":"turn.completed"}\n']
    )
    # 250 stderr lines (passes 200-line burst) — should emit 1 stderr_tick.
    stderr_lines = [b"info: chunk\n"] * 250

    proc = FakeProc(stdout_lines, stderr_lines)
    spawn = _spawner(proc)
    seen: list[str] = []

    async def driver():
        await asyncio.sleep(0.05)
        proc.exit_natural(0)

    runner_task = asyncio.create_task(
        run(
            CodexBackend(), "p", _cfg(), timeouts=_FAST, spawner=spawn,
            on_milestone=_milestone_recorder(seen),
        )
    )
    asyncio.create_task(driver())
    await runner_task

    # Split-name ticks fired at least once from each pipe.
    assert seen.count("stdout_tick") >= 1, seen
    assert seen.count("stderr_tick") >= 1, seen


@pytest.mark.asyncio
async def test_transient_reconnect_lines_do_not_emit_stdout_tick():
    """Retry storm must not refresh the dispatcher's pre_terminal_idle
    watchdog: 250 `Reconnecting... N/M` lines (well past the 200-line burst)
    emit zero stdout_ticks, while the same volume of real events emits at
    least one. docs/bugs/codex-transient-retry-storm.md."""
    stdout_lines = (
        [b'{"type":"thread.started","thread_id":"t-1"}\n']
        + [b'{"type":"error","message":"Reconnecting... 1/5"}\n'] * 250
    )
    proc = FakeProc(stdout_lines, [])
    seen: list[str] = []

    async def driver():
        await asyncio.sleep(0.05)
        proc.exit_natural(0)

    runner_task = asyncio.create_task(
        run(
            CodexBackend(), "p", _cfg(), timeouts=_FAST, spawner=_spawner(proc),
            on_milestone=_milestone_recorder(seen),
        )
    )
    asyncio.create_task(driver())
    outcome = await runner_task

    assert seen.count("stdout_tick") == 0, seen
    # The watchdog still arms — silence detection is not disabled, only the
    # refresh is withheld.
    assert "first_codex_output" in seen
    assert len(outcome.parse_result.transient_lines) == 250


class _SpacedStdout(_FakeStdout):
    """Delivers each line after `gap` seconds — codex's ~5-minute reconnect
    cadence compressed to test scale."""

    def __init__(self, lines: list[bytes], gap: float):
        super().__init__(lines)
        self._gap = gap

    async def readline(self) -> bytes:
        if self._lines:
            await asyncio.sleep(self._gap)
        return await super().readline()


@pytest.mark.parametrize(
    ("payload", "expect_tick"),
    [
        (b'{"type":"error","message":"Reconnecting... 1/5"}\n', False),
        (b'{"type":"item.completed","item":{"type":"agent_message","text":"x"}}\n', True),
    ],
)
@pytest.mark.asyncio
async def test_time_spaced_lines_tick_only_when_tick_eligible(
    monkeypatch, payload: bytes, expect_tick: bool,
):
    """The incident's shape: one line per stream-idle cycle, far below the
    200-line burst, so only the coalescer's interval branch can fire. It must
    fire for tick-eligible lines and stay silent for reconnect chatter —
    otherwise a retry storm keeps refreshing pre_terminal_idle.
    docs/bugs/codex-transient-retry-storm.md."""
    real_coalescer = runner_mod._ActivityCoalescer
    monkeypatch.setattr(
        runner_mod, "_ActivityCoalescer",
        lambda *a, **kw: real_coalescer(*a, **{**kw, "interval_s": 0.05}),
    )
    lines = [payload] * 4
    proc = FakeProc(lines, [])
    proc.stdout = _SpacedStdout(lines, gap=0.06)
    seen: list[str] = []

    async def driver():
        await asyncio.sleep(0.4)
        proc.exit_natural(0)

    runner_task = asyncio.create_task(
        run(
            CodexBackend(), "p", _cfg(), timeouts=_FAST, spawner=_spawner(proc),
            on_milestone=_milestone_recorder(seen),
        )
    )
    asyncio.create_task(driver())
    await runner_task

    if expect_tick:
        assert seen.count("stdout_tick") >= 1, seen
    else:
        assert seen.count("stdout_tick") == 0, seen
    assert "first_codex_output" in seen


@pytest.mark.asyncio
async def test_no_milestone_callback_doesnt_break_run():
    init = b'{"type":"init","session_id":"g-1","model":"gemini-2.5-pro"}\n'
    result = b'{"type":"result","status":"ok"}\n'
    proc = FakeProc([init, result], [])
    spawn = _spawner(proc)

    async def driver():
        await asyncio.sleep(0)
        proc.exit_natural(0)

    runner_task = asyncio.create_task(
        run(GeminiBackend(), "p", _cfg(), timeouts=_FAST, spawner=spawn)
    )
    asyncio.create_task(driver())
    outcome = await runner_task
    assert outcome.parse_result.saw_terminal


# ──────────────── oversized stdout lines (real StreamReader) ────────────────
#
# These spawn a real python child: the failure mode is asyncio's own
# `readline()` limit contract, which a fake stdout cannot reproduce.
# docs/bugs/codex-stdout-bigline-truncation.md

_BIG_LINE_CHILD = r"""
import json, sys
size = int(sys.argv[1])
for ev in (
    {"type": "thread.started", "thread_id": "t-1"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "PREAMBLE"}},
    {"type": "item.completed", "item": {"type": "reasoning", "text": "x" * size}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "AFTER-BIG"}},
    {"type": "turn.completed"},
):
    sys.stdout.write(json.dumps(ev) + "\n")
sys.stdout.flush()
"""

_TWO_BIG_LINES_CHILD = r"""
import json, sys
size = int(sys.argv[1])
for ev in (
    {"type": "thread.started", "thread_id": "t-1"},
    {"type": "item.completed", "item": {"type": "reasoning", "text": "x" * size}},
    {"type": "item.completed", "item": {"type": "reasoning", "text": "y" * size}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "AFTER-BIG"}},
    {"type": "turn.completed"},
):
    sys.stdout.write(json.dumps(ev) + "\n")
sys.stdout.flush()
"""

_BIG_STDERR_CHILD = r"""
import json, sys
size = int(sys.argv[1])
sys.stderr.write("y" * size + "\n")
sys.stderr.write("stderr-after-big\n")
sys.stderr.flush()
sys.stdout.write(json.dumps({"type": "turn.completed"}) + "\n")
sys.stdout.flush()
"""


def _child_spawner(script: Path, size: int, *, limit: int | None = None):
    """Spawn the child through the production path (`_default_spawn`) unless a
    `limit` override is given — overrides let a test hit the degrade path
    without generating megabytes."""

    async def factory(*_args, **kwargs):
        kwargs.pop("cwd", None)
        argv = (sys.executable, "-u", str(script), str(size))
        if limit is None:
            return await runner_mod._default_spawn(*argv, **kwargs)
        return await asyncio.create_subprocess_exec(*argv, limit=limit, **kwargs)

    return factory


def _write_child(tmp_path: Path, source: str) -> Path:
    script = tmp_path / "big_line_child.py"
    script.write_text(source, encoding="utf-8")
    return script


async def _run_child(tmp_path: Path, source: str, size: int, *, limit: int | None = None):
    # Bounded: a reader that dies on the oversized line leaves the child
    # blocked on a full pipe forever, which is a hang, not an assertion error.
    return await asyncio.wait_for(
        run(
            CodexBackend(), "p", _cfg(),
            timeouts=_FAST,
            spawner=_child_spawner(_write_child(tmp_path, source), size, limit=limit),
        ),
        timeout=20,
    )


@pytest.mark.asyncio
async def test_default_spawn_raises_stream_limit_above_asyncio_default(monkeypatch):
    """asyncio's 64 KiB default truncated a single codex `aggregated_output`
    line (232 KB measured). The limit is pinned to the dispatcher's
    STDOUT_FRAME_CAP."""
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(runner_mod, "_resolve_command", lambda c: c)
    await runner_mod._default_spawn("codex", "exec")
    assert captured["limit"] == 4 * 1024 * 1024


@pytest.mark.asyncio
async def test_line_over_asyncio_default_limit_is_parsed(tmp_path):
    """A 200 KB line — past asyncio's 64 KiB default, inside the new limit —
    parses normally, and everything after it still arrives."""
    outcome = await _run_child(tmp_path, _BIG_LINE_CHILD, 200_000)
    pr = outcome.parse_result
    assert pr.saw_terminal is True
    assert pr.agent_messages == "PREAMBLE\nAFTER-BIG"
    assert len(pr.all_messages) == 5
    assert pr.noise_log == []


@pytest.mark.asyncio
async def test_line_past_stream_limit_degrades_without_stopping_the_stream(tmp_path):
    """Beyond any fixed limit (`aggregated_output` has no upper bound), the
    oversized line is dropped with a noise_log trace — the reader must NOT
    stop, so the terminal event still lands."""
    outcome = await _run_child(tmp_path, _BIG_LINE_CHILD, 200_000, limit=8192)
    pr = outcome.parse_result
    assert pr.saw_terminal is True
    assert pr.agent_messages == "PREAMBLE\nAFTER-BIG"
    assert any("dropped oversized stdout line" in n for n in pr.noise_log), pr.noise_log


@pytest.mark.asyncio
async def test_preamble_before_oversized_line_is_not_returned_as_complete(tmp_path):
    """The dangerous path: codex emits a chatty preamble before the shell tool
    whose output blows the limit. A dead reader still yields a non-empty
    `agent_messages` (preamble only) with no terminal event — `empty_final`
    cannot catch that, and the caller sees a truncated answer as success."""
    outcome = await _run_child(tmp_path, _BIG_LINE_CHILD, 200_000, limit=65536)
    pr = outcome.parse_result
    assert pr.agent_messages != "PREAMBLE"
    assert "AFTER-BIG" in pr.agent_messages
    assert pr.saw_terminal is True


@pytest.mark.asyncio
async def test_each_oversized_line_leaves_its_own_trace(tmp_path):
    """Back-to-back oversized lines must each register: the drop trace doubles
    as the stdout-activity signal, so collapsing them would let a stream that
    is still producing output read as idle to `pre_terminal_idle`."""
    outcome = await _run_child(tmp_path, _TWO_BIG_LINES_CHILD, 200_000, limit=8192)
    pr = outcome.parse_result
    drops = [n for n in pr.noise_log if "dropped oversized stdout line" in n]
    assert len(drops) >= 2, pr.noise_log
    assert pr.agent_messages == "AFTER-BIG"
    assert pr.saw_terminal is True


@pytest.mark.asyncio
async def test_persistently_raising_reader_stops_claiming_activity():
    """The drop trace doubles as an activity bump. A reader that raises without
    consuming must not be able to refresh `pre_terminal_idle` indefinitely —
    reading continues, the claim of progress does not."""

    class _AlwaysRaises:
        def __init__(self, times: int):
            self.left = times

        async def readline(self) -> bytes:
            if self.left:
                self.left -= 1
                raise ValueError("Separator is not found, and chunk exceed the limit")
            return b""

    drops: list[str] = []
    reader = _AlwaysRaises(500)
    lines = [line async for line in runner_mod._iter_lines(reader, drops.append)]

    assert lines == []
    assert len(drops) == runner_mod._MAX_CONSECUTIVE_DROPS


@pytest.mark.asyncio
async def test_oversized_stderr_line_does_not_stop_stderr_stream(tmp_path):
    """Same contract on stderr: the tail buffer keeps filling after an
    oversized line, with the drop recorded in place."""
    outcome = await _run_child(tmp_path, _BIG_STDERR_CHILD, 200_000, limit=8192)
    assert "stderr-after-big" in outcome.stderr_tail
    assert "dropped oversized stderr line" in outcome.stderr_tail


def test_resolve_command_raises_for_missing():
    with pytest.raises(FileNotFoundError):
        _resolve_command("definitely-not-a-real-cli-xyzzy-aoeuh")


def test_runtimeouts_from_env_default():
    t = RunTimeouts.from_env({})
    assert t.codex_exit_drain == 5.0


def test_runtimeouts_from_env_override():
    t = RunTimeouts.from_env({"PRISM_CODEX_EXIT_DRAIN": "0.5"})
    assert t.codex_exit_drain == 0.5
