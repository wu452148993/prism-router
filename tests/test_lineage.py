"""Lineage side channel (docs/lineage.md): row shape and timing through the
registered tools, client-visible invariance, failure containment, the parent
walk, and multi-process appends to one file (request §6 item 6)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import types
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastmcp import Client

from prism_router import __version__, lineage
from prism_router import server as server_mod
from prism_router.config import ServerConfig
from prism_router.dispatcher import DispatchResult
from prism_router.errors import make_error
from prism_router.parser import ParseResult
from prism_router.paths import normalize_cwd
from prism_router.runner import RunOutcome

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="lineage is Windows-only")

PARENT = (4242, "2026-10-07T03:02:11.120Z", "claude.exe")
TS = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")
COMMON = [
    "v", "event", "ts", "call_id", "backend", "cwd", "project_dir", "prism_pid",
    "prism_version", "parent_session_id_env", "parent_pid", "parent_pid_start",
    "parent_exe", "requested_session_id", "child_session_id", "resumed",
]


@pytest.fixture
def lineage_dir(tmp_path, monkeypatch) -> Path:
    """Lineage on, writing under tmp_path, with a fixed parent identity."""
    monkeypatch.setattr(lineage, "_ctx", None)
    monkeypatch.setattr(lineage, "_parent", lambda: PARENT)
    directory = tmp_path / "lineage"
    lineage.configure({
        "PRISM_LINEAGE_DIR": str(directory),
        "CLAUDE_CODE_SESSION_ID": "parent-sid",
        "CLAUDE_PROJECT_DIR": "D:\\proj",
    })
    return directory


@pytest.fixture
def mcp(monkeypatch):
    monkeypatch.setattr(server_mod, "smoke_probe", lambda cmd, **_: True)
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
    monkeypatch.setattr(server_mod, "_GEMINI_TRANSIENT_RETRY_DELAY_S", 0.0)
    return server_mod.build_server(ServerConfig.from_env({}))


def _rows(directory: Path) -> list[dict]:
    return [
        json.loads(line)
        for f in sorted(directory.glob("*.jsonl"))
        for line in f.read_text(encoding="utf-8").splitlines()
    ]


def _outcome(sid: str | None, msg: str = "ok") -> RunOutcome:
    return RunOutcome(
        parse_result=ParseResult(
            session_id=sid, agent_messages=msg, all_messages=[], noise_log=[],
            transient_lines=[], saw_terminal=True, error_events=[],
        ),
        stderr_tail="", kill_chain=[], returncode=0, timed_out=False,
    )


def _failed(code: str) -> DispatchResult:
    return DispatchResult(outcome=None, error=make_error(code, "shutdown", retryable=True, raw="r"))


def _dispatch(monkeypatch, *steps, on_invoke=None) -> list:
    """Stand-in Dispatcher: dispatch i feeds steps[i][0] (unless None) to the
    `on_child` it was constructed with, then returns steps[i][1]. Returns the
    `on_child` each construction received."""
    observers: list = []
    queue = list(steps)

    class _Fake:
        def __init__(self, **kwargs):
            self._on_child = kwargs.get("on_child")
            observers.append(self._on_child)

        async def invoke(self, *, backend_name, prompt, cfg):
            if on_invoke is not None:
                on_invoke()
            child, result = queue.pop(0)
            if child is not None and self._on_child is not None:
                self._on_child(child)
            return result

    monkeypatch.setattr(server_mod, "Dispatcher", _Fake)
    return observers


async def _call(mcp, tool: str, **args):
    async with Client(mcp) as client:
        return await client.call_tool_mcp(tool, args)


# ---------- rows through the registered tools ----------

async def test_three_rows_carry_every_field_and_land_in_order(lineage_dir, monkeypatch, mcp, tmp_path):
    rows_at_dispatch: list[list[dict]] = []
    _dispatch(
        monkeypatch, ("sid-1", DispatchResult(outcome=_outcome("sid-1"))),
        on_invoke=lambda: rows_at_dispatch.append(_rows(lineage_dir)),
    )
    await _call(mcp, "codex", PROMPT="hi", cd=str(tmp_path))

    start, child, end = _rows(lineage_dir)
    assert [r["event"] for r in rows_at_dispatch[0]] == ["start"]  # on disk before dispatch
    assert list(start) == list(child) == COMMON
    assert list(end) == COMMON + ["outcome", "error_code", "duration_ms"]
    for row in (start, child, end):
        assert row["v"] == 1 and row["backend"] == "codex" and TS.fullmatch(row["ts"])
        assert row["call_id"] == start["call_id"]
        assert row["cwd"] == str(normalize_cwd(tmp_path))
        assert (row["project_dir"], row["parent_session_id_env"]) == ("D:\\proj", "parent-sid")
        assert (row["parent_pid"], row["parent_pid_start"], row["parent_exe"]) == PARENT
        assert (row["prism_pid"], row["prism_version"]) == (os.getpid(), __version__)
        assert row["requested_session_id"] is None
    assert (start["child_session_id"], start["resumed"]) == (None, None)
    assert (child["child_session_id"], child["resumed"]) == ("sid-1", False)
    assert (end["child_session_id"], end["resumed"]) == ("sid-1", False)
    assert (end["outcome"], end["error_code"]) == ("success", None)
    assert isinstance(end["duration_ms"], int) and end["duration_ms"] >= 0
    assert start["ts"] <= child["ts"] <= end["ts"]
    assert [f.name for f in lineage_dir.iterdir()] == [f"{start['ts'][:7]}.jsonl"]


@pytest.mark.parametrize("tool", ["codex", "gemini"])
@pytest.mark.parametrize(
    ("returned", "resumed"), [("S1", True), ("S2", False)],
    ids=["resumed", "replaced"],
)
async def test_resume_is_judged_against_the_requested_id(
    lineage_dir, monkeypatch, mcp, tmp_path, tool, returned, resumed,
):
    observers = _dispatch(monkeypatch, (returned, DispatchResult(outcome=_outcome(returned))))
    await _call(mcp, tool, PROMPT="hi", cd=str(tmp_path), SESSION_ID="S1")
    assert observers and all(observers), "the tool body or _invoke_* dropped on_child"
    _, child, end = _rows(lineage_dir)
    for row in (child, end):
        assert (row["requested_session_id"], row["child_session_id"], row["resumed"]) == (
            "S1", returned, resumed,
        )


@pytest.mark.parametrize("tool", ["codex", "gemini"])
async def test_backend_that_never_started_leaves_start_and_end_only(
    lineage_dir, monkeypatch, mcp, tmp_path, tool,
):
    _dispatch(monkeypatch, (None, _failed("spawn_failed")))
    await _call(mcp, tool, PROMPT="hi", cd=str(tmp_path))
    start, end = _rows(lineage_dir)
    assert (start["event"], end["event"]) == ("start", "end")
    assert (end["child_session_id"], end["resumed"]) == (None, None)
    assert (end["outcome"], end["error_code"]) == ("error", "spawn_failed")


async def test_pre_spawn_failure_is_recorded_without_any_dispatch(lineage_dir, monkeypatch, mcp, tmp_path):
    observers = _dispatch(monkeypatch)
    await _call(mcp, "codex", PROMPT="hi", cd=str(tmp_path), prompt_file="missing.md")
    start, end = _rows(lineage_dir)
    assert observers == []
    assert (end["outcome"], end["error_code"], end["child_session_id"]) == (
        "error", "prompt_file_error", None,
    )


@pytest.mark.parametrize(
    ("code", "outcome"),
    [
        ("timeout", "timeout"),
        ("pre_terminal_idle", "timeout"),
        ("post_terminal_grace", "timeout"),
        ("transport_failure", "error"),
    ],
)
async def test_failed_envelope_maps_to_outcome_and_keeps_the_observed_child(
    lineage_dir, monkeypatch, mcp, tmp_path, code, outcome,
):
    _dispatch(monkeypatch, ("sid-1", _failed(code)))
    await _call(mcp, "codex", PROMPT="hi", cd=str(tmp_path))
    _, child, end = _rows(lineage_dir)
    assert (end["outcome"], end["error_code"], end["child_session_id"]) == (outcome, code, "sid-1")


@pytest.mark.parametrize("second", ["g-2", None], ids=["retry-succeeds", "retry-never-starts"])
async def test_gemini_retry_writes_one_child_row_and_ends_on_the_last_known_id(
    lineage_dir, monkeypatch, mcp, tmp_path, second,
):
    _dispatch(
        monkeypatch,
        ("g-1", DispatchResult(outcome=_outcome("g-1", msg=""))),  # empty final → retried
        (second, _failed("spawn_failed") if second is None else DispatchResult(outcome=_outcome(second))),
    )
    result = await _call(mcp, "gemini", PROMPT="hi", cd=str(tmp_path))
    _, child, end = _rows(lineage_dir)
    assert child["child_session_id"] == "g-1"
    assert end["child_session_id"] == (second or "g-1")
    assert end["outcome"] == ("success" if second else "error")
    assert result.structuredContent["SESSION_ID"] == second


async def test_missed_milestone_backfills_the_child_row_before_end(lineage_dir, monkeypatch, mcp, tmp_path):
    _dispatch(monkeypatch, (None, DispatchResult(outcome=_outcome("sid-late"))))
    await _call(mcp, "codex", PROMPT="hi", cd=str(tmp_path))
    _, child, end = _rows(lineage_dir)
    assert (child["event"], child["child_session_id"]) == ("child", "sid-late")
    assert end["child_session_id"] == "sid-late"


# ---------- the client sees nothing ----------

async def test_lineage_never_changes_what_the_client_receives(monkeypatch, tmp_path):
    """Same calls under every lineage state: identical tool surface,
    instructions and raw MCP results; rows only where writable."""
    monkeypatch.setattr(server_mod, "smoke_probe", lambda cmd, **_: True)
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
    monkeypatch.setattr(lineage, "_ctx", None)
    monkeypatch.setattr(lineage, "_parent", lambda: PARENT)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    states = {
        "unconfigured": None,
        "on": {"PRISM_LINEAGE_DIR": str(tmp_path / "on")},
        "unwritable": {"PRISM_LINEAGE_DIR": str(blocker / "lineage")},
        "off": {"PRISM_LINEAGE": "0", "PRISM_LINEAGE_DIR": str(tmp_path / "off")},
    }
    seen = {}
    for name, env in states.items():
        if env is not None:
            lineage.configure(env)
        _dispatch(
            monkeypatch,
            ("sid-1", DispatchResult(outcome=_outcome("sid-1", "answer"))),
            ("sid-2", _failed("pre_terminal_idle")),
        )
        mcp = server_mod.build_server(ServerConfig.from_env({}))
        async with Client(mcp) as client:
            tools = [t.model_dump_json() for t in await client.list_tools()]
            ok = await client.call_tool_mcp("codex", {"PROMPT": "hi", "cd": str(tmp_path)})
            bad = await client.call_tool_mcp(
                "gemini", {"PROMPT": "hi", "cd": str(tmp_path), "SESSION_ID": "S1"},
            )
        seen[name] = (mcp.instructions, tools, ok.model_dump_json(), bad.model_dump_json())

    assert all(v == seen["unconfigured"] for v in seen.values())
    assert [r["event"] for r in _rows(tmp_path / "on")] == ["start", "child", "end"] * 2
    assert not (tmp_path / "off").exists()


@pytest.mark.parametrize("exc", [OSError("disk full"), RuntimeError("bug")])
async def test_write_failures_never_reach_the_call(lineage_dir, monkeypatch, mcp, tmp_path, caplog, exc):
    def explode(path, data):
        raise exc

    monkeypatch.setattr(lineage, "_append", explode)
    _dispatch(monkeypatch, ("sid-1", DispatchResult(outcome=_outcome("sid-1"))))
    with caplog.at_level(logging.DEBUG, logger="prism_router.lineage"):
        result = await _call(mcp, "codex", PROMPT="hi", cd=str(tmp_path))
    assert result.isError is False and result.structuredContent["success"] is True
    assert caplog.text.count("row not written") == 3


async def test_oversize_rows_are_dropped_as_anomalies(lineage_dir, monkeypatch, mcp, tmp_path, caplog):
    _dispatch(monkeypatch, (None, DispatchResult(outcome=_outcome("sid-1"))))
    with caplog.at_level(logging.WARNING, logger="prism_router.lineage"):
        result = await _call(
            mcp, "codex", PROMPT="hi", cd=str(tmp_path), SESSION_ID="s" * lineage.MAX_LINE_BYTES,
        )
    assert result.structuredContent["success"] is True
    assert _rows(lineage_dir) == []
    assert caplog.text.count("dropped") == 3


# ---------- track() exception paths ----------

async def test_cancel_and_errors_reraise_after_their_end_row(lineage_dir, tmp_path):
    started = asyncio.Event()

    async def hang(child):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(lineage.track("codex", tmp_path, "", hang))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    boom = RuntimeError("boom")

    async def fail(child):
        raise boom

    with pytest.raises(RuntimeError) as raised:
        await lineage.track("gemini", tmp_path, "", fail)
    assert raised.value is boom

    rows = _rows(lineage_dir)
    assert [r["event"] for r in rows] == ["start", "end", "start", "end"]
    assert [(r["backend"], r["outcome"], r["error_code"]) for r in rows[1::2]] == [
        ("codex", "cancelled", None), ("gemini", "error", None),
    ]


def test_interpreter_teardown_ends_as_killed(lineage_dir, tmp_path):
    @types.coroutine
    def suspend():
        yield

    async def invoke(child):
        await suspend()

    coro = lineage.track("codex", tmp_path, "", invoke)
    coro.send(None)
    with pytest.raises(KeyboardInterrupt):
        coro.throw(KeyboardInterrupt())
    _, end = _rows(lineage_dir)
    assert (end["outcome"], end["error_code"]) == ("killed", None)


async def test_lineage_off_passes_no_observer(monkeypatch, tmp_path):
    monkeypatch.setattr(lineage, "_ctx", None)
    received = []

    async def invoke(child):
        received.append(child)
        return {"success": True}

    assert await lineage.track("codex", tmp_path, "", invoke) == {"success": True}
    assert received == [None]


async def test_recorder_failures_never_replace_the_result_or_the_exception(
    lineage_dir, monkeypatch, tmp_path,
):
    """A failing write behind a failing DEBUG handler, and a `cd` the recorder
    cannot normalize, all stay inside the recorder."""
    def explode(*_args, **_kwargs):
        raise RuntimeError("recorder exploded")

    monkeypatch.setattr(lineage, "_append", explode)
    monkeypatch.setattr(lineage._LOG, "debug", explode)
    envelope = {"success": True, "SESSION_ID": "sid-1", "error": None}

    async def succeed(child):
        if child is not None:
            child("sid-1")
        return envelope

    assert await lineage.track("codex", tmp_path, "", succeed) is envelope
    assert await lineage.track("codex", "nul\x00path", "", succeed) is envelope

    original = ValueError("original")

    async def fail(child):
        raise original

    with pytest.raises(ValueError) as raised:
        await lineage.track("codex", tmp_path, "", fail)
    assert raised.value is original


async def test_a_child_reported_after_end_is_ignored(lineage_dir, tmp_path):
    """A stderr drain the dispatcher leaves running past a cancel must not
    append to the closed call."""
    observers = []

    async def invoke(child):
        observers.append(child)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await lineage.track("codex", tmp_path, "", invoke)
    observers[0]("sid-late")
    rows = _rows(lineage_dir)
    assert [r["event"] for r in rows] == ["start", "end"]
    assert (rows[1]["outcome"], rows[1]["child_session_id"]) == ("cancelled", None)


# ---------- configuration ----------

def test_configure_reads_the_knobs_once(monkeypatch, tmp_path):
    monkeypatch.setattr(lineage, "_ctx", None)
    monkeypatch.setattr(lineage, "_parent", lambda: PARENT)
    lineage.configure({"PRISM_LINEAGE": "0", "PRISM_LINEAGE_DIR": str(tmp_path)})
    assert lineage._ctx is None
    lineage.configure({})
    assert lineage._ctx.directory == Path.home() / ".prism" / "lineage"
    assert lineage._ctx.fields["project_dir"] is None
    assert lineage._ctx.fields["parent_session_id_env"] is None
    lineage.configure({"PRISM_LINEAGE_DIR": str(tmp_path)})
    assert lineage._ctx.directory == tmp_path


def test_main_configures_lineage_before_serving(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(lineage, "configure", lambda: order.append("configure"))
    monkeypatch.setattr(
        server_mod, "build_server",
        lambda cfg: types.SimpleNamespace(run=lambda: order.append("run")),
    )
    monkeypatch.setattr(server_mod.logging, "basicConfig", lambda **_: None)
    assert server_mod.main() == 0
    assert order == ["configure", "run"]


# ---------- parent walk ----------

def _procs(*rows):
    """rows: (pid, ppid, exe, created) → (table, created lookup)."""
    return {p: (pp, exe) for p, pp, exe, _ in rows}, {p: c for p, _, _, c in rows}.get


def test_parent_walk_climbs_launchers_to_claude():
    table, created = _procs(
        (50, 40, "python.exe", 500), (40, 30, "python.exe", 400),
        (30, 20, "prism-router.exe", 300), (20, 10, "uv.exe", 200),
        (10, 1, "Claude.EXE", 100), (1, 0, "explorer.exe", 1),
    )
    assert lineage._find_parent(50, table, created) == (10, lineage._filetime_iso(100), "Claude.EXE")


def test_parent_walk_stops_at_a_reused_pid():
    table, created = _procs(
        (50, 40, "python.exe", 500), (40, 30, "python.exe", 400), (30, 20, "claude.exe", 450),
    )
    assert lineage._find_parent(50, table, created) == (40, lineage._filetime_iso(400), "python.exe")


@pytest.mark.parametrize(
    ("claude_created", "expected"),
    [(450, (30, None, "claude.exe")), (550, (40, None, "python.exe"))],
    ids=["unverifiable", "provably-reused"],
)
def test_an_unreadable_link_keeps_the_age_bound_and_voids_certainty(claude_created, expected):
    table, created = _procs(
        (50, 40, "python.exe", 500), (40, 30, "python.exe", None),
        (30, 20, "claude.exe", claude_created),
    )
    assert lineage._find_parent(50, table, created) == expected


def test_parent_walk_without_claude_records_the_direct_parent():
    table, created = _procs(
        (50, 40, "python.exe", 500), (40, 30, "prism-router.exe", 400), (30, 99, "uv.exe", 300),
    )
    assert lineage._find_parent(50, table, created) == (
        40, lineage._filetime_iso(400), "prism-router.exe",
    )


def test_parent_walk_survives_cycles_unreadable_times_and_a_missing_self():
    cycle = {50: (40, "python.exe"), 40: (50, "python.exe")}
    assert lineage._find_parent(50, cycle, lambda pid: None) == (40, None, "python.exe")
    assert lineage._find_parent(50, {}, lambda pid: None) == (os.getppid(), None, None)


def test_real_parent_walk_and_creation_time():
    table = lineage._process_table()
    assert table[os.getpid()][0] == os.getppid() and table[os.getpid()][1]
    pid, start, exe = lineage._parent()
    assert isinstance(pid, int) and pid > 0
    assert exe is None or isinstance(exe, str)
    assert start is None or TS.fullmatch(start)
    born = datetime.fromisoformat(lineage._filetime_iso(lineage._created(os.getpid()))[:-1])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert now - timedelta(days=1) < born <= now


# ---------- request §6 item 6: concurrent appends to one file ----------

_WRITER = """
import sys, time
from pathlib import Path
from prism_router.lineage import _append
path, tag, count, gate = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), Path(sys.argv[4])
Path(f"{gate}.{tag}").touch()
while not gate.exists():
    time.sleep(0.001)
for seq in range(count):
    _append(path, ('{"w":"%s","seq":%d,"pad":"%s"}\\n' % (tag, seq, "x" * (200 + seq % 700))).encode())
"""


@contextlib.contextmanager
def _writers(path: Path, plan: dict[str, int]):
    """One writer process per tag, released together once every one has
    imported and is waiting at the gate; all are killed on exit."""
    gate = path.with_name("go")
    procs: dict[str, subprocess.Popen] = {}
    try:
        for tag, n in plan.items():
            procs[tag] = subprocess.Popen(
                [sys.executable, "-c", _WRITER, str(path), tag, str(n), str(gate)],
            )
        deadline = time.monotonic() + 60
        while not all(Path(f"{gate}.{tag}").exists() for tag in plan):
            assert time.monotonic() < deadline, "writers never reached the gate"
            time.sleep(0.01)
        gate.touch()
        yield procs
    finally:
        for p in procs.values():
            p.kill()
        for p in procs.values():
            p.wait(timeout=30)


def _read_back(path: Path) -> tuple[list[str], dict[str, list[int]], list[bytes]]:
    """Writer tags in file order, each writer's seqs, and unparseable lines
    (an unterminated tail included)."""
    *lines, tail = path.read_bytes().split(b"\n")
    order: list[str] = []
    seqs: dict[str, list[int]] = defaultdict(list)
    bad = [tail] if tail else []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            bad.append(line)
            continue
        order.append(row["w"])
        seqs[row["w"]].append(row["seq"])
    return order, seqs, bad


def test_two_concurrent_writers_of_1000_lines_each_lose_and_tear_nothing(tmp_path):
    path = tmp_path / "shared.jsonl"
    with _writers(path, {"a": 1000, "b": 1000}) as procs:
        assert [p.wait(timeout=120) for p in procs.values()] == [0, 0]
    order, seqs, bad = _read_back(path)
    assert sum(x != y for x, y in zip(order, order[1:])) >= 10, "the writers never overlapped"
    assert bad == []
    assert seqs == {"a": list(range(1000)), "b": list(range(1000))}


def test_killing_one_writer_tears_at_most_its_own_last_line(tmp_path):
    path = tmp_path / "shared.jsonl"
    with _writers(path, {"victim": 1_000_000, "survivor": 1000}) as procs:
        deadline = time.monotonic() + 60
        while not path.exists() or path.read_bytes().count(b'"w":"victim"') < 300:
            assert time.monotonic() < deadline, "victim never got going"
            time.sleep(0.01)
        procs["victim"].kill()
        procs["victim"].wait(timeout=30)
        assert procs["survivor"].wait(timeout=120) == 0
    _, seqs, bad = _read_back(path)
    assert seqs["survivor"] == list(range(1000))
    assert len(seqs["victim"]) >= 299, "the victim markers were not complete rows"
    assert seqs["victim"] == list(range(len(seqs["victim"])))
    assert len(bad) <= 1
