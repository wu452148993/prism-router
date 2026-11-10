"""Request-scoped liveness → MCP progress egress.

An MCP host that never hears from a server mid-call degrades its IDLE timeout
into a TOTAL-duration cap. This module forwards the SAME evidence the
pre_terminal_idle watchdog already consumes — stdout ticks and observed rollout
growth — as progress notifications, so the host's clock resets only while work
is demonstrably happening. No evidence → no notifications → the host cap
governs, which is its spec role.

One `LivenessEgress` per MCP tool call, created inside the tool body and shared
across every Dispatcher the request creates. Send tasks are spawned into a task
group nested inside the request's cancel scope; that nesting, not any
handler-local cleanup, is what orders an in-flight notification against the
SDK's cancellation error response. See docs/bugs/host-idle-progress-egress.md.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

import anyio

_LOG = logging.getLogger("prism_router.liveness")

# MCP rate-limiting floor between send ATTEMPTS.
MIN_INTERVAL_S = 60.0
# Ceiling on one send; the runner is cancelled in-scope when it expires.
SEND_TIMEOUT_S = 30.0
# Grace given to an in-flight send before `aclose` cancels it.
DRAIN_TIMEOUT_S = 2.0

# `(progress, message) -> awaitable`; the egress owns the counter.
ProgressSink = Callable[[float, str], Awaitable[None]]
# `anyio.abc.TaskGroup.start_soon` — returns None, NOT a task handle.
Spawn = Callable[..., Any]


class _Flight:
    """One in-flight send: payload, child-local cancel scope, completion
    signal. `cancelled` covers the window before the runner enters the scope —
    `start_soon` defers execution, so an `abort()` can land first."""

    __slots__ = ("progress", "message", "scope", "done", "cancelled")

    def __init__(self, progress: float, message: str) -> None:
        self.progress = progress
        self.message = message
        self.scope = anyio.CancelScope()
        self.done = anyio.Event()
        self.cancelled = False


class LivenessEgress:
    """Evidence-gated progress egress for a single MCP tool call.

    `note()` is the only intake: sync, O(1), non-raising, coupled to no kill
    decision. `aclose()` is the response barrier; `abort()` is its zero-await
    counterpart for cancel unwinds.
    """

    def __init__(
        self,
        sink: ProgressSink,
        *,
        spawn: Spawn,
        max_s: float = float("inf"),
        min_interval: float = MIN_INTERVAL_S,
        send_timeout: float = SEND_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sink = sink
        self._spawn = spawn
        self._min_interval = min_interval
        self._send_timeout = send_timeout
        self._clock = clock
        self._t0 = clock()
        self._deadline = self._t0 + max_s
        self._counter = 0.0
        self._last_attempt: float | None = None
        self._flight: _Flight | None = None
        self._closed = False

    @property
    def attempts(self) -> int:
        """Send attempts consumed so far (== the last progress value)."""
        return int(self._counter)

    def note(self, source: str) -> None:
        """Record one piece of liveness evidence; maybe start a send.

        Dropped when closed, past the attempt deadline, while a send is still
        pending (single-flight), or inside the throttle window. The throttle
        clock is consumed by the ATTEMPT, before the task exists, so a sink
        that raises instantly cannot machine-gun the host.
        """
        now = self._clock()
        if (
            self._closed
            or now >= self._deadline
            or self._flight is not None
            or (
                self._last_attempt is not None
                and now - self._last_attempt < self._min_interval
            )
        ):
            return
        self._last_attempt = now
        self._counter += 1.0
        flight = _Flight(
            self._counter, f"{source} activity, {now - self._t0:.0f}s elapsed",
        )
        self._flight = flight
        try:
            self._spawn(self._send, flight)
        except Exception as exc:  # noqa: BLE001 — a closing group races a late tick
            # The attempt keeps its counter slot and throttle interval; only
            # the wire frame is lost, and progress gaps are spec-legal.
            _LOG.debug("liveness progress not spawned: %r", exc)
            self._settle(flight)

    async def _send(self, flight: _Flight) -> None:
        """Task-group runner. `anyio.fail_after`, never `asyncio.wait_for`:
        the latter would run the sink in a task outside the request scope."""
        try:
            if flight.cancelled:
                return
            with flight.scope:
                try:
                    with anyio.fail_after(self._send_timeout):
                        await self._sink(flight.progress, flight.message)
                except TimeoutError:
                    _LOG.warning(
                        "liveness progress send exceeded %.0fs", self._send_timeout,
                    )
                except asyncio.CancelledError:
                    # anyio calls task.cancel() before delivering scope
                    # cancellation, so a real one always has a pending request
                    # here; a naked sink raise has none. Letting the latter out
                    # would cancel the tool's whole task group.
                    task = asyncio.current_task()
                    if task is None or task.cancelling():
                        raise
                    _LOG.warning("liveness sink raised CancelledError spuriously")
                except Exception as exc:  # noqa: BLE001 — never escape the group
                    _LOG.warning("liveness progress send failed: %r", exc)
        finally:
            self._settle(flight)

    def _settle(self, flight: _Flight) -> None:
        """Signal completion and release the single-flight slot, only-if-current."""
        flight.done.set()
        if self._flight is flight:
            self._flight = None

    def abort(self) -> None:
        """Stop future notes and REQUEST cancellation of the in-flight send.

        Synchronous, idempotent, zero awaits: during a cancel unwind an awaited
        drain would be re-cancelled at its first suspension point anyway, and
        delaying the re-raise would break "cancel re-raises CancelledError".
        """
        self._closed = True
        flight = self._flight
        if flight is not None and not flight.cancelled:
            flight.cancelled = True
            flight.scope.cancel()

    async def aclose(self, *, timeout: float = DRAIN_TIMEOUT_S) -> None:
        """Response barrier: once this returns, no notification can still reach
        the write stream — the pending send has either handed its frame over
        (ordered ahead of the response) or been cancelled before writing.

        Closes first so no new attempt can start, gives the send `timeout` to
        finish on its own, then cancels and waits for real settlement. That
        second wait is deliberately unbounded: the enclosing task group joins
        the same child regardless, so a deadline here would buy no
        end-to-end bound and would only make the postcondition a lie. Sink
        failures never surface; a cancellation arriving DURING the drain
        propagates, and the caller's `finally` aborts.
        """
        self._closed = True
        flight = self._flight
        if flight is None:
            return
        with anyio.move_on_after(timeout):
            await flight.done.wait()
            return
        self.abort()
        await flight.done.wait()
