# Codex transient retry storm masks the pre_terminal_idle watchdog

Date: 2026-08-11
Status: shipped
Codex CLI version: 0.144.1 (npm `@openai/codex`)

## Symptom

2026-08-10, project `d:\godot\shushu-idle`, CC session
`f6374c95-7331-44ab-937c-eb7a4d04d95b`: three consecutive `mcp__prism__codex`
review calls took 41m40s / 32m17s / 34m12s and delivered nothing — 1h49m of
wall clock for zero output. prism never intervened; each call ran to codex's
own give-up.

## What prism actually returned (record correction)

The initial report assumed prism had returned `success:true` with empty text.
It had not. The three envelopes (recovered from the ACF workflow subagent
transcript `…/subagents/workflows/wf_a2ff6283-12b/agent-abfef68afd891362e.jsonl`):

| finish (UTC) | success | agent_messages | error |
|---|---|---|---|
| 09:14:49 | `false` | 906 chars (partial reasoning) | `codex_error / classify / retryable=false` — `unexpected status 504 Gateway Timeout … url: https://cc.minecrahfut.fun:20001/v1/responses` |
| 09:47:19 | `false` | empty | same |
| 10:22:35 | `false` | empty | same |

prism failed closed all three times and the downstream agent correctly emitted
`status:"unavailable"`. The wasted hour came from the upstream proxy plus an
orchestrator that retried a `retryable:false` error twice more.

## Root cause

Codex rollout `~/.codex/sessions/2026/08/10/rollout-2026-08-10T16-33-09-019feacd-….jsonl`
records three `task_complete` payloads with `last_agent_message: null`, each
preceded by a gap with no semantic events: 1966s / 1936s / 2051s.

Those gaps are **not** stdout silence. prism's own instrument says so —
`diagnostics.max_stdout_silence_s` on the three envelopes:

```
368.5s   342.8s   445.3s
```

Codex `[model_providers.<id>]` defaults (verified against openai/codex, keys
present in the 0.144.1 binary): `stream_idle_timeout_ms = 300000` (5 min),
`stream_max_retries = 5`. A stalled upstream therefore produces 5 cycles of
"wait 300s → emit a `Reconnecting... N/5` warning on stdout → retry", i.e.
~390s of measured stdout silence per cycle and ~1950s total before codex
surfaces the final 504. The observed durations (1936–2051s) match.

Every reconnect line reached prism's runner, which bumped stdout activity for
*any* stdout line before parsing it. So the `pre_terminal_idle` watchdog
(1200s) was refreshed every ~350–450s and correctly never fired: it was
measuring bytes, and bytes were flowing.

## Hypotheses tested and rejected

Both items from the original brief were aimed at mechanisms that did not occur
here. Corpus = 1336 CC transcripts, 1388 real prism envelopes (codex 815,
gemini 573).

| Hypothesis | Verdict | Evidence |
|---|---|---|
| prism reads empty as success | rejected for this incident | 0 envelopes with `success:true` + empty `agent_messages`; codex 661/661 successes carry text |
| the idle watchdog is missing/broken | rejected | end-to-end repro (fake codex: one line then 40s silence, threshold 5s) killed at 5.5s with `pre_terminal_idle`, `max_stdout_silence_s=5.0`, tree reaped. In the corpus it fired 118×: 74 rollout-recovered to success, 44 hard errors |
| a 600s idle watchdog would have caught it | rejected | max measured silence was 445s; catching this by threshold alone needs <343s, which collides with codex's own 300s stream-idle cycle |

Related: gemini's `empty_final` gate fired 29× in the same corpus — that class
is real for gemini and has never been observed for codex.

## Fix

1. **Operator config (zero wrapper code, biggest lever).**
   `~/.codex/config.toml` → `[model_providers.cch-proxy] stream_max_retries = 1`.
   A doomed turn gives up in ~12min instead of ~33min, for every codex caller,
   and it gives up *below* prism's watchdog so the failure surfaces as the
   upstream's own error rather than a wrapper kill.

2. **`runner._read_stdout` — withhold the tick for recognized reconnect
   chatter.** Lines the parser siphons into `transient_lines`
   (`Reconnecting... N/M`, classified by `events.is_transient_codex_error`) no
   longer bump the stdout coalescer, so they cannot refresh
   `pre_terminal_idle`. `StreamParser.transient_count` exposes the counter the
   runner compares across `feed_line`. Arming is unchanged: `first_codex_output`
   still fires on the first line whatever it is. Noise lines still count as
   activity — the codex event whitelist drifts across versions (`turn.failed`
   is not in `CODEX_EVENT_TYPES` today and lands in `noise_log`), so "not
   admitted by the parser" is too fragile a signal for a kill decision. Only
   the explicitly-classified transient class is excluded, so what the threshold
   measures is *tick-eligible stdout* — raw bytes minus recognized reconnect
   chatter — and not progress in any stronger sense. (Field evidence from
   2026-08-11: codex can keep writing reasoning and tool calls to its rollout
   while stdout is wedged, so even zero tick-eligible stdout does not prove the
   session is idle. Tracked separately; not addressed here.)

3. **`server._invoke_codex` — `empty_final` fail closed, no retry.** An
   error-free outcome with empty/whitespace `agent_messages` now returns
   `code=empty_final, stage=classify, retryable=False` instead of an empty
   success. Never observed in 815 recorded codex calls, so this is insurance
   against upstream drift, not a fix. Two deliberate asymmetries with the
   gemini path ([gemini-empty-final-fail-closed.md](gemini-empty-final-fail-closed.md)):
   **no re-dispatch** (a codex turn costs minutes and an empty codex final has
   no observed transient cause) and **`retryable=False`** (the flag is a caller
   hint — advising a caller to repeat a multi-minute turn whose tool side
   effects already ran would contradict the reason for skipping the internal
   retry). Gemini keeps `retryable=True` because its empty final has a pinned
   transient cause and a demonstrated recovery-on-retry.

## Consequences and limits

- **The watchdog now preempts codex's own retry policy.** At the codex default
  `stream_max_retries = 5` with a ~390s cycle, the 4th reconnect lands near
  1170s and prism kills at 1200s: a turn that would have recovered on cycle 4
  or 5 is lost, and rollout recovery cannot rescue it (no terminal payload has
  been written yet). Intentional — 750 further seconds of waiting is not worth
  the tail probability of a late-cycle recovery — and `PRISM_PRE_TERMINAL_IDLE=3600`
  remains the escape hatch. On this machine fix 1 removes the conflict: codex
  gives up at ~700s, inside the wrapper's bound. Machines left at the codex
  default get the wrapper's policy rather than codex's.
- **`max_stdout_silence_s` changed meaning.** Same field name, but reconnect
  lines no longer refresh it, so post-change values are not comparable with the
  368–445s samples quoted above.
- **`RECONNECTING_RE` matches by prefix.** A future message such as
  `Reconnecting... 5/5: retry budget exhausted: 504` would still be siphoned,
  losing both its tick and its error classification. The regex is left as-is —
  every observed sample is a bare `Reconnecting... N/M`, and tightening it
  without evidence would risk misclassifying benign chatter as a real error.
  Fix 3 is a *partial* backstop only: a turn whose sole error got mis-siphoned
  ends as `empty_final` instead of an empty success, but a turn that emitted
  some agent text before the mis-siphoned fatal line still returns success with
  a truncated answer.

## Deferred: lowering `PRE_TERMINAL_IDLE_S` 1200 → 900

Rejected for now. With fix 2 the 1200s threshold already cuts a retry storm
from ~1950s to 1200s. The extra 300s of savings is paid for by risking the
recovery path: all 74 watchdog firings in the corpus were rollout-recovered
into successes, which only works if codex has already written `task_complete`
when the kill lands. Killing 300s earlier moves some of those into hard
failures, and the codex-side legitimate-silence distribution is unmeasurable
from envelopes (successful codex calls carry no diagnostics by design —
`server._envelope`'s narrow gate). Revisit with data, as a standalone
one-constant commit.

## Verification

- `pytest -m ""` — 422 passed, 2 skipped (platform-gated), integration markers
  included, i.e. a real `codex exec` round-trip under the new config.
- New tests: `test_transient_reconnect_lines_do_not_emit_stdout_tick`
  (250 reconnect lines → 0 `stdout_tick` on the burst branch, watchdog still
  armed), `test_time_spaced_lines_tick_only_when_tick_eligible` (the incident's
  shape — one line per idle cycle, interval branch only: 0 ticks for
  reconnects, ≥1 for real events), `test_transient_count_tracks_only_siphoned_lines`,
  `test_codex_empty_success_fails_closed_without_retry` (single dispatch),
  `test_codex_whitespace_only_final_fails_closed`,
  `test_codex_existing_error_not_masked_by_empty_final`,
  `test_codex_nonempty_success_default_envelope_unchanged`.
- `codex doctor` → `config.toml parse ok` after the config edit.
