# Rollout recovery for pre_terminal_idle

Date: 2026-05-18
Status: shipped
Codex CLI version pinned: 0.130.0 (rollout schema verified against `openai/codex@main`
files in §Citations).

## Symptom

When the [pre_terminal_idle watchdog](pre-terminal-idle-watchdog.md) fires,
`mcp__prism__codex` returns a `pre_terminal_idle` error envelope with empty
`agent_messages` — even when codex had **already produced** the final answer.

The answer was written to codex's own per-session JSONL ledger at
`~/.codex/sessions/YYYY/MM/DD/rollout-<ISO-TS>-<UUID>.jsonl` (the rollout file).
It just never reached prism via stdout. The upstream CLI bug stalls stdout
flushing while leaving the rollout writer healthy.

## Concrete incident

Claude CLI session `97255fd4-d167-498a-8c6c-34edf1c26131` invoked
`mcp__prism__codex` four times for a Phase 2 design review. Three calls
returned `pre_terminal_idle`. The last one mapped to codex rollout
`019e38de-f488-7dd0-affd-087f748888f8`:

```
spawned             t=0s     (2026-05-18T02:16:35Z)
first_codex_output  t+0.4s
stdout_tick         t+73s    ← last stdout tick before silence
stderr_tick         t+104s
                             ← 1200s of silence
prism kill          t+1304s
```

Codex's rollout file at the same call:
- `session_meta` written at `2026-05-18T02:16:39Z`
- **`task_complete`** with full `last_agent_message="**Critical Findings**\n1. [BLOCKER]..."`
  written at `2026-05-18T02:24:37Z` (t+478s)
- Nothing after — file stable for the remaining ~14 minutes prism waited.

So codex internally completed the turn 14 minutes before prism gave up. The
agent_message was on disk the whole time.

## Root cause

Upstream codex CLI 0.130.0 stdout-flush bug: heavy reasoning workloads can
trigger a state where stdout stops accepting writes (or buffers them past
codex exit) while the rollout writer keeps writing per-event. The
[pre_terminal_idle watchdog](pre-terminal-idle-watchdog.md) correctly detects
the stdout death; but the original implementation threw away the answer
codex had already persisted.

prism cannot patch the codex CLI. The fix has to live in prism as a
post-watchdog recovery path that reads codex's own rollout file.

## Why we can identify the right rollout file

Codex generates a `Uuid::new_v4()` thread_id at the start of each `exec`
invocation and emits it as the first stdout event (`thread.started`). prism's
`StreamParser` already captures this on first-event admit (`parser.py:82-85`),
and the parser stores it as `session_id`. The same UUID appears in the
rollout filename suffix (`recorder.rs:1329-1348`). One UUID, two surfaces:
stdout (early) and rollout filename (durable).

We surface the captured thread_id to the dispatcher via a new `thread_id`
milestone (runner-emitted on first admit). When `pre_terminal_idle` fires
later, the dispatcher already has the UUID and can glob
`~/.codex/sessions/**/rollout-*-<UUID>.jsonl`.

We do NOT pre-allocate the session_id and pass it to codex; codex CLI 0.130.0
has no flag for that. Only `exec resume <SID>` accepts a session_id, and
strictly for continuation (verified at three layers: local `codex exec --help`,
`backend_codex.py:40-43` inline note, and DeepWiki query into
`codex-rs/exec/src/cli.rs`).

## Decision

When `kill_reason == "pre_terminal_idle"`:

1. **Sidecar preference (defensive)** — if the worker already wrote a
   sidecar `RunOutcome` AND its parser legitimately saw a terminal event,
   prefer it (rebuilt with `returncode=0`). Covers a tiny late-delivery race
   where the worker finished parsing but dispatcher missed the reference
   frame; the worker's view is richer than rollout's `last_agent_message`.
2. **Rollout recovery** — bounded by `asyncio.wait_for(5s)` in a thread
   pool. Walks the rollout JSONL bottom-up for the final `task_complete` /
   `task_failed` / `error` payload (F1 guarantees ≤1 per `exec`), fenced to the
   current turn by an invocation timestamp baseline plus a `task_started` stop
   — a resumed session's earlier turns live in the same file and must never
   answer for this one
   ([rollout-recovery-cross-turn-leak.md](rollout-recovery-cross-turn-leak.md)).
   - `task_complete` with non-empty `last_agent_message` → synthesize
     `RunOutcome` with `returncode=0`, `saw_terminal=False`,
     `session_id=thread_id`. Return success with
     `diagnostics.recovered_from_rollout=True`.
   - `task_failed` or `error` → surface a `codex_error` envelope with
     codex's own error message in `raw`. Marked recovered for ops trail.
   - Empty `last_agent_message` or no terminal event in rollout → fall
     through to the existing `pre_terminal_idle` error envelope. Refusing
     to claim empty success is intentional — empty `agent_messages` is
     strictly worse than a visible `pre_terminal_idle` error.
3. **Otherwise** — original `pre_terminal_idle` error.

### Why `returncode=0` on the synthesized outcome

The dispatcher kills the worker; `proc.returncode` after kill is non-zero.
But `classify.py` ordering checks
`returncode != 0 and not parse_result.saw_terminal and not error_events`
**before** the success return — that would re-fire `spawn_failed` on a
recovered outcome. Forcing `returncode=0` reflects that the kill was
dispatcher-induced after a complete result existed on disk; semantically
this is a successful outcome that the runner-level rc cannot describe.

### Why `saw_terminal=False`

Honest. The codex stdout NEVER carried `turn.completed`; we got the answer
from the rollout file instead. Forging `saw_terminal=True` would
mis-represent the transport state to anything that inspects the parse
result downstream.

### Disable switch

`PRISM_ROLLOUT_RECOVERY=0` in the process env disables the recovery path
entirely. Read at call time (not module load), so a server restart-free
toggle works at the cost of mutating the env of an already-running process.

## Implementation

| File | Change |
|---|---|
| `src/prism_router/parser.py` | Expose `_session_id` as a `@property` so runner can read it pre-terminal. |
| `src/prism_router/runner.py` | Relax `MilestoneCallback` to `Callable[..., None]`. In `_read_stdout`, emit `on_milestone("thread_id", thread_id=parser.session_id)` once after the parser captures it. |
| `src/prism_router/_rollout_recovery.py` | New module. `find_rollout_path` (UUID-suffix glob with OSError-tolerant stat/glob), `read_terminal_event` (bottom-up scan, trailing-partial tolerant), `recover` (env gate + lookup + read). |
| `src/prism_router/dispatcher.py` | New `_WorkerState.codex_thread_id`; new `thread_id` milestone branch in `_drain_stderr`; in `_run_worker`, run recovery via `asyncio.to_thread + wait_for(5s)` before building the kill result; `_build_kill_result` gains rollout/sidecar branches under `reason == "pre_terminal_idle"`; new `_synthesize_recovered_outcome` (raises `RuntimeError` on missing thread_id — real raise, not `assert`, because `python -O` strips asserts). |
| `src/prism_router/errors.py` | `Diagnostics` TypedDict gains `NotRequired` keys: `recovered_from_rollout`, `rollout_path`, `max_stdout_silence_s`. |
| `src/prism_router/server.py` | `_envelope` accepts `extra_diagnostics`; emits diagnostics when set even if `return_all_messages=False` and `error is None` (otherwise recovery signal is silently dropped on the default path). Only whitelisted keys (`_PUBLIC_DIAG_KEYS`) surface; internal dispatcher fields stay private. Both `_invoke_codex` and `_invoke_gemini` pass `extra_diagnostics=result.diagnostics`. |
| `tests/test_parser.py` | `session_id` property exposed mid-stream + None pre-event + unchanged on subsequent events. |
| `tests/test_runner_pure.py` | `thread_id` milestone emitted once with `thread_id` kwarg, after `first_codex_output`. Not emitted when init event absent. Existing `seen.append` callers wrapped in a `_milestone_recorder` shim that swallows kwargs. |
| `tests/test_rollout_recovery.py` | New file. 17 tests: glob/stat races, newest-mtime tiebreak, CODEX_HOME env, three terminal kinds, trailing-partial tolerance, empty-message refusal, env disable, end-to-end. |
| `tests/test_dispatcher.py` | 7 new tests: recovery success on `task_complete` (default `return_all_messages=False`), no thread_id captured → fallback, no rollout file → fallback, `task_failed` → `codex_error`, env disabled → fallback, empty message → fallback, synthesizer raises `RuntimeError` without thread_id. |
| `tests/test_server.py` | New tests: `_envelope` surfaces recovery diagnostics on default path; normal success path stays unchanged; end-to-end through `classify` does not misfire `spawn_failed` on synthesized outcome. |
| `tests/test_errors.py` | `Diagnostics` accepts `NotRequired` recovery keys. |
| `tests/fixtures/rollouts/` | Six fixtures: `complete`, `failed`, `error_event`, `truncated`, `no_terminal`, `empty_message`. |
| `docs/mcp-audit/samples/codex-pre-terminal-idle-recovered.txt` | Synthesized post-fix envelope using the incident's real thread_id and recovered text. Marked clearly as synthesized. |

## Limitations

- **`return_all_messages=True` on recovered success** returns `all_messages=[]`.
  The raw event stream stalled before stdout could carry intermediate events;
  the rollout's chronology is per-event but in a different schema we don't
  translate back. Only the final `last_agent_message` is recovered.
- **`--ephemeral`** (codex flag that disables session persistence) means no
  rollout file exists. Recovery returns None and the user gets the original
  `pre_terminal_idle`. We don't currently pass `--ephemeral`; documented for
  future awareness.
- **Multiple codex sessions in the same cwd** are matched by UUID
  (globally unique), not by cwd — concurrent codex spawns are safe even
  when their `--cd` targets overlap.
- **Recovery wait budget**: 5s wall-clock via `asyncio.wait_for`. This
  bounds dispatcher wait time, not the underlying filesystem operation;
  the thread continues but its result is discarded. Acceptable for a
  20-minute failure path.
- **Stale `post_terminal` check** in `server.py:169` (`post_terminal_killed=any(s.reason == "post_terminal" ...)`)
  predates this change. Actual reason names today are `post_terminal_grace`.
  The check is currently a no-op; rename deferred to a separate commit per
  project memory rule (bug-doc + fix together).

## Rollback

```powershell
$Env:PRISM_ROLLOUT_RECOVERY = "0"   # process env, picked up at call time
```

Or revert the commit. The recovery path is additive — disabling it returns
exactly the pre-change error envelope shape.

## Citations

- `codex-rs/exec/src/exec_events.rs:10-14` — `thread.started` is the first event
- `codex-rs/exec/src/exec_events.rs:40-42` — `ThreadStartedEvent.thread_id`
- `codex-rs/exec/src/event_processor_with_jsonl_output.rs:394-397` —
  stdout `thread_id` comes from `SessionConfiguredEvent.thread_id`
- `codex-rs/rollout/src/recorder.rs:1329-1348` — rollout filename
  `sessions/YYYY/MM/DD/rollout-{date_str}-{conversation_id}.jsonl`
- `codex-rs/protocol/src/protocol.rs:1308-1316` — `TurnComplete` serializes
  as `task_complete`

## Related

- [`pre-terminal-idle-watchdog.md`](pre-terminal-idle-watchdog.md) — the watchdog
  that triggers recovery; threshold rationale and empirical data
- [`codex-post-exit-hang.md`](codex-post-exit-hang.md) — earlier upstream
  symptom (codex exits but stdout pipe held); F1 verifies the single-terminal
  guarantee this recovery relies on
