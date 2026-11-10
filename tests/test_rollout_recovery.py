"""Unit tests for `_rollout_recovery`. Targets the upstream codex-CLI
stdout-flush bug recovered via codex's own per-session rollout JSONL.
See docs/bugs/rollout-recovery.md for the bug pattern."""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime
from pathlib import Path

import pytest

from prism_router import _rollout_recovery as rr

_FIXTURES = Path(__file__).parent / "fixtures" / "rollouts"


def _epoch(iso: str) -> float:
    """Rollout timestamps are ISO-8601 UTC (`...Z`)."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# Invocation baseline for the single-turn fixtures: they all write their
# terminal payload at 02:24:37Z, so any earlier spawn time accepts it.
_BASELINE = _epoch("2026-05-18T02:00:00Z")
# Between turn 1's terminal (02:24:37Z) and turn 2's start (02:25:03Z) in
# `resumed_two_turns.jsonl` — i.e. where a resumed invocation really spawns.
_RESUME_BASELINE = _epoch("2026-05-18T02:24:50Z")


def _install_rollout(
    codex_home: Path, fixture_name: str, *,
    thread_id: str, date_parts: tuple[str, str, str] = ("2026", "05", "18"),
) -> Path:
    """Copy a fixture into a fake CODEX_HOME layout under
    `sessions/YYYY/MM/DD/rollout-<TS>-<UUID>.jsonl`."""
    yyyy, mm, dd = date_parts
    dir_ = codex_home / "sessions" / yyyy / mm / dd
    dir_.mkdir(parents=True, exist_ok=True)
    target = dir_ / f"rollout-2026-05-{dd}T02-16-36-{thread_id}.jsonl"
    shutil.copy(_FIXTURES / fixture_name, target)
    return target


# ---- find_rollout_path ----------------------------------------------------


def test_find_rollout_returns_none_when_codex_home_missing(tmp_path: Path) -> None:
    assert rr.find_rollout_path("uuid-x", codex_home=tmp_path / "nope") is None


def test_find_rollout_returns_none_when_no_sessions_dir(tmp_path: Path) -> None:
    assert rr.find_rollout_path("uuid-x", codex_home=tmp_path) is None


def test_find_rollout_returns_none_when_uuid_not_present(tmp_path: Path) -> None:
    _install_rollout(tmp_path, "complete.jsonl", thread_id="uuid-a")
    assert rr.find_rollout_path("uuid-missing", codex_home=tmp_path) is None


def test_find_rollout_hits_via_uuid_glob(tmp_path: Path) -> None:
    target = _install_rollout(tmp_path, "complete.jsonl", thread_id="uuid-a")
    found = rr.find_rollout_path("uuid-a", codex_home=tmp_path)
    assert found is not None and found.samefile(target)


def test_find_rollout_picks_newest_mtime_when_multiple_match(tmp_path: Path) -> None:
    """Unlikely under real codex (UUIDs are globally unique) but defensive."""
    old = _install_rollout(
        tmp_path, "complete.jsonl", thread_id="uuid-dup",
        date_parts=("2026", "05", "17"),
    )
    newer = _install_rollout(
        tmp_path, "complete.jsonl", thread_id="uuid-dup",
        date_parts=("2026", "05", "18"),
    )
    # Force a deterministic mtime gap, not relying on filesystem ordering.
    os.utime(old, (time.time() - 60, time.time() - 60))
    os.utime(newer, (time.time(), time.time()))
    found = rr.find_rollout_path("uuid-dup", codex_home=tmp_path)
    assert found is not None and found.samefile(newer)


def test_find_rollout_honors_codex_home_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_rollout(tmp_path, "complete.jsonl", thread_id="uuid-env")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    found = rr.find_rollout_path("uuid-env")
    assert found is not None


# ---- read_terminal_event --------------------------------------------------


def test_read_terminal_returns_complete_for_task_complete(tmp_path: Path) -> None:
    target = _install_rollout(tmp_path, "complete.jsonl", thread_id="x")
    rec = rr.read_terminal_event(target, not_before=_BASELINE)
    assert rec is not None and rec.kind == "complete"
    assert rec.agent_message is not None
    assert rec.agent_message.startswith("**Critical Findings**")
    assert rec.error is None


def test_read_terminal_returns_failed_for_task_failed(tmp_path: Path) -> None:
    target = _install_rollout(tmp_path, "failed.jsonl", thread_id="x")
    rec = rr.read_terminal_event(target, not_before=_BASELINE)
    assert rec is not None and rec.kind == "failed"
    assert rec.error is not None and "rate_limit" in rec.error
    assert rec.agent_message is None


def test_read_terminal_returns_failed_for_error_event(tmp_path: Path) -> None:
    target = _install_rollout(tmp_path, "error_event.jsonl", thread_id="x")
    rec = rr.read_terminal_event(target, not_before=_BASELINE)
    assert rec is not None and rec.kind == "failed"
    assert rec.error is not None and "stream disconnected" in rec.error


def test_read_terminal_skips_trailing_partial_line(tmp_path: Path) -> None:
    target = _install_rollout(tmp_path, "truncated.jsonl", thread_id="x")
    rec = rr.read_terminal_event(target, not_before=_BASELINE)
    assert rec is not None and rec.kind == "complete"
    assert rec.agent_message == "recovered text from trailing-partial fixture"


def test_read_terminal_returns_none_when_no_terminal_event(tmp_path: Path) -> None:
    target = _install_rollout(tmp_path, "no_terminal.jsonl", thread_id="x")
    assert rr.read_terminal_event(target, not_before=_BASELINE) is None


def test_read_terminal_returns_none_for_empty_last_agent_message(
    tmp_path: Path,
) -> None:
    """Refuse to claim recovered success on empty content (would be worse than
    surfacing the pre_terminal_idle error to the user)."""
    target = _install_rollout(tmp_path, "empty_message.jsonl", thread_id="x")
    assert rr.read_terminal_event(target, not_before=_BASELINE) is None


# ---- resumed sessions: never answer with a previous turn -------------------
# `codex exec resume <SID>` appends every turn to ONE rollout, so a backward
# scan can reach the PREVIOUS turn's task_complete and hand stale text back as
# this turn's answer — a success with the wrong answer. Reproduced against a
# real two-turn rollout. docs/bugs/rollout-recovery-cross-turn-leak.md


def _truncate_after(target: Path, marker: str) -> Path:
    """Drop everything after the first line containing `marker` — models the
    file state at the moment the watchdog kills a turn mid-flight."""
    lines = target.read_text(encoding="utf-8").splitlines()
    cut = next(i for i, l in enumerate(lines) if marker in l) + 1
    target.write_text("\n".join(lines[:cut]) + "\n", encoding="utf-8")
    return target


def test_read_terminal_accepts_current_turn_of_resumed_session(
    tmp_path: Path,
) -> None:
    """The turn we launched completed: its answer is the one to return."""
    target = _install_rollout(tmp_path, "resumed_two_turns.jsonl", thread_id="x")
    rec = rr.read_terminal_event(target, not_before=_RESUME_BASELINE)
    assert rec is not None and rec.kind == "complete"
    assert rec.agent_message == "ANSWER TO THE CURRENT QUESTION"


def test_read_terminal_declines_when_current_turn_started_but_unfinished(
    tmp_path: Path,
) -> None:
    """Structural guard: killed after `task_started`, before any terminal."""
    target = _truncate_after(
        _install_rollout(tmp_path, "resumed_two_turns.jsonl", thread_id="x"),
        '"task_started","turn_id":"019e38de-2222',
    )
    assert rr.read_terminal_event(target, not_before=_RESUME_BASELINE) is None


def test_read_terminal_declines_before_current_turn_starts(
    tmp_path: Path,
) -> None:
    """Temporal guard: killed in the window after the resumed invocation wrote
    setup records but before `task_started` — the structural guard alone would
    walk straight into turn 1's answer."""
    target = _truncate_after(
        _install_rollout(tmp_path, "resumed_two_turns.jsonl", thread_id="x"),
        "thread_settings_applied",
    )
    assert rr.read_terminal_event(target, not_before=_RESUME_BASELINE) is None


def test_read_terminal_declines_terminal_without_parseable_timestamp(
    tmp_path: Path,
) -> None:
    """Fail closed: ownership of an undated payload cannot be proven."""
    target = _install_rollout(tmp_path, "complete.jsonl", thread_id="x")
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            '{"timestamp":"2026-05-18T02:24:37.086Z","type":"event_msg"',
            '{"type":"event_msg"',
        ),
        encoding="utf-8",
    )
    assert rr.read_terminal_event(target, not_before=_BASELINE) is None


def test_read_terminal_declines_offset_naive_timestamp(tmp_path: Path) -> None:
    """A timestamp that merely drops its `Z` must NOT be reinterpreted in the
    host timezone — `datetime.timestamp()` would shift it by the UTC offset and
    could smuggle a stale payload past the fence."""
    target = _install_rollout(tmp_path, "complete.jsonl", thread_id="x")
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            '"timestamp":"2026-05-18T02:24:37.086Z"',
            '"timestamp":"2026-05-18T02:24:37.086"',
        ),
        encoding="utf-8",
    )
    # `not_before=0` so the age fence cannot decline: only the offset-naive
    # rejection can produce None. With a realistic baseline this test would
    # pass on hosts east of UTC even without that rejection, because the
    # local-time reading lands BEFORE the baseline.
    assert rr.read_terminal_event(target, not_before=0.0) is None


@pytest.mark.parametrize(
    ("slack_multiple", "accepted"),
    [(0.999, True), (1.0, True), (1.001, False)],
)
def test_read_terminal_millisecond_slack_boundary(
    tmp_path: Path, slack_multiple: float, accepted: bool,
) -> None:
    """`_TS_RESOLUTION_S` absorbs the rollout's millisecond truncation and
    nothing more. The exact-boundary case pins inclusivity: flipping the
    comparison to `<=` would reject a payload exactly one resolution behind.
    Offsets are derived from the constant so they cannot drift from it; the
    constant itself is pinned separately, since deriving alone would stay green
    if the documented 1 ms policy were widened."""
    assert rr._TS_RESOLUTION_S == 0.001  # matches the 3-decimal rollout format
    target = _install_rollout(tmp_path, "complete.jsonl", thread_id="x")
    terminal_ts = _epoch("2026-05-18T02:24:37.086Z")
    behind_s = rr._TS_RESOLUTION_S * slack_multiple
    rec = rr.read_terminal_event(target, not_before=terminal_ts + behind_s)
    assert (rec is not None) is accepted


def test_read_terminal_returns_none_on_unreadable_file(tmp_path: Path) -> None:
    """Path.read_bytes raises OSError on permission/IO failure; recovery
    degrades to None rather than raising upward into dispatcher."""
    assert rr.read_terminal_event(tmp_path / "does-not-exist.jsonl", not_before=_BASELINE) is None


# ---- recover (end-to-end) -------------------------------------------------


def test_recover_disabled_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_rollout(tmp_path, "complete.jsonl", thread_id="uuid-disabled")
    monkeypatch.setenv("PRISM_ROLLOUT_RECOVERY", "0")
    assert rr.recover("uuid-disabled", not_before=_BASELINE, codex_home=tmp_path) is None


def test_recover_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert rr.recover("uuid-nope", not_before=_BASELINE, codex_home=tmp_path) is None


def test_recover_end_to_end_complete(tmp_path: Path) -> None:
    _install_rollout(tmp_path, "complete.jsonl", thread_id="uuid-ok")
    rec = rr.recover("uuid-ok", not_before=_BASELINE, codex_home=tmp_path)
    assert rec is not None and rec.kind == "complete"
    assert rec.agent_message is not None


def test_recover_end_to_end_failed(tmp_path: Path) -> None:
    _install_rollout(tmp_path, "failed.jsonl", thread_id="uuid-fail")
    rec = rr.recover("uuid-fail", not_before=_BASELINE, codex_home=tmp_path)
    assert rec is not None and rec.kind == "failed"
    assert rec.error is not None
