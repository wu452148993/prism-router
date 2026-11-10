# Pre-terminal stdout-idle watchdog

Date: 2026-05-16
Status: shipped
Codex CLI versions affected: confirmed at 0.130.0 (and earlier); upstream behavior at higher versions unknown.

## Symptom

Long-running codex MCP calls hang indefinitely after codex has internally
completed the task. The user's Claude Code session waits up to 2 hours
(`overall_timeout=7200s`) before `prism-router` returns a `timeout` error and
the session unblocks. During the wait:

- `codex.exe` process is alive (visible in `wmic`/`tasklist`)
- The native `cmd.exe → node.exe → codex.exe` chain is intact, no orphans
- Codex's own rollout JSONL (`~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl`)
  records a final `event_msg/task_complete` envelope at the moment the model
  finished — but stdout never sees `turn.completed` and `codex.exe` never
  exits.
- prism-router's `worker` python process is blocked at
  `runner.py:await wait_task` (`proc.wait()` of the codex shim chain).

Original incident: CC session `9ab5582b...` invoked `mcp__prism__codex` at
`2026-05-16 00:09:13 +0800`; codex completed internally at `00:50:31` but the
entire chain stayed alive until `02:09:13` when `overall_timeout` fired. The
upstream codex CLI was then upgraded to 0.130.0; smoke tests succeeded but
heavier walkthrough prompts reproduced the same hang at the new version.

## Root cause

Upstream codex CLI bug. `codex exec --json` is supposed to emit
`turn.completed` / `thread.completed` on stdout when a turn ends — that's the
only signal `prism-router`'s parser uses to flip `saw_terminal=True` and
trigger orderly shutdown. For some heavy reasoning workloads, codex 0.130.0
writes `task_complete` only to its internal rollout file and never to stdout.
The process then declines to exit, blocking `proc.wait()` forever.

`prism-router` is downstream and cannot patch the codex CLI. The fix has to
live in `prism-router` as an output-idle watchdog with a configurable bound.

## Why prior safety nets didn't catch this

| Safety net | Trigger | Why this hang escapes it |
|---|---|---|
| `codex_exit_drain` (5s, in runner) | proc.wait() resolves | proc.wait() never resolves — codex.exe doesn't exit |
| `post_terminal_grace` (120s, in dispatcher) | stderr `terminal_event_seen` milestone | milestone is gated on `parser.saw_terminal`, which only flips on stdout `turn.completed` — never emitted |
| `drain_timeout` / `idle_after_terminal` (pending hang-fix.diff) | post-terminal pipe held | same gate — saw_terminal must flip first |
| `overall_timeout` (7200s) | global ceiling | works, but 2h wait |
| MCP cancel | user-initiated | bypasses the wrapper-side question |

The architectural gap: every watchdog assumes codex will at least emit a
terminal event. The hang breaks that assumption.

## The previous "NO pre-terminal idle watchdog" rule

`dispatcher.py:23-27` explicitly forbade a pre-terminal idle watchdog with
the rationale that reasoning models can stream silent responses for 20–40
minutes (see `worker-isolation-handoff.md` Appendix A). That rationale held
when the only available evidence was a single observed silent run at
40 min — but it gated against an entire class of hang detection without
considering that:

1. The threshold question is not "longest legitimate silence ever" but
   "smallest threshold that wins on aggregate ROI". A small false-kill rate
   is acceptable when it cuts 100min of wait time per hang.
2. The false-kill is recoverable: `PRISM_PRE_TERMINAL_IDLE=3600` env override
   + retry.
3. The 35% hang rate is too high to leave unaddressed.

## Empirical data

Source: agentsview SQLite index across 259 Claude Code sessions, 1210
codex MCP tool calls (`mcp__codex__codex` 982 + `mcp__prism__codex` 228).
Duration = call message timestamp → next message timestamp.

### Outcome distribution

| outcome | n | % |
|---|---|---|
| success (envelope.success=true) | 439 | 36% |
| failure (envelope.success=false) | 213 | 18% |
| missing tool_result (hang / wrapper-eaten cancel) | 418 | 35% |
| user cancel (-32001) | 40 | 3% |
| CC oversize persist | 65 | 5% |

### Successful call duration percentiles (n=432)

| pct | duration |
|---|---|
| p50 | 6.7 min |
| p75 | 14 min |
| p90 | 25.5 min |
| p95 | 31.7 min |
| p99 | 60.5 min |
| max | 111.6 min |

Top successful calls (46-111 min) are genuine long reasoning, not big
prompts — prompt length 550-7278 chars is uncorrelated with duration.

### Real-hang duration percentiles (n=214, missing tool_result with duration > 2 min)

| pct | duration |
|---|---|
| p50 | 12.2 min |
| p75 | 27.5 min |
| p90 | 52.9 min |
| max | 120.9 min (= `overall_timeout=7200s` ceiling) |

### Threshold tradeoff

`%successful total > T` is an UPPER bound on false-kill — actual max
internal silence within a successful call is bounded well below total (codex
streams reasoning summaries and tool calls periodically).

| Threshold | Successful total > T (upper bound false-kill) | Real hangs killed earlier (savings) | Conservative false-kill (≤50% of total assumption) |
|---|---|---|---|
| 10 min | 35.2% | 58.9% | ≤ 13.9% |
| 15 min | 22.2% | 42.5% | ≤ 6.2% |
| **20 min** | **13.9%** | **33.6%** | **≤ 2.8%** |
| 30 min | 6.2% | 22.4% | ≤ 1.2% |
| 45 min | 2.5% | 12.1% | ≤ 0.5% |
| 60 min | 1.2% | 8.9% | ≤ 0.0% |

## Decision

`PRE_TERMINAL_IDLE_S = 1200.0` (20 min) default; env override
`PRISM_PRE_TERMINAL_IDLE` for known long-silence workloads (set to
2700/3600 if false kills appear).

Reasoning trail:

1. First pass (no data): 45 min was codex's "smallest defensible" via
   the 20-40 min silence claim from the old design notes.
2. After data: a more aggressive 20 min default kills 33.6% of hangs much
   faster, with a conservative false-kill upper bound of 2.8%.
3. The operator (single-user wrapper) explicitly accepted the false-kill
   tradeoff in exchange for faster recovery.
4. Codex review (round 3) agreed 20 min is the right call given operator
   preference and the absence of multi-tenant safety pressure.

## Implementation

Files touched in the same commit (per project memory rule "bug-doc and fix
code in the same commit"):

| File | Change |
|---|---|
| `src/prism_router/errors.py` | Added `pre_terminal_idle` to `ErrorCode` + `ALL_ERROR_CODES` |
| `src/prism_router/runner.py` | Split `activity_tick` coalescer into stdout/stderr halves emitting `stdout_tick` / `stderr_tick` (legacy `activity_tick` still emitted alongside for one release of compat). Coalescer interval/burst made injectable for tests. |
| `src/prism_router/dispatcher.py` | Added `PRE_TERMINAL_IDLE_S` constant (env `PRISM_PRE_TERMINAL_IDLE`); `Dispatcher.__init__` accepts `pre_terminal_idle`; `_WorkerState` gains `last_stdout_activity_ts` + `max_stdout_silence_s`; `_drain_stderr` wires `first_codex_output` → arm, `stdout_tick` → refresh, `terminal_event_seen` → disarm; new `_arm_pre_terminal_idle` coroutine; `_build_kill_result` adds `pre_terminal_idle` branch and forces error path even when partial outcome was recovered (per codex round 2 mitigation: never classify as success with partial text); success path logs `max_stdout_silence_s` via `_LOG.info` for future tightening data. |
| `src/prism_router/classify.py` | Added `pre_terminal_idle` between `timed_out` and `drain_timeout` in the priority ladder (pre-terminal kills outrank post-terminal kills). |
| `docs/bugs/worker-isolation-handoff.md` | Replaced the "NO pre-terminal idle watchdog" rule and Appendix A; cross-linked here. |
| `tests/test_errors.py` | Updated audit-bucket coverage. |
| `tests/test_classify.py` | Added 3 priority-ladder tests for the new code. |
| `tests/test_dispatcher.py` | Added 4 watchdog tests: kills on stdout silence; does not arm without `first_codex_output`; disarmed by `terminal_event_seen`; `pre_terminal_idle=0` disables. |
| `tests/test_runner_pure.py` | Added test asserting `stdout_tick`/`stderr_tick` are emitted in addition to legacy `activity_tick`. |

## How to retry on a false kill

```bash
# Bump threshold for the affected shell only
export PRISM_PRE_TERMINAL_IDLE=3600   # 60 min — for known long pure reasoning
# Or session-global if you want
$Env:PRISM_PRE_TERMINAL_IDLE = "3600"  # PowerShell
```

The error envelope's `raw` field includes this hint.

## Future tightening

Successful calls now log `max_stdout_silence_s`. Once enough samples
accumulate (e.g. 1-2 weeks of usage), if the observed legitimate p99 silence
is well under 1200s, the default can be lowered toward 600s. No
architectural change needed — just a constant update.
