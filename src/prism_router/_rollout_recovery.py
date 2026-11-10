"""Recover codex's last agent_message from its per-session rollout JSONL
when the upstream stdout-flush bug strands the answer (pre_terminal_idle
fires but codex has already written task_complete to its rollout file).

See docs/bugs/rollout-recovery.md for the bug pattern and decision rationale.
Pure I/O + JSON parsing; no subprocess, no kill paths. Called only from
dispatcher._build_kill_result on the pre_terminal_idle branch (via
asyncio.to_thread + wait_for(5s) so a slow glob does not block the loop).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_LOG = logging.getLogger("prism_router.rollout_recovery")

# Codex rollout JSONL terminal event payload types. NOT the same as
# CODEX_SPEC.terminal_event_types (those are stdout `turn.completed` /
# `thread.completed`). Rollout uses `event_msg` with `payload.type` from
# `codex-rs/protocol/src/protocol.rs:1308-1316` (task_complete) and an
# `error` payload variant carrying `message`. `task_failed` is forward-compat:
# not observed in 0.130.0 rollouts but cheap to accept.
_TERMINAL_PAYLOAD_TYPES = frozenset({"task_complete", "task_failed", "error"})

# Turn boundary marker. A bottom-up scan that reaches this before a terminal
# payload has left the current turn — everything above belongs to an earlier
# turn of the same resumed session.
_TURN_START_PAYLOAD_TYPE = "task_started"

# Rollout timestamps are millisecond-resolution; the dispatcher's baseline is a
# raw float. Without this slack a record written inside the same millisecond as
# the baseline reads as "older" purely from truncation. One millisecond widens
# no real window — a previous turn's terminal is minutes old.
_TS_RESOLUTION_S = 0.001


@dataclass(frozen=True)
class Recovered:
    kind: str  # "complete" | "failed"
    path: Path
    agent_message: str | None  # populated when kind == "complete"
    error: str | None  # populated when kind == "failed"


def _codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    if raw:
        return Path(raw)
    return Path.home() / ".codex"


def find_rollout_path(
    thread_id: str, *, codex_home: Path | None = None,
) -> Path | None:
    """Locate the rollout file for `thread_id`.

    Filename pattern (per `codex-rs/rollout/src/recorder.rs:1329-1348`):
    `sessions/YYYY/MM/DD/rollout-<ISO-TS>-<UUID>.jsonl`. Glob by UUID suffix —
    globally unique, so date partitioning is irrelevant for matching.

    Returns the newest live match (mtime tiebreaker) or None. Tolerates files
    that disappear between glob and stat (cleanup races).
    """
    home = codex_home or _codex_home()
    sessions = home / "sessions"
    if not sessions.exists():
        return None
    try:
        candidates = list(sessions.glob(f"**/rollout-*-{thread_id}.jsonl"))
    except OSError as exc:
        _LOG.warning("rollout glob failed: %r", exc)
        return None
    live: list[tuple[float, Path]] = []
    for path in candidates:
        try:
            live.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not live:
        return None
    live.sort(key=lambda item: item[0], reverse=True)
    return live[0][1]


def _event_epoch(obj: dict) -> float | None:
    """Rollout `timestamp` (ISO-8601 UTC, `...Z`) as epoch seconds; None when
    absent, unparseable, or offset-naive — the caller treats that as "cannot
    prove ownership" and declines. Naive strings are rejected rather than
    guessed: `datetime.timestamp()` would silently read them as host-local and
    shift the comparison by the UTC offset."""
    raw = obj.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def read_terminal_event(path: Path, *, not_before: float) -> Recovered | None:
    """Scan rollout JSONL for the CURRENT turn's task_complete / task_failed /
    error payload. Returns None on no terminal event present (genuine
    mid-reasoning hang) or empty `last_agent_message` (refuses to claim empty
    success).

    `codex exec resume <SID>` appends every turn to the SAME rollout, so a
    naive bottom-up scan can cross the turn boundary and hand the PREVIOUS
    turn's answer back as this turn's result — a stale success, worse than the
    hang it recovers from. Two independent guards, because neither subsumes the
    other (docs/bugs/rollout-recovery-cross-turn-leak.md):

    - temporal: the payload's own `timestamp` may sit at most
      `_TS_RESOLUTION_S` behind `not_before`, the wall clock at which this
      invocation spawned. Earlier turns finished before we spawned, so this
      holds whatever the file's shape. A payload whose timestamp is missing,
      unparseable, or offset-naive fails closed.
    - structural: reaching a `task_started` first means the current turn wrote
      no terminal payload. Clock-independent, and it short-circuits the common
      case.

    Tolerates a trailing partial JSON line (codex still flushing): individual
    decode errors skip the line, not the whole scan. F1 (docs/bugs/codex-post-
    exit-hang.md) confirms at most one task-terminal payload per turn.
    """
    try:
        raw = path.read_bytes().decode("utf-8", "replace")
    except OSError as exc:
        _LOG.warning("rollout read failed: %r", exc)
        return None

    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "event_msg":
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type")
        if ptype == _TURN_START_PAYLOAD_TYPE:
            return None  # current turn never reached a terminal payload
        if ptype not in _TERMINAL_PAYLOAD_TYPES:
            continue
        ts = _event_epoch(obj)
        if ts is None or ts + _TS_RESOLUTION_S < not_before:
            _LOG.warning(
                "rollout terminal payload predates this invocation "
                "(ts=%r, not_before=%.3f) — declining stale recovery",
                obj.get("timestamp"), not_before,
            )
            return None
        if ptype == "task_complete":
            msg = payload.get("last_agent_message")
            if isinstance(msg, str) and msg:
                return Recovered(
                    kind="complete", path=path,
                    agent_message=msg, error=None,
                )
            # task_complete present but empty — refuse to claim success.
            return None
        # task_failed / error
        err_msg = payload.get("message") or payload.get("error") or ptype
        return Recovered(
            kind="failed", path=path,
            agent_message=None, error=str(err_msg),
        )
    return None


def recover(
    thread_id: str, *, not_before: float, codex_home: Path | None = None,
) -> Recovered | None:
    """End-to-end recovery: env gate → file lookup → terminal scan.

    `not_before` is the wall clock (epoch seconds) at which this invocation
    spawned; it fences off earlier turns of a resumed session. Returns None on
    env-disable, missing file, missing terminal, empty task_complete, or a
    terminal payload that predates the invocation. Dispatcher falls back to the
    standard pre_terminal_idle error envelope on None.
    """
    if os.environ.get("PRISM_ROLLOUT_RECOVERY") == "0":
        return None
    path = find_rollout_path(thread_id, codex_home=codex_home)
    if path is None:
        return None
    return read_terminal_event(path, not_before=not_before)
