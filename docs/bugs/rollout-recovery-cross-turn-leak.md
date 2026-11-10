# Rollout recovery could return the PREVIOUS turn's answer

Date: 2026-08-11
Status: shipped
Introduced by: `7f26af1` (rollout recovery), 2026-05-18

## Symptom

On a resumed session (`SESSION_ID` passed by the caller → `codex exec resume
<SID>`), a `pre_terminal_idle` kill could return `success:true` carrying the
answer to the *previous* turn's question. The envelope is indistinguishable
from a normal success: real SESSION_ID, non-empty `agent_messages`,
`error:null`. In a review pipeline that reads as a verdict.

## Root cause

`codex exec resume` appends every turn to the SAME rollout file, so one file
holds `task_started`/`task_complete` pairs for turn 1, turn 2, … (real example:
`~/.codex/sessions/2026/08/10/rollout-…-019feacd-….jsonl` holds three turns).

`_rollout_recovery.read_terminal_event` walked the file bottom-up and accepted
the first terminal payload it met, with nothing binding that payload to the
turn prism had just launched. If the watchdog fired while the current turn was
still in flight — exactly the case the recovery path exists for — the scan ran
past the current turn's `task_started` and found the previous turn's
`task_complete`. `dispatcher._synthesize_recovered_outcome` then built an
outcome with `returncode=0`, classify found nothing wrong, and the stale text
shipped as this turn's answer.

## Reproduction (real data)

Take the two-turn rollout
`~/.codex/sessions/2026/08/11/rollout-2026-08-11T02-52-20-019fed04-….jsonl`
(turn 1 `task_complete` = 4818 chars; turn 2 `task_complete` =
`last_agent_message: null`). Truncate it just after turn 2's `task_started` —
i.e. the state on disk when the watchdog kills a turn mid-flight — and call
`read_terminal_event`: pre-fix it returns turn 1's 4818-character message,
`kind="complete"`.

Trigger conditions are ordinary, not exotic: caller passes `SESSION_ID` (ACF
does), and the watchdog fires before the current turn writes its terminal
payload (observed the same day: a session still emitting reasoning to its
rollout at 20:02:55Z was killed at 20:02:56Z).

## Fix

Two independent guards in `read_terminal_event`, because neither covers the
other's window:

1. **Temporal (primary).** `recover(..., not_before=<epoch>)` receives the wall
   clock the dispatcher takes immediately before spawning the worker, and a
   terminal payload is accepted only if its own rollout `timestamp` is no more
   than `_TS_RESOLUTION_S` (1 ms) behind it — rollout timestamps carry three
   decimals, so a record written inside the baseline's millisecond would
   otherwise read as older purely from truncation. Earlier turns finished
   before we spawned, so the guard holds whatever the file's shape. A payload
   whose timestamp is missing, unparseable, or offset-naive fails closed:
   ownership cannot be proven, so recovery declines rather than guessing a
   timezone.
2. **Structural.** The backward scan returns `None` on meeting `task_started`:
   the current turn started and wrote no terminal payload. Clock-independent,
   and it short-circuits the common case. Positional rather than
   `turn_id`-matching — prism never learns the current `turn_id`, and "the last
   `task_started` is the turn we launched" holds because prism runs exactly one
   turn per `codex exec` invocation.

The structural guard alone is not enough: codex writes setup records
(`thread_settings_applied`) on resume *before* `task_started`, so a kill landing
in that window walks straight past them into the previous turn's
`task_complete`. In the real 2026-08-10 rollout that window is ~6 ms wide, but
a codex that wedges during session load holds the file in exactly that state
for as long as the watchdog waits.

Cost: recovery now declines in one case where it used to succeed — a turn whose
`task_started` was written but whose `task_complete` is missing *because codex
never flushed it*, while an earlier turn's answer sits above. Declining is
correct there; that earlier answer is not the answer to this question. Second
cost: if a future codex build drops or reformats rollout `timestamp`, recovery
degrades to always-decline rather than risking a stale answer.

## Residual: concurrent calls on one SESSION_ID

The baseline proves chronology, not ownership. prism runs up to
`dispatcher.MAX_CONCURRENT` invocations with no per-session lock, so two calls
can `codex exec resume <SID>` into the same rollout. If A stalls to the
watchdog while B completes, B's terminal payload postdates A's baseline and A
would adopt B's answer. Not fixed here: serializing per SESSION_ID changes the
dispatcher's concurrency semantics, which this change is scoped not to touch,
and two live turns interleaving into one codex thread is already undefined
upstream. Callers must not fan out concurrent calls on one SESSION_ID; a
per-session lock is the follow-up if they ever do.

A backward wall-clock step (NTP or VM rollback) between the previous turn's
terminal and this spawn would weaken the temporal guard the same way. The
structural guard is unaffected once `task_started` is written.

## Notes

- Fresh (non-resumed) sessions were never affected: one turn per file.
- Found by codex review round 2 of the
  [transient retry storm](codex-transient-retry-storm.md) change, then
  confirmed against real rollouts before fixing.
- The 74 rollout-recovered successes in the corpus were not audited for
  staleness; the mechanism is proven but its historical hit rate is unmeasured.

## Verification

Fixture `tests/fixtures/rollouts/resumed_two_turns.jsonl` (turn 1 complete with
text → `thread_settings_applied` → turn 2 started → turn 2 complete), truncated
per case in `tests/test_rollout_recovery.py`:

| test | file state at kill | expected |
|---|---|---|
| `..._accepts_current_turn_of_resumed_session` | turn 2 complete | turn 2's answer |
| `..._declines_when_current_turn_started_but_unfinished` | cut after turn 2 `task_started` | `None` (structural) |
| `..._declines_before_current_turn_starts` | cut after `thread_settings_applied` | `None` (temporal) |
| `..._declines_terminal_without_parseable_timestamp` | timestamp stripped | `None` (fail closed) |

`test_read_terminal_millisecond_slack_boundary` pins the temporal fence:
accepts 0.999 ms and exactly 1 ms behind the baseline, declines 1.001 ms
(offsets derived from `_TS_RESOLUTION_S`, so they cannot drift from it).
`test_read_terminal_declines_offset_naive_timestamp` uses `not_before=0` so the
age fence cannot mask the offset-naive rejection. Both were mutation-checked:
deleting the `tzinfo is None` rejection fails the naive-timestamp test, and
weakening `<` to `<=` fails the exact-boundary case.

The dispatcher-level recovery tests call `_restamp_now(rollout)` from their
fake spawner, so the rollout is written AFTER the dispatcher takes its baseline
— the production order. Without it those tests would be silently fenced off,
which is how the gate was confirmed live. Full suite: `pytest -m ""` →
422 passed, 2 skipped.
