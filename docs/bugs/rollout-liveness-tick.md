# Rollout-liveness tick for the pre-terminal idle watchdog

Date: 2026-08-19
Status: shipped
Codex CLI version: 0.144.1 (empty-summary stdout filtering and rollout
persistence policy verified there).

## Symptom

Six deterministic `pre_terminal_idle` false kills in two days, in two
incidents on the same task class: 2026-08-17 21:15/21:36/21:57 UTC and
2026-08-19 03:41/04:02/04:23 UTC, spans 1184–1206s. Every killed turn was a
legitimate pure-reasoning turn (review prompt with inline diff, zero tool
calls) still appending reasoning to its rollout ~every 10s until ~20s before
the kill. `task_complete` was never written, so rollout recovery had nothing
to recover; and because the watchdog error path drops SESSION_ID (followups
item 3), each retry restarted the whole turn from zero — 20+ minutes lost per
kill, three times in a row per incident.

## Root cause chain

1. The cch-proxy backend returns reasoning items with an empty `summary` in
   100% of sampled rollouts (123/123 and 108/108 sampled 2026-08-19, 58/58 in
   a July file) despite `summary=auto`.
2. `codex exec --json` emits reasoning to stdout only as `item.completed`
   with a NON-empty summary — `codex-rs/exec/src/event_processor_with_jsonl_output.rs`
   filters empty ones (DeepWiki-verified at codex-cli 0.144.1).
3. `~/.codex/config.toml` `model_reasoning_effort = "max"` pushes
   pure-reasoning turns past 20 minutes.

Product: zero tick-eligible stdout for the turn's entire duration ⇒ the
watchdog kills at exactly 1200s while codex is mid-reasoning.

The only tick source under this proxy is interim assistant messages: turns
where the model volunteers mid-turn prose survive (observed gaps 787/430/356s);
single-block thinkers die at exactly 1200s. Whether a call survives is a coin
flip on the model's output style. The legitimate silence distribution rides
the threshold line: a 1495s success carried a 1195s stdout gap (5s margin), a
1850s success ~1189–1209s. Full-corpus check (all `~/.codex/sessions`
rollouts: 5077 turns, 810 pure-reasoning exec turns) found no third pattern.

## Why not just raise the threshold

- `PRISM_PRE_TERMINAL_IDLE=3600` is already deployed as a stopgap on the
  affected project, but it blinds B-class detection: with a 60-minute window
  a true wedge gets user-cancelled long before the watchdog fires, and
  rollout recovery runs ONLY on `pre_terminal_idle` kills — no kill, no
  fingerprint.
- Think time is uncorrelated with prompt bytes (May data), so no prompt-side
  workaround exists.
- Any fixed stdout-only threshold overlaps the legitimate distribution; the
  false-kill/late-kill tradeoff cannot be tuned away on that single axis.

## Decision — rollout-liveness tick (scheme A)

The rollout file is a second liveness surface owned by codex itself. While
the watchdog is armed, poll it stat-only via a single-flight worker-thread
probe (`_probe_rollout`), launched at `min(30s, threshold/8)` cadence and
NEVER awaited inline — its result is applied on a later loop iteration:

- **Locate**: the probe reuses recovery's `find_rollout_path` UUID glob (all
  date dirs — a resumed thread's file lives under its ORIGINAL date) until
  the path resolves. Codex materializes the rollout on its first flush,
  which can land after `first_codex_output` (deferred creation;
  DeepWiki-verified), so arming cannot assume the file exists.
- **Stat**: `os.stat` st_size growth since the PREVIOUS observation refreshes
  the same idle deadline as `stdout_tick`. The first successful stat only
  sets the baseline (`rollout_baseline_ts`), so a resumed session's
  pre-existing content never reads as liveness. No read, no held handle while
  the turn runs.
- **Kill condition** becomes: no stdout tick AND no observed rollout growth,
  both for `threshold` seconds.

The single-flight, never-awaited probe is load-bearing: the kill decision
never waits on the filesystem (untracked kill timing stays exactly as
before), a hung filesystem cannot block the event loop, and threads cannot
stack — a permanently hung probe leaks exactly one pool slot per call (the
same exposure recovery's `to_thread` already has) while liveness quietly
degrades to stdout-only.

Two refinements close the observation gaps a naive poll would leave:

- **Final beat**: when the deadline is reached on a TRACKED file, the loop
  grants exactly one `poll_floor` beat (forcing a probe if none is in
  flight) before killing, so growth landing inside the last cadence window
  is still observed — e.g. a `task_complete` appended seconds before expiry.
  This narrows the deadline blind window from one cadence (~30s) to probe
  latency, still without ever awaiting the filesystem; a hung probe just
  forfeits the beat. Untracked kills skip the beat entirely.
- **Observation staleness**: the kill records whether probes had gone dark —
  a probe still unresolved after the final beat (a hung probe leaves misses
  at 0 and can be younger than the age bound), ≥2 consecutive evidence-less
  probes (scale-free; catches real stat/glob failures at any threshold), or
  no successful stat within `2×(cadence+poll_floor)`. A dark channel
  forfeits any stall claim — codex may have kept appending unobserved — and
  flags `rollout_observation_stale` so B-trigger audits skip such kills.
- **Late thread_id**: `thread_id` is a separate stderr write that can trail
  `first_codex_output` by a chunk gap; while it is unknown the loop sleeps
  at the poll floor instead of the full deadline, so a late arrival still
  engages liveness instead of stranding the call stdout-only.

### Why retry storms stay killable

`BackgroundEvent` notices ("stream error: …; retrying") are NOT persisted to
the rollout (codex-rs rollout persistence policy, DeepWiki-verified) — a
reconnect storm grows nothing, so the storm guard from
[codex-transient-retry-storm.md](codex-transient-retry-storm.md) holds
through the new tick.

### Degradation is harmless by construction

| Failure | Behavior |
|---|---|
| thread_id never captured | never probes — kill at threshold, as before |
| rollout file never found (deferred creation, `--ephemeral`) | probe re-globs at poll cadence; until resolved, stdout-only — as before |
| `os.stat` OSError / probe exception | probe returns no evidence, no liveness credit |
| filesystem hung or slow (UNC `CODEX_HOME`, dying disk) | probe never awaited — loop timing unaffected; one in-flight probe max, no new probes launched while stuck; stdout-only |
| external truncation (size shrinks) | baseline rebased, no liveness credit |
| future codex buffers rollout writes past threshold | no growth observed — as before |

Every failure mode leaves `last_rollout_growth_ts` unset/stale, so the
watchdog can only get LESS aggressive than stdout-only, never more — and
never slower to kill than the threshold, since the kill check never waits on
a probe.

### Attribution granularity

Growth is credited at observation time (30s cadence at the production
threshold), so a kill after a true stall lands at most
`threshold + 30s + scheduling` after the stall began. Immaterial at 20 min.

## A doubles as B's detector

Both result paths now carry rollout-liveness observability:

- diagnostics gain `rollout_ticks` + `rollout_watched_s` +
  `last_rollout_growth_age_s`, present only when the file was actually
  tracked (key absence = liveness unavailable). Public via the server
  whitelist wherever diagnostics surface.
- a TRACKED kill's `raw` notes the rollout verdict. Three tiers, chosen for
  attribution honesty: probes gone dark → "rollout observation lost (…s
  since last successful stat — no stall verdict)"; observed growth →
  "rollout file stalled too (…s since last observed growth, N growth
  ticks)" — sound, because the growth clock IS the liveness deadline, so
  the kill itself proves ≥threshold of rollout silence under a healthy
  observation channel. Never grew → "no rollout growth observed in the …s
  watched" — a watch-window fact, NOT a stall claim, because a late
  baseline (file resolved shortly before expiry) cannot prove the rollout
  was silent for the threshold. Untracked kills keep the pre-liveness raw
  byte-identical.

A kill with observed growth and a healthy observation channel whose
recovery then finds `task_complete` (`recovered_from_rollout=true` +
`rollout_ticks>0`, no `rollout_observation_stale`) means: codex wrote the
answer, stdout stayed dead, and the rollout then went quiet for the full
threshold — a true B-class wedge fingerprint. That envelope is scheme B's
trigger signal (B = live tail-parse with early kill on rollout
`task_complete` + stdout grace; stays gated behind its trigger). A
never-grew or observation-stale recovery is inconclusive — check
`rollout_watched_s` / `rollout_observation_stale` before counting it.

Note: the kill path's diagnostics still don't surface on the wire (the
server drops diagnostics on outcome-less errors — followups item 3, separate
commit). The kill `raw` note is the transcript-visible signal for now;
recovered-success envelopes surface the full set already.

## Expected outcomes (all tested)

1. rollout keeps growing + stdout silent ⇒ never killed (core fix)
2. rollout stalls — including after writing `task_complete` ⇒ kill at
   threshold past last observed growth; recovery unchanged (the
   `task_complete` variant recovers AND fingerprints B)
3. no thread_id / no rollout file ⇒ identical to before (raw and
   diagnostics byte-identical; stall note appears only when tracked)
4. gemini ⇒ fully bypassed, as before

## Implementation

| File | Change |
|---|---|
| `src/prism_router/dispatcher.py` | `ROLLOUT_POLL_CAP_S`; `_WorkerState` gains `rollout_path` / `last_rollout_size` / `rollout_baseline_ts` / `last_rollout_growth_ts` / `rollout_ticks` / `last_probe_ok_ts` / `probe_misses_since_ok` / `rollout_observation_stale`; `_arm_pre_terminal_idle` runs the single-flight probe, wakes at the poll floor while thread_id is pending, grants the final beat, records staleness, and kills only when both surfaces are silent; new `_probe_rollout` (thread-side), `_apply_rollout_probe`, `_rollout_liveness_diag`; tracked kills append the tiered liveness note to `raw`; kill/success diagnostics gain the liveness keys |
| `src/prism_router/errors.py` | `Diagnostics` gains `NotRequired` `rollout_ticks`, `rollout_watched_s`, `last_rollout_growth_age_s`, `rollout_observation_stale` |
| `src/prism_router/server.py` | `_PUBLIC_DIAG_KEYS` + the four keys |
| `tests/test_dispatcher.py` | 6 scenario tests (incl. deferred file creation, late single append, dark observation channel) + probe/apply unit test; byte-identical raw pinned to full equality on the untracked-kill tests |
| `tests/test_server.py` | whitelist passthrough + force-gate regression extended with the liveness keys |

Unchanged by design: codex-only gating, `pre_terminal_idle=0` disable,
recovery armed only on `pre_terminal_idle`, threshold default, runner,
classify.

## Limitations

- Stat-only cannot see WHAT grew: any rollout append counts as liveness.
  Accepted — an appending writer task is a live codex, and terminal-aware
  early decisions are exactly scheme B, which stays gated.
- The rollout writer is an async buffered channel (DeepWiki); empirically it
  flushes every ~10s during pure reasoning on 0.144.1. If a future codex
  buffers longer than the threshold, the tick silently degrades to
  stdout-only behavior — the safe direction.
- A pathological wedge that keeps appending forever would now run to
  `overall_timeout` (2h) instead of 1200s. No such pattern exists in the
  5077-turn corpus (wedges stop writing after `task_complete`); accepted.
- Cancelling the in-flight probe task abandons its RESULT, not the thread;
  a truly hung filesystem strands one pool slot per call — the same accepted
  exposure as recovery's bounded `to_thread`.
- Growth is credited when the probe result is APPLIED (next loop iteration),
  not when the stat ran — attribution can lag by up to one cadence, which
  only delays kills further, never hastens them.
- Residual deadline blind window ≈ final-beat probe latency (~ms healthy):
  growth landing between the final probe's stat and the kill is missed. The
  kill is still never earlier than pre-liveness behavior.
- The kill is evaluated only on the pre-liveness loop's own check schedule
  (`now + max(poll_floor, threshold − elapsed)` recurrence); probe and
  late-thread_id wakes in between never evaluate it. This makes the kill
  instant bit-identical to the old loop whenever liveness contributes
  nothing (no thread_id / no file / no growth — a finer check schedule
  would otherwise kill EARLIER than the old loop's coarse-wake overshoot on
  some inputs), and provably never earlier when it does (liveness `elapsed`
  is ≤ the stdout-only `elapsed`, so every reschedule lands at or past the
  old loop's next wake).
- A probe that stats just before an EXTERNAL truncation can credit one tick
  from the pre-truncation growth (which did happen); the next probe rebases.
  Bounded at one threshold of extra wait; codex's own writer only appends.
- A merely SLOW (not hung) filesystem whose probe outlives the final beat
  yields "observation lost" instead of a stall verdict — conservative
  direction: it withholds a B claim, never fabricates one.

## Follow-up

- After a one-week soak with zero false kills, revert
  `PRISM_PRE_TERMINAL_IDLE=3600` in the shushu `.mcp.json`
  (`D:\godot\shushu-idle\.mcp.json`) so B-class fingerprint monitoring
  regains sensitivity — with the knob at 3600 a true wedge is likely
  user-cancelled before any fingerprint is written.
- Scheme B stays gated behind: (a) a post-liveness recovery-stamped kill, or
  (b) an offline audit finding user-cancel × rollout-`task_complete` long
  before the cancel.
- Followups item 3 (SESSION_ID + diagnostics passthrough on the watchdog
  error path) is a separate commit; it will make the kill-path liveness
  diagnostics wire-visible.

## Citations

- Empty-summary stdout filter: `codex-rs/exec/src/event_processor_with_jsonl_output.rs`
  (DeepWiki, codex-cli 0.144.1)
- Rollout writer (async buffered task, deferred file creation):
  `codex-rs/rollout/src/recorder.rs` (DeepWiki)
- Persistence policy (BackgroundEvent not persisted):
  `codex-rs/rollout/src/policy.rs` (DeepWiki)
- Forensic corpus 2026-08-19: all `~/.codex/sessions` rollouts — 5077 turns,
  810 pure-reasoning exec turns, 6 kills in 2 incidents; empty-summary
  sampling 123/123, 108/108, 58/58

## Related

- [pre-terminal-idle-watchdog.md](pre-terminal-idle-watchdog.md) — the
  watchdog and the original 1200s calibration
- [rollout-recovery.md](rollout-recovery.md) — rollout file semantics, UUID
  glob, post-kill recovery this change leaves untouched
- [codex-transient-retry-storm.md](codex-transient-retry-storm.md) — the
  storm guard the new tick must not (and does not) weaken
