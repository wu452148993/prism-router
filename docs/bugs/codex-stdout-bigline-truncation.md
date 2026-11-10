# Oversized codex stdout line silently kills the stdout reader

Date: 2026-08-11
Status: shipped
Codex CLI version: 0.144.1 (npm `@openai/codex`)

## Symptom

A healthy codex session goes stdout-dark mid-turn. prism receives no further
events — no `turn.completed`, no final `agent_message` — while codex keeps
working and keeps writing its rollout. 20 minutes later the dispatcher's
`pre_terminal_idle` watchdog kills a session that never stopped making
progress. Observed 2026-08-11: rollout still being written at 20:02:55Z, kill
at 20:02:56Z, last stdout event at 19:42:56Z.

## Root cause

`runner._read_stdout` read codex stdout with `asyncio.StreamReader.readline()`.
`asyncio.create_subprocess_exec` defaults to `limit = 65536` (64 KiB) per pipe.
`codex exec --json` writes every shell command's **complete, untruncated**
stdout into one `item.completed{item.type=command_execution}` line
(`aggregated_output`). Measured: a 116-byte prompt asking codex to run
`rg -n '' uv.lock` (219,728-byte file) produced a **232,127-byte** stdout line,
of which `aggregated_output` was 225,796 bytes. The caller's PROMPT size is
irrelevant — the stream does not echo it.

Past the limit `readline()` raises
`ValueError: Separator is not found, and chunk exceed the limit`. `_read_stdout`
had no handler, so the task died; `run()`'s teardown
`gather(stdout_task, stderr_task, return_exceptions=True)` swallowed the
exception. Completely silent.

Two downstream outcomes:

1. No terminal event, no final message → the turn dies on `pre_terminal_idle`,
   or (if codex exits cleanly first) is caught by the `empty_final` gate.
2. **Worse, and not covered by `empty_final`:** codex emits intermediate
   `agent_message` events *before* it runs the tool. Those are already joined
   into `agent_messages`. So the envelope is `success:true` with a non-empty
   but truncated preamble, indistinguishable downstream from a real answer.
   The first call of the 2026-08-10 incident carried exactly this — 906 chars
   of preamble.

Isolation experiment (fake codex, one oversized line between two messages):

| line length | events parsed | saw_terminal | agent_messages |
|---|---|---|---|
| 30,000 B | 4 | True | `AFTER-BIG` |
| 70,000 B | 1 | False | empty |
| 200,000 B | 1 | False | empty |
| 200,000 B + `limit=4 MiB` | 4 | True | `AFTER-BIG` |

Initial triage over two days (2026-08-10/11) found the "stdout wedged" sessions
were review agents running repeated `rg` over a repo, and did not see the
pattern in sessions whose tool output stayed at ~17 KB. Small sample, absolute
wording not warranted — see Historical attribution for the measured
association.

### Do not infer line length from the rollout

Codex writes a **model-facing truncated** `custom_tool_call_output` to the
rollout (~41 KB in practice); the `--json` stdout event carries the full
225,796 bytes for the same command. An order of magnitude apart. An earlier
investigation drew the wrong conclusion from rollout sizes.

## Fix

`src/prism_router/runner.py`, two parts — either alone is insufficient.

1. **`_default_spawn` passes `limit = 4 MiB`**, aligned with the dispatcher's
   `STDOUT_FRAME_CAP`. Covers the observed magnitude.
2. **`_iter_lines` catches the `ValueError` and keeps reading.** Raising the
   limit only moves the threshold — `aggregated_output` has no upper bound and
   one `cat` of a large file defeats any constant. Degrade path is fixed and
   narrow: catch → drop the line → one trace → continue. For stdout the trace
   goes through the parser into `noise_log`, for stderr into the tail buffer.
   No config knob, no envelope field, no reassembly, no retry. **The stream
   must never stop silently.**

Nothing is skipped after the raise. asyncio has two overrun variants: when the
separator was already buffered it discards the whole line, so the next
`readline()` is a real event and skipping it would re-create the bug we are
fixing. When the separator was not yet seen the buffer is cleared mid-line and
the leftover tail surfaces as one unparsable line, which the parser files as
noise. The trace fires per discarded buffer rather than per logical line — the
two variants are only distinguishable by message text, and under-reporting
would let a stream that is still emitting oversized lines look idle to the
watchdog. Traces are capped at 100 per unbroken run of drops (the parser's own
`NOISE_LOG_CAP`): reading never stops, but a reader that raises without
consuming cannot flood milestones and refresh `pre_terminal_idle` forever.

A drop also bumps the stdout activity coalescer: a multi-hundred-KB line takes
real time to arrive and would otherwise read as silence to `pre_terminal_idle`.

## Historical attribution

Re-audited 2026-08-11 over `~/.codex/sessions` rollouts plus retained CC
transcripts; window 2026-07-07..08-11 (codex 0.144.1), n=842
`mcp__prism__codex` calls. **Everything below is correlational — none of it is
causal proof.** Full tables and a 6-item insufficient-evidence list lived in a
machine-local untracked scratch (`.git/acf/codex-stdout-bigline-history.md`)
that is *not* part of this repository.

- **Incidence.** 123/842 (14.6%) show the "prism stdout stops while codex keeps
  working" pattern: 77 rescued by `_rollout_recovery` (reported `success`), 46
  returned `pre_terminal_idle`.
- **Association with tool output.** In 122/123 (99.2%) the onset of stdout
  silence coincides with a shell tool output completing (random baseline ≈11%).
  110/122 (90%) of those outputs hit codex's ~41 KB rollout truncation cap, vs
  9.5% (35/370) among outputs proven harmless — risk ratio 9.3×.
- **Contradicts the watchdog doc's premise.**
  [pre-terminal-idle-watchdog.md](pre-terminal-idle-watchdog.md) assumes codex
  had already finished when stdout went quiet. `task_complete` in fact lands
  94–1184 s after the onset (median 650 s), with 0/76 inside ±30 s.
- **Its 2026-05-16 incident is a different bug.** That run was codex 0.116.0
  with a maximum tool output of 10,897 B, untruncated. The upstream-flush
  attribution holds *for that incident* but does not generalise to later
  `pre_terminal_idle` cases.
- **Its "35% missing `tool_result`" figure is not re-measurable.** 982/1210 of
  that dataset were `mcp__codex__codex` — a separate MCP server that never runs
  prism's `_read_stdout` — and those transcripts are gone. In this window the
  rate is 1/842 = 0.12%.
- **`_rollout_recovery` vs this bug.** That it has *de facto* patched this bug
  since codex 0.144.1: confidence **high** (77/77 recoveries follow a 1200 s
  stdout-silence kill). That it was *designed* for it: confidence **low** — it
  predates the trigger conditions.

**Limits — do not compress away.** Rollout cap-hit is a proxy, **not a stdout
line-length measurement**; there is exactly one direct calibration (41,259 B
rollout vs 225,796 B stdout, 5.47×). The derived `est_raw` estimator has false
positives and **cannot adjudicate individual calls** (110/383 healthy turns
flagged, 17 provably harmless). Historical stdout line lengths are
unrecoverable: raw `--json` stdout was never retained on either side.

No code change follows from any of this. Revisiting the 1200 s threshold or
`_rollout_recovery` needs its own plan.

## Consequences and limits

- The truncated-preamble path (outcome 2) is closed at the source: the reader
  no longer stops, so the terminal event and the final message still land.
  `empty_final` remains the backstop for the empty case. It was reaching
  callers pre-fix: `success` envelopes from turns carrying an oversized tool
  output lacked codex's final rollout `last_agent_message` in 21/110 (19.1%)
  vs 3/502 (0.6%) otherwise. All 21 carry `truncated:false`, so `cap_envelope`
  is excluded as a confounder; 13 are unambiguous (envelope text shorter than
  the final message, worst 851 vs 11,628 chars), the other 8 rest on a
  substring test that a formatting difference could also fail.
- **Not fixed here:** `classify` never checks `parse_result.saw_terminal` on the
  `returncode == 0` path (`classify.py:318` falls through to `return None`), and
  `empty_final` (`server.py:279`) keys only on an empty `agent_messages`. That
  pair is what let outcome 2 out as `success`. This fix removes the cause, not
  the gap — any *other* source of stream loss would still be reported as a clean
  success. Deliberately out of scope: the change surface for this bug is
  `runner.py`, and a `saw_terminal` gate needs its own plan.
- **Not fixed here:** a single line at the new limit still interacts badly with
  `STDOUT_FRAME_CAP`. A ~4 MiB accepted event (or a ~4 MiB leftover fragment in
  `noise_log`) can push the worker→dispatcher frame past 4 MiB, which the
  dispatcher rejects wholesale. Pre-existing (a large accepted event already
  did this at 64 KiB scale, just less reachably) and out of scope; it needs a
  size policy in the worker, not in the reader.
- **Rejected in review (round 3):** adding backoff plus a "reader stalled" state
  that would force a non-terminal partial result to fail closed. `readline()`
  raises only from the overrun path, which always consumes, so a permanently
  raising real `StreamReader` does not exist; the loop is bounded anyway by the
  process lifetime plus `codex_exit_drain`, and the failure state it guards is
  the generic incomplete-stream case that `classify` already owns. Adding it
  here would mean new envelope semantics in the reader.
- `noise_log` may now contain one unparsable tail fragment per oversized line
  in the separator-not-found case. Harmless to `classify` — it is neither
  JSON-shaped nor whitelisted — but it is not a codex-authored line.

## Verification

- `pytest -m ""` — 429 passed, 2 skipped (baseline before this change: 422/2).
- New tests in `tests/test_runner_pure.py`, spawning a real python child
  because the failure lives in asyncio's own `readline()` contract and a fake
  stdout cannot reproduce it:
  `test_default_spawn_raises_stream_limit_above_asyncio_default`,
  `test_line_over_asyncio_default_limit_is_parsed` (200 KB line parses, stream
  intact), `test_line_past_stream_limit_degrades_without_stopping_the_stream`
  (limit forced to 8 KiB: line dropped with a `noise_log` trace, terminal event
  still captured), `test_preamble_before_oversized_line_is_not_returned_as_complete`
  (outcome 2, at asyncio's real 64 KiB default),
  `test_each_oversized_line_leaves_its_own_trace` (back-to-back oversized
  lines each register, so activity is not under-reported),
  `test_persistently_raising_reader_stops_claiming_activity` (trace cap),
  `test_oversized_stderr_line_does_not_stop_stderr_stream`.
- Mutation check: removing the `except ValueError` makes the degrade tests fail
  (they hang the child on a full pipe and hit the 20s bound); removing
  `limit=_STREAM_LIMIT` fails the two limit tests.
- Repro script behaviour reversed: 70 K / 200 K lines went from
  `saw_terminal=False, agent='PREAMBLE'` to `saw_terminal=True,
  agent='PREAMBLE\nAFTER-BIG'`.
- **End-to-end check, still open.** The unit tests prove the mechanism; only
  production counts prove there was no second cause. Pre-fix baseline over the
  audited window (2026-07-07..08-11, n=842 `mcp__prism__codex` calls, from CC
  transcript envelopes): `recovered_from_rollout=true` with
  `max_stdout_silence_s≈1200` was **77/842 (9.1%)**; that plus
  `pre_terminal_idle` was **123/842 (14.6%)**. Re-run both counts over a
  comparable post-fix window. If they do not fall sharply, something other than
  the line limit is still stopping stdout. Note the second count is the harder
  one to reproduce: `pre_terminal_idle` envelopes carry neither `SESSION_ID` nor
  `diagnostics`, so they can only be joined to a rollout by `cd` plus time
  window.
