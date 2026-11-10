"""Protocol gates for the liveness progress egress, against the REAL MCP SDK.

These drive `mcp.server.lowlevel.Server.run` over memory streams so every
frame — and its ORDER — is observable. Two claims:

- the response is written only after the `aclose` barrier settles, and no
  notification frame trails it (blocked-writer test);
- the mandatory H1 gate: with an in-flight progress send held open, a
  `notifications/cancelled` must not be followed by a progress notification.
  Run against the EXACT spawn mechanism `LivenessEgress` ships with.

A negative control models the rejected pre-H1 mechanism and proves the gate
can fail.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools

import anyio
import mcp.types as types
import pytest
from fastmcp import FastMCP
from mcp.shared.message import SessionMessage

from prism_router import server as server_mod
from prism_router._liveness import LivenessEgress

RECV_TIMEOUT_S = 5.0
SHIELD_MAX_S = 5.0
PROGRESS_TOKEN = 7


def _frame(payload: dict) -> SessionMessage:
    return SessionMessage(types.JSONRPCMessage.model_validate(payload))


class _RawClient:
    """Hand-rolled JSON-RPC peer: no client SDK between us and the wire."""

    def __init__(self, to_server, from_server) -> None:
        self._to_server = to_server
        self._from_server = from_server

    async def send(self, payload: dict) -> None:
        await self._to_server.send(_frame(payload))

    async def receive(self, timeout: float = RECV_TIMEOUT_S) -> dict:
        with anyio.fail_after(timeout):
            message = await self._from_server.receive()
        return message.message.model_dump(by_alias=True, exclude_none=True)

    async def receive_none(self, window: float = 0.3) -> None:
        """Assert the wire stays quiet for `window` seconds."""
        with anyio.move_on_after(window):
            extra = await self._from_server.receive()
            raise AssertionError(f"unexpected trailing frame: {extra.message}")

    async def handshake(self) -> None:
        await self.send({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": types.LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "prism-test", "version": "0"},
            },
        })
        await self.receive()
        await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def call_tool(self, name: str, request_id: int = 1) -> None:
        await self.send({
            "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {
                "name": name, "arguments": {},
                "_meta": {"progressToken": PROGRESS_TOKEN},
            },
        })


@contextlib.asynccontextmanager
async def _connected(mcp: FastMCP, *, write_buffer: int):
    to_server_send, to_server_recv = anyio.create_memory_object_stream(10)
    to_client_send, to_client_recv = anyio.create_memory_object_stream(write_buffer)
    srv = mcp._mcp_server
    async with anyio.create_task_group() as tg:
        tg.start_soon(functools.partial(
            srv.run, to_server_recv, to_client_send,
            srv.create_initialization_options(), raise_exceptions=False,
        ))
        client = _RawClient(to_server_send, to_client_recv)
        await client.handshake()
        try:
            yield client, to_client_send
        finally:
            tg.cancel_scope.cancel()


def _probe_server(invoke) -> FastMCP:
    mcp = FastMCP(name="liveness-probe")

    @mcp.tool
    async def probe() -> str:
        return await server_mod._run_with_liveness(True, invoke)

    return mcp


def _is_progress(frame: dict) -> bool:
    return frame.get("method") == "notifications/progress"


class _RecordingEgress(LivenessEgress):
    """Exposes the request's egress so a test can inspect its state, or reach
    its `_spawn` (i.e. the tool's own task group)."""

    made: list["_RecordingEgress"] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _RecordingEgress.made.append(self)


@pytest.fixture
def recording_egress(monkeypatch):
    """Installs `_RecordingEgress` for tests that need the request's egress."""
    _RecordingEgress.made = []
    monkeypatch.setattr(server_mod, "LivenessEgress", _RecordingEgress)


async def test_response_is_written_only_after_the_barrier_settles(recording_egress):
    """Blocked writer (buffer 0): the tool's invoke has already returned, so
    only `aclose` is holding the handler. The response must NOT be on the wire
    while the progress send is still parked, and the progress frame must come
    out first.

    The trailing `receive_none` is a BOUNDED quiet-window probe, not a proof of
    absence; the structural assertion (egress closed, no live flight, once the
    response has landed) is what rules out a still-armed sender.
    """
    returned = asyncio.Event()

    async def invoke(note):
        note("stdout")
        returned.set()
        return "envelope"

    async with _connected(
        _probe_server(invoke), write_buffer=0,
    ) as (client, writer):
        await client.call_tool("probe")

        # Poll rather than sleep a fixed slice: a stalled runner should fail
        # this honestly, not flake it.
        with anyio.fail_after(RECV_TIMEOUT_S):
            await returned.wait()
            while writer.statistics().tasks_waiting_send < 1:
                await anyio.sleep(0.01)

        stats = writer.statistics()
        assert stats.tasks_waiting_send == 1, (
            "invoke has returned, so only aclose is holding: the ONE parked "
            "sender must be the notification. A response queued here would "
            "mean the barrier did not hold."
        )
        assert stats.current_buffer_used == 0

        first = await client.receive()
        assert _is_progress(first)
        assert first["params"]["progressToken"] == PROGRESS_TOKEN
        assert first["params"]["progress"] == 1
        assert first["params"]["message"].startswith("stdout activity")

        second = await client.receive()
        assert second["id"] == 1
        assert "result" in second

        egress = _RecordingEgress.made[-1]
        assert egress._closed and egress._flight is None, (
            "no sender may remain armed once the response is on the wire"
        )
        await client.receive_none()


class _DetachedEgress(LivenessEgress):
    """The REJECTED pre-H1 mechanism: sends spawned OUTSIDE the request-scoped
    task group. Only the spawn differs, so the control isolates exactly the one
    variable H1 is about."""

    keep: set = set()

    def __init__(self, sink, *, spawn, **kwargs):
        def detached(fn, flight):
            task = asyncio.create_task(fn(flight))
            _DetachedEgress.keep.add(task)
            task.add_done_callback(_DetachedEgress.keep.discard)

        super().__init__(sink, spawn=detached, **kwargs)


class _CancelScenario:
    """Shared H1 scenario.

    The handler parks inside a SHIELDED cancel scope, so its cleanup (and with
    it `abort()`) provably cannot run while the cancellation is processed. That
    isolates the STRUCTURAL claim — "sends are spawned into the request's
    cancellation scope" — from the racy handler-cleanup path, which happens to
    win under a plain `await` and would otherwise mask a broken mechanism.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        self.shielded = asyncio.Event()
        self.unwound = asyncio.Event()
        self.sink_outcome: list[str] = []

    def sink_factory(self, real_factory):
        def factory():
            inner = real_factory()
            assert inner is not None, "a fastmcp Context must be active here"

            async def sink(progress: float, message: str) -> None:
                self.entered.set()
                try:
                    await self.gate.wait()
                except asyncio.CancelledError:
                    self.sink_outcome.append("cancelled")
                    raise
                self.sink_outcome.append("sent")
                await inner(progress, message)

            return sink
        return factory

    async def invoke(self, note):
        note("stdout")
        try:
            # Hard-bounded shield: a failing assertion must never leave an
            # uncancellable handler wedged in the session teardown.
            with anyio.CancelScope(shield=True), anyio.move_on_after(SHIELD_MAX_S):
                await self.shielded.wait()
            await asyncio.Event().wait()
            return "unreachable"  # pragma: no cover
        finally:
            self.unwound.set()

    async def drive(self, client) -> dict:
        """Call the tool, cancel it mid-send, return the cancellation frame."""
        await client.call_tool("probe")
        with anyio.fail_after(RECV_TIMEOUT_S):
            await self.entered.wait()
        await client.send({
            "jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": 1},
        })
        response = await client.receive()
        assert response["id"] == 1
        assert "error" in response, response
        assert response["error"]["message"] == "Request cancelled"
        assert not self.unwound.is_set(), (
            "the handler must still be shielded — otherwise this test is only "
            "measuring the cleanup race, not the structural guarantee"
        )
        self.gate.set()
        return response


async def test_no_progress_notification_follows_the_cancellation_response(
    monkeypatch,
):
    """MANDATORY H1 gate, against the EXACT shipped spawn mechanism.

    An in-flight progress send is held before it reaches the write stream;
    `notifications/cancelled` arrives; the SDK sends its cancellation error
    response INDEPENDENTLY of handler cleanup. With the handler shielded, the
    only thing that can stop the send is the request cancel scope reaching it.

    The quiet window is a BOUNDED probe; the load-bearing assertion is that the
    send was CANCELLED — a cancelled sender cannot write later, however long
    one waits.
    """
    scenario = _CancelScenario()
    monkeypatch.setattr(
        server_mod, "_progress_sink",
        scenario.sink_factory(server_mod._progress_sink),
    )

    async with _connected(
        _probe_server(scenario.invoke), write_buffer=10,
    ) as (client, _w):
        try:
            await scenario.drive(client)
            assert scenario.sink_outcome == ["cancelled"], (
                "the request scope must have cancelled the in-flight send "
                "while the handler was still shielded"
            )
            await client.receive_none(window=0.4)
        finally:
            scenario.shielded.set()  # let the handler unwind for teardown


async def test_negative_control_a_scope_free_send_writes_after_the_response(
    monkeypatch,
):
    """Control: the identical scenario with ONLY the spawn mechanism swapped
    for the rejected detached one. A progress frame lands AFTER the SDK's
    cancellation error response — proving the gate above can fail and is not
    passing vacuously."""
    scenario = _CancelScenario()
    monkeypatch.setattr(
        server_mod, "_progress_sink",
        scenario.sink_factory(server_mod._progress_sink),
    )
    monkeypatch.setattr(server_mod, "LivenessEgress", _DetachedEgress)

    try:
        async with _connected(
            _probe_server(scenario.invoke), write_buffer=10,
        ) as (client, _w):
            try:
                await scenario.drive(client)
                late = await client.receive()
                assert _is_progress(late), late
                assert scenario.sink_outcome == ["sent"]
            finally:
                scenario.shielded.set()
    finally:
        for task in list(_DetachedEgress.keep):
            task.cancel()
        _DetachedEgress.keep.clear()


async def test_progress_reaches_the_wire_from_a_nested_dispatcher_task():
    """Production never calls `note()` from the tool body: `_drain_stderr` and
    the idle watchdog are nested `asyncio.create_task` children, and the send
    task therefore inherits THEIR contextvars. `report_progress` resolves the
    progressToken through `request_ctx`, so this is the path that decides
    whether the feature works at all in production."""
    async def invoke(note):
        async def drain():  # stands in for Dispatcher's _drain_stderr task
            async def watchdog():  # ... and its nested watchdog task
                note("rollout")
            await asyncio.create_task(watchdog())

        await asyncio.create_task(drain())
        return "envelope"

    async with _connected(_probe_server(invoke), write_buffer=10) as (client, _w):
        await client.call_tool("probe")
        first = await client.receive()
        assert _is_progress(first), first
        assert first["params"]["progressToken"] == PROGRESS_TOKEN
        assert first["params"]["message"].startswith("rollout activity")
        assert "result" in await client.receive()


async def test_a_rogue_child_fails_one_request_and_leaves_the_session_alive(
    recording_egress,
):
    """anyio cancels a task group's own scope when any child ends in
    CancelledError. Surfacing that as CancelledError would be read by the SDK
    as transport shutdown (`message.cancelled` is False -> re-raise into the
    server task group) and kill the session for every later call. It must be an
    ordinary request-local failure instead."""
    async def invoke(note):
        async def rogue() -> None:
            raise asyncio.CancelledError()

        _RecordingEgress.made[-1]._spawn(rogue)
        await asyncio.sleep(0.05)
        return "envelope"  # pragma: no cover

    async with _connected(_probe_server(invoke), write_buffer=10) as (client, _w):
        await client.call_tool("probe")
        response = await client.receive()
        assert response["id"] == 1, response
        # A response at all is the point: the request failed, the wire lived.
        assert response.get("result", {}).get("isError") or "error" in response

        # The session must still serve the NEXT request.
        await client.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        assert (await client.receive())["id"] == 2


async def test_throttle_keeps_the_wire_to_one_notification_per_window(
    recording_egress,
):
    """Protocol-level rate limiting. Each note waits for its predecessor to
    SETTLE, so the single-flight slot is free every time — only the 60 s
    throttle can hold the wire to one frame."""
    async def invoke(note):
        egress = _RecordingEgress.made[-1]
        for _ in range(3):
            note("stdout")
            with anyio.fail_after(RECV_TIMEOUT_S):
                while egress._flight is not None:  # slot free before the next
                    await anyio.sleep(0.01)
        assert egress.attempts == 1, "the throttle, not single-flight, held it"
        return "envelope"

    async with _connected(_probe_server(invoke), write_buffer=10) as (client, _w):
        await client.call_tool("probe")
        first = await client.receive()
        assert _is_progress(first)
        result = await client.receive()
        assert "result" in result
        await client.receive_none()
