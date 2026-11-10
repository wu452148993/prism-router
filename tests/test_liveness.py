"""Unit gates for the request-scoped liveness progress egress.

One test per claim in docs/bugs/host-idle-progress-egress.md (Acceptance →
Unit). Docstrings name the claim each test is proving.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import sys
import time

import anyio
import pytest

from prism_router import server as server_mod
from prism_router._liveness import (
    DRAIN_TIMEOUT_S,
    MIN_INTERVAL_S,
    SEND_TIMEOUT_S,
    LivenessEgress,
    _Flight,
)
from prism_router.dispatcher import OVERALL_TIMEOUT_S


class _Clock:
    """Injected monotonic clock: throttle / deadline logic is time arithmetic,
    not real waiting."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


async def _settle() -> None:
    """Give a just-spawned runner its scheduling slot. Enough for any sink
    that completes or raises without suspending."""
    for _ in range(3):
        await asyncio.sleep(0)


@contextlib.asynccontextmanager
async def _egress(sink, **kwargs):
    """Egress in a task group, mirroring the tool body. The `abort` backstop
    matches production and keeps a hung sink from stalling the group join."""
    async with anyio.create_task_group() as tg:
        egress = LivenessEgress(sink, spawn=tg.start_soon, **kwargs)
        try:
            yield egress
        finally:
            egress.abort()


def _recorder(calls: list) -> object:
    async def sink(progress: float, message: str) -> None:
        calls.append((progress, message))
    return sink


# ───── throttle / single-flight / counter ─────


async def test_raising_sink_storm_stays_under_one_attempt_per_interval():
    """Throttle is consumed by the ATTEMPT, bumped before the task exists — an
    immediately-raising sink cannot machine-gun beyond 1 attempt per interval
    even when fed at the runner's 2 s coalescing cadence."""
    attempted: list[float] = []

    async def sink(progress: float, message: str) -> None:
        attempted.append(progress)
        raise RuntimeError("sink is down")

    clock = _Clock()
    async with _egress(sink, clock=clock, min_interval=MIN_INTERVAL_S) as egress:
        for _ in range(60):  # t = 0, 2, ... 118 s
            egress.note("stdout")
            await _settle()
            clock.advance(2.0)

    # 118 s of evidence at 2 s cadence → attempts at t=0 and t=60 only.
    assert attempted == [1.0, 2.0]
    assert egress.attempts == 2
    assert egress.attempts <= clock.t / MIN_INTERVAL_S + 1


async def test_single_flight_drops_evidence_while_a_send_is_pending():
    """New evidence arriving while a send is pending is dropped — proven with
    the throttle disabled so only single-flight can stop it."""
    calls: list = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def sink(progress: float, message: str) -> None:
        calls.append(progress)
        started.set()
        await release.wait()

    async with _egress(sink, min_interval=0.0) as egress:
        egress.note("stdout")
        await started.wait()
        for _ in range(5):
            egress.note("stdout")
        await _settle()
        assert egress.attempts == 1, "pending send must swallow further evidence"
        release.set()
        await _settle()
        egress.note("stdout")
        await _settle()
        assert egress.attempts == 2, "slot must be reusable once the send settles"
    assert calls == [1.0, 2.0]


async def test_counter_is_monotonic_across_simulated_re_dispatch():
    """gemini's transient retry builds a NEW Dispatcher but reuses the SAME
    egress; `progress` must strictly increase across that boundary."""
    calls: list = []
    async with _egress(_recorder(calls), min_interval=0.0) as egress:
        for _dispatch in range(3):  # three Dispatchers, one request
            for _ in range(2):
                egress.note("stdout")
                await _settle()

    values = [progress for progress, _ in calls]
    assert values == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert all(b > a for a, b in zip(values, values[1:]))


# ───── aclose / abort lifecycle ─────


async def test_aclose_waits_for_an_in_flight_send_to_settle():
    """The barrier: `aclose` returns only after the pending send has handed
    its frame over, so no notification can trail the response."""
    order: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def sink(progress: float, message: str) -> None:
        started.set()
        await release.wait()
        order.append("sent")

    async with _egress(sink) as egress:
        egress.note("stdout")
        await started.wait()
        asyncio.get_running_loop().call_later(0.05, release.set)
        await egress.aclose(timeout=DRAIN_TIMEOUT_S)
        order.append("closed")

    assert order == ["sent", "closed"]


async def test_aclose_cancels_a_send_that_overruns_the_drain_budget():
    """When settlement does not arrive inside the budget, `aclose` cancels the
    send and still returns — a hung sink parks the slot, it does not park the
    response."""
    outcome: list[str] = []
    started = asyncio.Event()

    async def sink(progress: float, message: str) -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise
        outcome.append("sent")

    async with _egress(sink) as egress:
        egress.note("stdout")
        await started.wait()
        t0 = time.monotonic()
        await egress.aclose(timeout=0.05)
        elapsed = time.monotonic() - t0
        # Asserted before the group join, which would settle the flight anyway.
        assert outcome == ["cancelled"]
        assert egress._flight is None, "cancelled flight must release the slot"

    assert elapsed < 2.0, "aclose cancelled rather than waiting out the send"


@pytest.mark.parametrize("with_flight", [False, True], ids=["idle", "active"])
async def test_note_after_close_never_sends(with_flight: bool):
    """Both close shapes must disable notes — closing an egress that HAS a
    live flight takes a different branch from closing an idle one."""
    calls: list = []
    async with _egress(_recorder(calls), min_interval=0.0) as egress:
        if with_flight:
            egress.note("stdout")
        await egress.aclose()
        before = egress.attempts
        egress.note("stdout")
        await _settle()
    assert egress.attempts == before
    assert len(calls) == (1 if with_flight else 0)


async def test_double_aclose_is_idempotent():
    """A second `aclose` must be a fast no-op, not a second drain."""
    calls: list = []
    async with _egress(_recorder(calls), min_interval=0.0) as egress:
        egress.note("stdout")
        await egress.aclose()
        t0 = time.monotonic()
        await egress.aclose()
        assert time.monotonic() - t0 < 0.5
    assert len(calls) == 1


async def test_abort_after_aclose_and_aclose_after_abort_both_settle():
    """Either order leaves the flight settled and the egress closed."""
    calls: list = []
    async with _egress(_recorder(calls), min_interval=0.0) as egress:
        egress.note("stdout")
        await egress.aclose()
        egress.abort()  # after aclose: idempotent, nothing left to cancel
        assert egress._flight is None

    started = asyncio.Event()

    async def hung(progress: float, message: str) -> None:
        started.set()
        await asyncio.sleep(30)

    async with _egress(hung) as egress2:
        egress2.note("stdout")
        await started.wait()
        egress2.abort()  # cancellation REQUESTED, not yet settled
        t0 = time.monotonic()
        await egress2.aclose(timeout=DRAIN_TIMEOUT_S)  # aclose observes settlement
        assert time.monotonic() - t0 < 1.0
        assert egress2._flight is None


async def test_abort_during_a_cancel_unwind_requests_cancellation_without_awaiting():
    """Cancel unwind: `abort()` is synchronous, undelayed, does not raise, does
    not mask the active CancelledError, and does NOT claim settlement — it
    must leave the flight unsettled and still current, cancelling only later."""
    state: list[str] = []
    started = asyncio.Event()

    async def sink(progress: float, message: str) -> None:
        started.set()
        state.append("running")
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state.append("cancelled")
            raise

    assert not inspect.iscoroutinefunction(LivenessEgress.abort)

    async with _egress(sink) as egress:
        egress.note("stdout")
        flight = egress._flight
        await started.wait()
        try:
            raise asyncio.CancelledError()
        except asyncio.CancelledError:
            t0 = time.monotonic()
            egress.abort()
            latency = time.monotonic() - t0
            assert isinstance(sys.exc_info()[1], asyncio.CancelledError)
        # "Cannot await" is proven structurally above; this is a loose
        # operational probe for a BLOCKING delay, kept wide enough that host
        # descheduling cannot fail correct code.
        assert latency < 0.15, f"abort must not delay the unwind ({latency:.3f}s)"
        # Requested, NOT settled: a `_settle` inside abort would satisfy a
        # "sink still running" check too, so assert the flight state directly.
        assert flight is not None and not flight.done.is_set()
        assert egress._flight is flight
        assert flight.scope.cancel_called
        assert state == ["running"]
        await _settle()
        assert state == ["running", "cancelled"]
        assert flight.done.is_set()


async def test_aclose_and_abort_do_not_mask_a_non_cancellation_exception():
    """The error-envelope path drains inside the active `except` block; the
    exception under handling must survive both calls untouched (G2)."""
    calls: list = []
    async with _egress(_recorder(calls), min_interval=0.0) as egress:
        egress.note("stdout")
        try:
            raise ValueError("invoke blew up")
        except ValueError as exc:
            await egress.aclose(timeout=DRAIN_TIMEOUT_S)
            egress.abort()
            assert sys.exc_info()[1] is exc
    assert len(calls) == 1, "the pending send still settled before the raise unwound"


async def test_abort_before_the_runner_starts_skips_the_send():
    """`start_soon` defers execution, so an `abort()` can land before the
    runner's first line — the flag must catch it, and the attempt still keeps
    its counter slot."""
    calls: list = []
    async with _egress(_recorder(calls), min_interval=0.0) as egress:
        egress.note("stdout")  # spawned, not yet scheduled
        egress.abort()
        await _settle()
    assert calls == []
    assert egress.attempts == 1


async def test_spawn_rejection_completes_the_slot_and_consumes_the_attempt():
    """A closing task group rejects `start_soon`; the slot is completed and
    cleared, while the attempt still consumes its counter AND its throttle
    interval (the wire gap is spec-legal)."""
    rejected: list = []

    def rejecting_spawn(fn, flight):
        rejected.append(flight)
        raise RuntimeError("This task group is not active")

    clock = _Clock()
    egress = LivenessEgress(
        _recorder([]), spawn=rejecting_spawn, clock=clock,
        min_interval=MIN_INTERVAL_S,
    )
    egress.note("stdout")
    assert egress.attempts == 1
    assert rejected[0].done.is_set(), "rejected flight must be completed"
    assert egress._flight is None, "rejected flight must release the slot"

    clock.advance(2.0)
    egress.note("stdout")
    assert egress.attempts == 1, "throttle interval was consumed by the rejection"

    clock.advance(MIN_INTERVAL_S)
    egress.note("stdout")
    assert egress.attempts == 2, "slot was cleared, so a later attempt still runs"

    t0 = time.monotonic()
    await egress.aclose()  # must not hang on a flight that never ran
    assert time.monotonic() - t0 < 0.5


async def test_settle_releases_the_slot_only_if_it_is_still_current():
    """A late `finally` from a superseded flight must never free the live
    slot."""
    egress = LivenessEgress(_recorder([]), spawn=lambda *a: None)
    stale = _Flight(1.0, "stale")
    current = _Flight(2.0, "current")
    egress._flight = current

    egress._settle(stale)
    assert stale.done.is_set()
    assert egress._flight is current

    egress._settle(current)
    assert egress._flight is None


async def test_a_spuriously_cancelling_sink_does_not_cancel_the_task_group():
    """A sink raising CancelledError with NO cancellation pending is a broken
    sink, not a cancellation: `CancelledError` is not an `Exception`, so
    letting it out would make anyio cancel the tool's whole task group."""
    survived: list[str] = []

    async def sink(progress: float, message: str) -> None:
        raise asyncio.CancelledError()

    async def sibling() -> None:
        await asyncio.sleep(0.05)
        survived.append("alive")

    async with anyio.create_task_group() as tg:
        tg.start_soon(sibling)
        egress = LivenessEgress(sink, spawn=tg.start_soon, min_interval=0.0)
        egress.note("stdout")
        await egress.aclose()
        assert not tg.cancel_scope.cancel_called

    assert survived == ["alive"]


async def test_a_real_cancellation_still_propagates_out_of_the_runner():
    """The spurious-cancel guard must not swallow a REAL one. Proven at the
    child-local scope: `cancelled_caught` is set only if the CancelledError
    actually reached `__exit__`, i.e. the runner re-raised instead of
    swallowing. The sink's own record cannot show this — it is upstream of
    the guard and looks identical either way."""
    outcome: list[str] = []
    started = asyncio.Event()

    async def sink(progress: float, message: str) -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise

    async with _egress(sink) as egress:
        egress.note("stdout")
        flight = egress._flight
        await started.wait()
        egress.abort()
        await egress.aclose()

    assert outcome == ["cancelled"]
    assert flight is not None and flight.scope.cancelled_caught is True


async def test_aclose_waits_for_real_settlement_when_the_sink_resists_cancel():
    """The postcondition is absolute: after `aclose` returns, the send is
    settled. A sink that shields itself past the drain window delays `aclose`
    — it must never let it return with the flight still live."""
    state: list[str] = []
    started = asyncio.Event()

    async def sink(progress: float, message: str) -> None:
        started.set()
        with anyio.CancelScope(shield=True):
            await anyio.sleep(0.3)  # ignores abort()'s cancel
        state.append("finished")

    async with _egress(sink) as egress:
        egress.note("stdout")
        flight = egress._flight
        assert flight is not None
        await started.wait()
        await egress.aclose(timeout=0.05)
        # Asserted HERE, not after the group join — the join would settle the
        # flight either way and make these vacuous. Deterministic rather than a
        # stopwatch: `cancel_called` proves aclose CANCELLED and then waited,
        # `done` proves it waited for real settlement.
        assert state == ["finished"]
        assert egress._flight is None, "aclose must not return on a live flight"
        assert flight.done.is_set()
        assert flight.scope.cancel_called


async def test_a_raising_sink_does_not_cancel_the_enclosing_task_group():
    """An escaping child exception would cancel the tool's whole task group;
    the runner swallows sink failures, so a sibling child survives."""
    survived: list[str] = []
    ran: list[str] = []

    async def sink(progress: float, message: str) -> None:
        ran.append("sink")
        raise RuntimeError("boom")

    async def sibling() -> None:
        await asyncio.sleep(0.05)
        survived.append("alive")

    async with anyio.create_task_group() as tg:
        tg.start_soon(sibling)
        egress = LivenessEgress(sink, spawn=tg.start_soon, min_interval=0.0)
        egress.note("stdout")
        await _settle()
        assert not tg.cancel_scope.cancel_called
        await egress.aclose()

    assert ran == ["sink"], "vacuous otherwise: the sink must have raised"
    assert survived == ["alive"]


# ───── attempt deadline / bounded send ─────


def test_egress_max_s_is_derived_from_the_dispatcher_constant():
    """G4/M2: single-sourced. Asserting the arithmetic alone cannot tell a
    derived value from a hardcoded 5370, so move the source constant and
    re-import: the result must follow it."""
    import importlib

    from prism_router import dispatcher

    assert server_mod.EGRESS_MAX_S == OVERALL_TIMEOUT_S - 1800.0 - SEND_TIMEOUT_S
    # Subtracting the send timeout is what keeps the LATEST delivery inside
    # overall_timeout - host idle cap.
    assert server_mod.EGRESS_MAX_S + SEND_TIMEOUT_S == OVERALL_TIMEOUT_S - 1800.0
    assert server_mod.EGRESS_MAX_S > 0

    original = dispatcher.OVERALL_TIMEOUT_S
    try:
        dispatcher.OVERALL_TIMEOUT_S = 3600.0
        importlib.reload(server_mod)
        assert server_mod.EGRESS_MAX_S == 3600.0 - 1800.0 - SEND_TIMEOUT_S
    finally:
        dispatcher.OVERALL_TIMEOUT_S = original
        importlib.reload(server_mod)
    assert server_mod.EGRESS_MAX_S == OVERALL_TIMEOUT_S - 1800.0 - SEND_TIMEOUT_S


async def test_note_is_a_permanent_no_op_past_the_attempt_deadline():
    """After `egress_max_s` the host's idle clock must run again, so `note()`
    stops forever — not just for one window."""
    calls: list = []
    clock = _Clock()
    async with _egress(
        _recorder(calls), clock=clock, max_s=server_mod.EGRESS_MAX_S,
        min_interval=0.0,
    ) as egress:
        clock.advance(server_mod.EGRESS_MAX_S - 1.0)
        egress.note("stdout")
        await _settle()
        assert egress.attempts == 1

        clock.advance(1.0)  # exactly at the deadline
        egress.note("stdout")
        await _settle()
        assert egress.attempts == 1

        clock.advance(3600.0)
        egress.note("stdout")
        await _settle()
        assert egress.attempts == 1
    assert len(calls) == 1


async def test_send_is_bounded_by_send_timeout():
    """The send is bounded independently of the drain budget: `aclose` gets a
    100x larger budget and still returns via the send timeout."""
    state: list[str] = []
    started = asyncio.Event()

    async def sink(progress: float, message: str) -> None:
        started.set()
        state.append("start")
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state.append("cancelled")
            raise
        state.append("sent")

    async with _egress(sink, send_timeout=0.05, min_interval=0.0) as egress:
        egress.note("stdout")
        await started.wait()
        t0 = time.monotonic()
        await egress.aclose(timeout=5.0)
        elapsed = time.monotonic() - t0
        assert state == ["start", "cancelled"]

    # Tight enough to catch a hardcoded ceiling: the INJECTED 0.05 s bound is
    # what fired, and it beat the 5 s drain budget by two orders of magnitude.
    assert elapsed < 0.75, f"send was not bounded by send_timeout ({elapsed:.3f}s)"


async def test_timeout_scopes_receive_the_configured_delays(monkeypatch):
    """The behavioural timeout tests prove that cancellation HAPPENS; they
    cannot tell a configured budget from a hardcoded literal that merely fires
    inside their slack. Record what actually reaches the anyio scopes, for both
    an injected value and the shipped default."""
    seen: dict[str, list[float]] = {"fail_after": [], "move_on_after": []}
    real_fail, real_move = anyio.fail_after, anyio.move_on_after

    def fail_after(delay, *args, **kwargs):
        seen["fail_after"].append(delay)
        return real_fail(delay, *args, **kwargs)

    def move_on_after(delay, *args, **kwargs):
        seen["move_on_after"].append(delay)
        return real_move(delay, *args, **kwargs)

    monkeypatch.setattr(anyio, "fail_after", fail_after)
    monkeypatch.setattr(anyio, "move_on_after", move_on_after)

    started, release = asyncio.Event(), asyncio.Event()

    async def sink(progress: float, message: str) -> None:
        started.set()
        await release.wait()

    async def drive(egress, **aclose_kwargs) -> None:
        started.clear()
        release.clear()
        egress.note("stdout")
        await started.wait()
        asyncio.get_running_loop().call_soon(release.set)
        await egress.aclose(**aclose_kwargs)

    async with _egress(sink, send_timeout=0.05, min_interval=0.0) as injected:
        await drive(injected, timeout=0.07)
    assert seen["fail_after"] == [0.05]
    assert seen["move_on_after"] == [0.07]

    seen["fail_after"].clear()
    seen["move_on_after"].clear()
    async with _egress(sink, min_interval=0.0) as default:
        await drive(default)
    assert seen["fail_after"] == [SEND_TIMEOUT_S]
    assert seen["move_on_after"] == [DRAIN_TIMEOUT_S]


async def test_shipping_timeout_wrapper_keeps_the_sink_in_the_runner_task(monkeypatch):
    """R5: `asyncio.wait_for` would wrap the sink in a separate asyncio task
    OUTSIDE the request-scoped group, reopening the cancellation race. Prove
    the shipped wrapper (a) never calls `wait_for` and (b) awaits the sink in
    the runner task itself, adding no nested task."""
    async def exploding_wait_for(*args, **kwargs):
        raise AssertionError("wait_for escapes the request-scoped task group")

    monkeypatch.setattr(asyncio, "wait_for", exploding_wait_for)

    seen: dict = {}

    async def sink(progress: float, message: str) -> None:
        seen["sink_task"] = asyncio.current_task()
        seen["during"] = set(asyncio.all_tasks())

    async with anyio.create_task_group() as tg:
        baseline = set(asyncio.all_tasks())

        def spawn(fn, flight):
            async def runner(f):
                seen["runner_task"] = asyncio.current_task()
                await fn(f)
            tg.start_soon(runner, flight)

        egress = LivenessEgress(sink, spawn=spawn)
        egress.note("stdout")
        await egress.aclose(timeout=DRAIN_TIMEOUT_S)

    assert seen["sink_task"] is seen["runner_task"]
    assert seen["during"] - baseline == {seen["runner_task"]}


async def test_send_timeout_does_not_escape_the_runner(caplog):
    """`anyio.fail_after` cancels the runner in-scope; the TimeoutError must be
    absorbed there, never surfacing as a task-group exception. The log
    assertion is what distinguishes this from `aclose` doing the cancelling."""
    async def sink(progress: float, message: str) -> None:
        await asyncio.sleep(30)

    with caplog.at_level("WARNING", logger="prism_router.liveness"):
        async with _egress(sink, send_timeout=0.05, min_interval=0.0) as egress:
            egress.note("stdout")
            await egress.aclose(timeout=5.0)
    # Reaching here means the task group joined without a TimeoutError escaping.
    assert "send exceeded" in caplog.text, "the send timeout, not aclose, fired"


async def test_note_never_raises_when_the_spawner_explodes():
    """The egress promises not to raise into the dispatcher's evidence sites."""
    ran: list[str] = []

    def boom(fn, flight):
        ran.append("spawn")
        raise ValueError("spawner exploded")

    egress = LivenessEgress(_recorder([]), spawn=boom)
    egress.note("stdout")  # must not propagate
    assert ran == ["spawn"], "vacuous otherwise: the spawner must have run"
    assert egress.attempts == 1


async def test_note_is_synchronous_so_an_evidence_site_cannot_await_the_egress():
    """Guard the EGRESS side of invariant 1: the dispatcher calls `note` inline
    on kill paths, so it must be a plain function AND hand back nothing
    awaitable — declaring it `def` while returning the sink coroutine would
    pass an `iscoroutinefunction` check and still let a call site await a hung
    sink. Awaits elsewhere in dispatcher control flow stay a review contract."""
    assert not inspect.iscoroutinefunction(LivenessEgress.note)

    def rejecting_spawn(*args) -> None:
        raise RuntimeError("no task needed for this structural guard")

    egress = LivenessEgress(_recorder([]), spawn=rejecting_spawn)
    assert egress.note("stdout") is None


def test_module_defaults_match_the_design():
    assert MIN_INTERVAL_S == 60.0
    assert SEND_TIMEOUT_S == 30.0
    assert DRAIN_TIMEOUT_S == 2.0


@pytest.mark.parametrize("source", ["stdout", "rollout"])
async def test_message_names_the_evidence_source(source: str):
    calls: list = []
    clock = _Clock()
    async with _egress(_recorder(calls), clock=clock) as egress:
        clock.advance(62.0)
        egress.note(source)
        await _settle()
    assert calls[0][0] == 1.0
    assert calls[0][1] == f"{source} activity, 62s elapsed"
