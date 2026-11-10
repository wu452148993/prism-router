# Handoff: prism-router worker-isolation refactor

> Self-contained brief. Spec locked through 5 rounds of codex critique
> (verdict: **SHIP**). Implementation landed alongside this doc.
>
> All paths relative to `D:\projects\prism-router\`. Local TZ UTC+8.

## TL;DR — what was actually wrong

`prism-router` had been treating the codex CLI's lifecycle as an **in-loop
asyncio coroutine** for over five iterations. Codex never promised the things
the in-loop code assumed: that it would emit progress during compaction, that
its stdout pipe would close promptly after `turn.completed`, that
`taskkill /T` would always reach every grandchild. Each time one of those
assumptions broke, a new layer of asyncio dance was bolted on
(`_terminate_with_shield`, `drain_grace`, `_post_terminal_idle_watchdog`, …).

The bug is the framing, not the codex CLI. Third-party process lifecycles
do not belong inside our event loop.

This refactor restores the process boundary that should have existed all
along:

```
main MCP loop ── spawns ──▶ Python worker (per call)
                              │
                              │  worker spawns codex
                              ▼
                              cmd.exe → node.exe → codex.exe + grandchildren
                              (all wrapped by Windows Job Object)
```

Main owns one universal kill switch: `CloseHandle(job)` →
`KILL_ON_JOB_CLOSE` → OS atomically reaps worker + codex + every grandchild.
Main never directly touches a codex pipe.

## Confirmed bug (Bug B, 2026-05-10)

The only failure mode actually observed and reproducible was a **post-
terminal cleanup deadlock**:

- Repro: `~/.codex/sessions/2026/05/10/rollout-2026-05-10T23-32-06-019e1284-63eb-7ad0-9b7d-6c69f9c05ba2.jsonl`
- Rollout tail: `task_complete` @ `16:24:31Z`
- prism MCP call did not return for 41 min after task_complete
- User cancelled at `17:05` local
- Same shape as the bug `3044b14` originally fixed (commit log: 33 min hang
  after `outcome=completed` per session ledger)

Mechanism: on Windows, codex's shell-tool grandchildren inherit the stdout
pipe write-end. After `codex.exe` exits, the pipe is still held; the
in-loop `readline()` never sees `ERROR_BROKEN_PIPE` (EOF). `proc.wait()`
also waits on pipe disconnect on the Windows asyncio transport, so the
main loop pinned. Earlier patches narrowed the window; they did not close
it for every codex grandchild configuration. This is the bug the refactor
structurally eliminates.

## Misdiagnosis recorded (Bug A, 2026-05-10)

Initial handoff drafts cited a second "pre-terminal idle hang" against
`rollout-2026-05-10T21-00-35-019e11f9-ac42-7a21-9911-b6db65d2ecb9.jsonl`.
**This was a misdiagnosis.**

Actual rollout tail (verified):
```
13:48:54  task_complete       ← turn 1 finished
13:53:35  task_started        ← turn 2 began
13:53:39  user_message
              ← 40 min 30 s of reasoning silence
14:34:09  task_started        ← turn 3
14:34:40  task_complete       ← clean terminal at end of file
```

The 40-minute silence between turns 2 and 3 was a single long reasoning
response from the OpenAI backend, not a hang. Reasoning models routinely
stream 20–40 min single responses while every local signal (CPU, file
mtime, dashboard counters) stays flat — see Appendix B for the diagnostic
criterion that distinguishes the two.

Practical implication: any pre-terminal idle watchdog kills healthy
long-reasoning runs. The refactor therefore **does not include** any
pre-terminal idle watchdog. `overall_timeout=7200s` is the only ceiling
before `task_complete` is observed.

## Locked architecture (D1–D8 after 5 review rounds)

| # | Decision |
|---|---|
| D1 | Single Content-Length-framed JSON in/out + L2 milestone events on stderr (`spawned`, `first_codex_output`, `terminal_event_seen`, `stdout_tick`, `stderr_tick`, `sidecar_published`, `terminating`, `terminated`). No streaming of codex payloads. |
| D2 | classify() runs in main. Worker emits raw `RunOutcome` JSON; main does schema validation + classify; on worker crash main synthesizes `transport_failure`. |
| D3 | Worker has ZERO wall-clock timers. ONE event-triggered coroutine: `codex_exit_drain=5s`, anchored to codex process exit (not to spawn time). |
| D4 | MCP cancel propagates JSON-RPC `-32001`. No envelope synthesis for cancel. Only `overall_timeout` / `post_terminal_grace` / `transport_failure` synthesize envelopes. |
| D5 | Single PR clean cutover. No feature flag dual-path. |
| D6 | Worker entry: `python -u -m prism_router.worker` + env `PYTHONNOUSERSITE=1` + `PYTHONUNBUFFERED=1`. No `-S` (would break venv/editable installs). |
| D7 | Main snapshots an env allowlist (codex/openai/proxy/cert/process essentials) before any await; worker receives it via `Popen(env=…)`. |
| D8 | Delete `docs/bugs/runner-hang-handoff.md` (abandoned patch plan). Keep `docs/bugs/codex-post-exit-hang.md` (original Bug B report). |

## Watchdog matrix (final state)

Main owns all wall-clock decisions:

- `overall_timeout = 7200 s` — global ceiling
- `pre_terminal_idle = 1200 s` (20 min default; env `PRISM_PRE_TERMINAL_IDLE`)
  — armed on `first_codex_output`, refreshed by `stdout_tick`, disarmed
  by `terminal_event_seen`. Added 2026-05-16 after empirical data showed
  the upstream codex CLI bug (task_complete written internally but
  `turn.completed` never reaches stdout, and `codex.exe` refuses to exit)
  still occurs at codex 0.130.0 on heavy reasoning tasks. The original
  decision to omit this watchdog (Appendix A) was based on a wrong
  assumption about how often legitimate silence exceeds 20 min. See
  `docs/bugs/pre-terminal-idle-watchdog.md` for data and rationale.
- `post_terminal_grace = 120 s` — armed by stderr `terminal_event_seen` milestone
- MCP cancel — re-raises CancelledError

Worker:
- `codex_exit_drain = 5 s` — event-triggered by codex process exit
- No other timers

## Process model

```
prism-router main (FastMCP stdio loop)
  per tool call → Dispatcher instance:
  ├─ env snapshot (allowlist) + per-call temp dir + probe-write 1 byte
  ├─ stdin preflight: framed body must be ≤ 4 MiB
  ├─ await asyncio.create_subprocess_exec(worker, ...)
  ├─ sync critical section (no awaits between):
  │     OpenProcess(pid) → CreateJobObject(KILL_ON_JOB_CLOSE) →
  │     AssignProcessToJobObject(job, hProcess) → CloseHandle(hProcess)
  ├─ async StreamWriter writes Content-Length-framed request JSON, drain
  ├─ stderr drain task (always on): parses milestone events, updates
  │     last_any_activity_ts + terminal_seen_ts, bounded 4 KiB chunked reads
  ├─ stdout read task with phased timeouts:
  │     Phase 1 (first byte): governed by overall_timeout + cancel + post_terminal_grace
  │     Phase 2 (header):    10 s absolute after first byte, ≤ 256 B
  │     Phase 3 (body):      10 s idle (no progress for 10 s), ≤ 4 MiB
  ├─ asyncio.wait([read_task, kill_event_task], FIRST_COMPLETED)
  │     • read_task valid → use it (kill_event ignored, except cancel always wins)
  │     • kill_event → CloseHandle(job); if `sidecar_published` seen, try recovery
  └─ classify(RunOutcome) → envelope → MCP response
```

```
worker (ephemeral, per-call)
  ├─ read Content-Length-framed stdin JSON
  ├─ asyncio.create_subprocess_exec codex
  ├─ feed prompt to codex stdin (StreamWriter + drain) → close stdin
  ├─ parser loop on codex stdout:
  │     first line → emit `first_codex_output` milestone
  │     coalesced stdout_tick / stderr_tick (≥ 2 s elapsed OR ≥ 200 lines)
  │     terminal event detected → emit `terminal_event_seen` milestone
  ├─ when codex_proc.wait() resolves (hard event, not wall-clock):
  │     drain stdout up to `codex_exit_drain = 5 s` for trailing bytes
  │     → assemble RunOutcome
  ├─ if RunOutcome > 1 MiB: write to sidecar (.tmp → fsync → rename),
  │     emit `sidecar_published`{size, sha256}, stdout frame = reference
  └─ exit 0 on delivery / 1 on internal exception
```

## Files

| File | Status |
|---|---|
| `src/prism_router/_job_object.py` | NEW — ctypes wrap (CreateJobObjectW, SetInformationJobObject(KILL_ON_JOB_CLOSE, non-inheritable, no breakaway), OpenProcess, AssignProcessToJobObject) |
| `src/prism_router/worker.py` | NEW — per-call worker entry; ZERO top-level side effects |
| `src/prism_router/dispatcher.py` | NEW — per-call Dispatcher; semaphore(4); janitor; phased timeouts; sidecar recovery |
| `src/prism_router/runner.py` | RESHAPED — stripped kill chain / shielded cleanup / drain race; kept spawn + stdin write + reader + parser feed + `codex_exit_drain` |
| `src/prism_router/server.py` | EDITED — `await run(...)` → `await Dispatcher().invoke(...)` |
| `src/prism_router/errors.py` | EDITED — added `post_terminal_grace`, `transport_failure`, `request_too_large`, `disk_unavailable`, `body_idle_timeout` |
| `tests/test_runner_pure.py` | REWRITTEN — milestone callback + codex_exit_drain tests; kill-chain tests dropped |
| `tests/test_worker.py` | NEW |
| `tests/test_dispatcher.py` | NEW |
| `tests/test_job_object.py` | NEW |
| `tests/test_server.py` | EDITED — monkeypatch `Dispatcher` instead of `run` |
| `tests/test_errors.py` | EDITED — added new codes to MISSING bucket |
| `docs/bugs/runner-hang-handoff.md` | DELETED (abandoned patch plan; this doc supersedes) |
| `docs/bugs/codex-post-exit-hang.md` | KEPT (original Bug B report) |

## Historical patch audit

The 5 commits `cecb85e`, `3044b14`, `242de19`, `3cfacd9`, `b01efc0` all
correctly targeted Bug B class or are defensive evidence pins. None were
misdiagnoses. Each gets structurally subsumed by worker isolation:

| Commit | Subsumed by |
|---|---|
| `cecb85e` (cancel kill chain) | `CloseHandle(job)` |
| `3044b14` A' (drain_grace) | `codex_exit_drain` in worker |
| `3044b14` C (post_terminal_idle_watchdog) | `post_terminal_grace` in main |
| `3044b14` E (drain_timeout/idle_after_terminal codes) | Retained as legacy aliases in `errors.py` |
| `242de19` / `3cfacd9` (terminal event pins) | `terminal_event_seen` parser still depends on these |
| `b01efc0` (SID asymmetry) | Unrelated to hang class; untouched |

## v1.1 follow-ups (from codex round 5)

1. Optional stdin sidecar for >4 MiB prompts (`request_too_large` is hard
   reject in v1).
2. Raw terminal-like fallback for parser misses (extra arming signal).
3. `body_idle_timeout` / `codex_exit_drain` configurable via env.
4. Startup-hook trust deployment guidance for shared CI / multi-tenant hosts.
5. Janitor observability (log/metric counter).

## Appendix A — Why `pre_terminal_idle` was reinstated (2026-05-16)

**Original claim (now refuted):** reasoning-model single responses on the
OpenAI backend can hold a streaming HTTP connection for 20–40 minutes (and
longer) while:

- `codex.exe` shows CPU ≈ 0 (main thread in IOCP wait on SSE bytes)
- Windows `ReadOperationCount` does NOT advance for TLS socket async reads
- Rollout file mtime is frozen (reasoning chunks batch-flush at response end)
- OpenAI dashboard shows 0 calls in this window (Usage entry is on completion)

Any pre-terminal idle threshold ≤ 90 min would produce deterministic
false-positive kills of healthy long reasoning. The only signal that
distinguishes "real cleanup deadlock" from "long reasoning" is the rollout
file's terminal event record — see Appendix B.

**Why this turned out to be wrong:** the 20-40 minute silence figure was an
upper bound on *legitimate* silence, but the watchdog wasn't competing with
*all* legitimate silence — only with the silence-then-recover case. Empirical
data (1210 historical codex MCP calls) shows:

- Successful call total duration p95 = 31.7 min (so 95% of legitimate work
  fits comfortably under a 20-30 min threshold)
- Missing-result (hang) rate = 35% — far higher than expected; the 2h
  overall_timeout dominates the user wait time
- The hang reproduces at codex 0.130.0 under heavy reasoning workloads

The new watchdog at 1200s (20 min) default + `PRISM_PRE_TERMINAL_IDLE=3600`
escape hatch is the chosen tradeoff: small false-kill risk in exchange for
~5x recovery speed-up on hangs. See `docs/bugs/pre-terminal-idle-watchdog.md`
for the full rationale and threshold sensitivity table.

## Appendix B — Bug B vs healthy reasoning: triage criterion

```
prism still stuck  AND  rollout tail has task_complete   →  REAL bug (Bug B)
prism still stuck  AND  rollout tail has NO task_complete →  normal reasoning, do not touch
```

Locate the rollout:

```bash
find ~/.codex/sessions -name "*<CODEX_SESSION_ID>*"
# Windows: /c/Users/<user>/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-*-<SID>.jsonl
```

Single-line check:

```bash
tail -n 1 <rollout>.jsonl | grep -o '"task_complete"' && echo TERMINATED || echo STILL_REASONING
```

Real-bug fingerprint additionally requires:

1. Rollout ends with `task_complete` (codex's session record says "done").
2. Rollout file mtime frozen ≥ `post_message + drain_grace + overall_timeout/2`
   (default ≈ 60 + 3 + 3600 ≈ 60 minutes).
3. prism MCP call still pending; `cmd.exe → node.exe → codex.exe` chain or
   residue still in process list.

Anti-patterns (do NOT use):

- 5-second CPU / I/O sampling to call "dead" — falls inside the SSE silent
  window and produces false positives.
- `codex resume <SID>` as a liveness probe — forks a separate process reading
  the same rollout; concurrent writes corrupt the file.
- OpenAI dashboard zero-call inference — a 22-minute in-flight streaming
  response shows 0 completed calls until it finishes.
- `taskkill` while prism elapsed < 60 min — well below reasoning-model single
  response upper bound.

The `taskkill` previously suspected of "uninterruptible IRP state" silence
is in fact a snapshot race: `taskkill /T` enumerates child PIDs at scan
time, but `node.exe` can spawn new grandchildren after the snapshot, which
the kill then misses. This is a known Windows behavior, not a codex issue.

## Pointers

- Previous (abandoned) handoff: `docs/bugs/runner-hang-handoff.md` — to be deleted with this commit
- Original Bug B report: `docs/bugs/codex-post-exit-hang.md`
- Bug B repro ledger: `~/.codex/sessions/2026/05/10/rollout-2026-05-10T23-32-06-019e1284-…jsonl`
- Bug A misdiagnosis ledger (DO NOT cite as hang): `~/.codex/sessions/2026/05/10/rollout-2026-05-10T21-00-35-019e11f9-…jsonl`
- Recent runner.py patch commits: `b01efc0`, `3cfacd9`, `242de19`, `3044b14`, `cecb85e`
