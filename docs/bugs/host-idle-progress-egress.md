# Host idle-timeout kills live long calls: forward liveness as MCP progress

**Status: IMPLEMENTED — landed with this doc. The F8 live gate PASSED before
merge (see Acceptance), so it is not deferred to soak.
Design lineage: codex R1 (F1-F8) → gemini independent (M1-M3) → codex R2
(G1-G6) → R3/R4/R5 (H1: cancellation-response ordering → spawner
contract → wait_for task escape) → R6 APPROVE. Default-on adjudicated
ACCEPT by both reviewers. Implementation lineage: I1 (gemini) → I2 (codex, r1-r12).**

## Symptom

Claude Code (host) aborts a prism tool call after 1800 s of MCP-level
silence, while codex is still working:

> MCP server "prism" tool "codex" sent no response or progress for 1800s;
> aborting. ... set a per-server "timeout" (ms) ... or
> CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT (ms) globally (0 disables).

Incident 2026-08-20 (CC session 98947157, ACF repo): a 39.3-min review call
(thread 01a01f1a-22cc-…) was aborted at exactly 1800 s. Codex was never idle
(max stdout-eligible gap ≈180 s; 51 tool calls; rollout write gaps ≤39 s),
was NOT killed by the abort — the host sends no effective cancellation, the
child ran to task_complete 9.5 min later — and prism would have returned
success at 2360 s, well inside overall_timeout 7200. The finished report was
recoverable only by manual rollout-tail forensics; the caller never received
a SESSION_ID and blind-retried the whole turn.

Not a pre_terminal_idle regression: prism's own watchdog (1200 s, stdout +
rollout liveness, docs/bugs/rollout-liveness-tick.md) never came close to
firing. The kill lives one layer ABOVE prism.

## Root cause

server.py is deliberately silent mid-call ("No daemon, no shim, no progress
notifications"). The host cannot see codex stdout or the rollout file; its
only liveness channel is MCP traffic. A silent server degrades the host's
IDLE timeout into a TOTAL-duration cap: every call > 30 min dies regardless
of health. effort=max review workloads made >30-min turns routine.

## Why progress notifications (not a bigger host timeout)

MCP spec 2025-06-18, lifecycle → Timeouts:

> Implementations MAY choose to reset the timeout clock when receiving a
> progress notification corresponding to the request, as this implies that
> work is actually happening. However, implementations SHOULD always
> enforce a maximum timeout ...

Verified on this machine (2026-08-20 probe, CC 2.x, fastmcp 3.2.4):

- CC attaches `progressToken` to tools/call (`Meta(progressToken=4, ...)`).
- A/B probe under `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=30000`: a 90 s tool
  ticking every 10 s SURVIVED; the identical silent tool died at exactly
  30 s with the incident's message. Progress resets the idle clock.
- Zero context cost: 9 notifications with `message` set → 0 entries in the
  session transcript. Transport-level, never model-visible.

Raising the per-repo `"timeout"` knob instead would waive the host's outer
net for a hung PRISM (not just a hung codex), require per-repo config
sprawl, and communicate nothing. The spec's intended split: progress =
"work is happening"; the (unchanged) 1800 s cap = maximum for a server that
stops producing evidence.

## Design: evidence-gated progress egress (r7)

Forward the SAME liveness evidence the pre_terminal_idle watchdog already
consumes — never a bare timer. No evidence → no notifications → the host
cap governs, which is its spec role.

Evidence sources (both already exist; main process only, no worker/runner
change — runner.py:227-275 already emits the milestones, dispatcher
:954-1059 already consumes them):

- `stdout_tick` milestones (`_drain_stderr`) — backend-agnostic; the
  stdout_tick branch runs even when the idle watchdog is disabled (gemini,
  `PRISM_PRE_TERMINAL_IDLE=0`), so those paths still forward stdout-tier
  evidence. Runner-coalesced (≥2 s or ≥200 lines); reconnect-storm chatter
  already withheld by the runner. `first_codex_output` stays watchdog
  ARMING evidence but is NOT forwarded: the runner emits it before
  transient classification (runner.py:264-275), so a first
  `Reconnecting...` line would otherwise reset the host timer on
  non-evidence (G3).
- Observed rollout st_size growth (`_apply_rollout_probe`) — codex only;
  present iff the idle watchdog is armed.

### Components

1. `_liveness.py`: `LivenessEgress` — **one instance per MCP tool call
   (request-scoped), owned by server.py and shared across every Dispatcher
   the request creates, including gemini's internal transient-retry
   re-dispatch** (F2). Owns ALL egress mechanics:
   - Monotonic counter: incremented per send ATTEMPT; `progress` strictly
     increases across the whole request even through retries; wire gaps
     from failed sends are spec-legal.
   - Throttle on `last_attempt`, bumped BEFORE task creation; a failing
     sink consumes the interval — an immediately-raising sink cannot
     machine-gun beyond one attempt per 60 s (F4).
   - Single-flight: one `_Flight` record; new evidence while pending is
     dropped. `_Flight` owns a completion event and cancellation state
     rather than relying on a task handle returned by the spawner (F4,
     reworked in R4 — anyio's `start_soon` returns None).
   - Spawn mechanism (H1): send tasks are NOT bare `asyncio.create_task` —
     the egress creates a `_Flight`, then invokes an injected spawn
     callable backed by `TaskGroup.start_soon(runner, flight)` in the
     request-scoped anyio task group. `start_soon()` returns None and MUST
     NOT be treated as a task handle. The runner catches and logs ALL
     non-cancellation sink exceptions so none escape and cancel the
     enclosing task group (an uncaught child exception would take the
     whole tool handler down); its `finally` signals the completion event
     and clears the slot only-if-current. `abort()` marks cancellation in
     `_Flight`: a runner that has not started yet observes the flag and
     exits; an already-running one is cancelled via its child-local anyio
     CancelScope (held by `_Flight`, cancellable synchronously
     cross-task). If `start_soon` rejects because the group is closing,
     the slot is completed and cleared while the attempt still consumes
     its counter and throttle interval. The request-scoped task group is
     what lets the SDK's request-scope cancellation reach any in-flight
     send (H1 ordering).
   - Bounded send (F4, reworked in R5): the runner directly awaits the
     sink inside `anyio.fail_after(SEND_TIMEOUT_S)`. `asyncio.wait_for`
     MUST NOT be used here: it wraps the sink coroutine in a separate
     asyncio task (tasks.py:426-460) OUTSIDE the request-scoped task
     group, reopening H1's cancellation-response race. The timeout scope
     therefore cancels the runner task itself, preserving request-scope
     ordering.
   - Attempt deadline (G4): `egress_max_s = max(0, OVERALL_TIMEOUT_S −
     1800 − SEND_TIMEOUT_S)`, derived in server.py from the imported
     dispatcher constant — single-sourced (M2). Subtracting the 30 s send
     timeout places the LATEST successful delivery no later than
     `OVERALL_TIMEOUT_S − 1800` (a send started just before a bare cutoff
     could otherwise land 30 s past it). After the deadline `note()` is a
     permanent no-op: the host's idle clock runs again and reaps any
     occupation by ~overall_timeout (F5/F6). Scope: this bound assumes the
     host's DEFAULT 1800 s idle cap; a host configured longer/disabled
     leaves prism's own overall watchdog as the authoritative bound.
   - Drain/abort split (F3 + M3): on the NORMAL and error-envelope return
     paths the tool awaits `aclose(timeout=2 s)` INSIDE the try, before
     the envelope leaves: sets `closed` first (F4), awaits the in-flight
     send's settlement, cancels+awaits on timeout — a true barrier, no
     notification can race or trail the response. The `finally` performs
     only a SYNCHRONOUS, idempotent `abort()` (set closed + cancel
     in-flight, zero awaits): during an MCP-cancel unwind an awaited drain
     would be re-cancelled at its first suspension point and bypassed
     anyway; abort keeps "Cancel re-raises CancelledError" (dispatcher
     invariant 5) undelayed. CAUTION (H1): the SDK sends a cancellation
     ERROR RESPONSE independently of handler cleanup —
     `RequestResponder.cancel()` cancels the handler scope and immediately
     responds (mcp/shared/session.py:138-150; confirmed at
     mcp/server/lowlevel/server.py:773-781) — so handler-local `abort()`
     alone cannot order an in-flight notification before that response.
     Ordering is established STRUCTURALLY instead: send tasks are spawned
     into the request's cancellation scope (see Components #1/#3), so
     scope-cancel either lets a frame that already reached the write
     stream precede the response, or cancels the send before it writes;
     the mandatory protocol-cancellation gate below proves it.
2. dispatcher: new optional `on_liveness: Callable[[str], None] | None`
   (constructor param, mirrors the timeout params). Called at the TWO
   evidence sites (`stdout_tick`, rollout growth in
   `_apply_rollout_probe`), always through a non-raising containment
   helper: a callback exception is logged and swallowed AT THE CALL SITE,
   so it can never escape and kill `_drain_stderr` or
   `_arm_pre_terminal_idle` (G1 — the egress promises not to raise, but
   the dispatcher does not trust that promise). Dispatcher has NO egress
   lifecycle duties and imports nothing from fastmcp. `None` → the call
   sites are skipped entirely.
3. server.py: captures the Context INSIDE each decorated tool body via
   `fastmcp.server.dependencies.get_context()` (dependencies.py:324) —
   there the tool's ContextVar is guaranteed active, the `_invoke_*`
   helpers stay independently testable, and tool signatures stay
   untouched; today's live probe proved a tool with NO ctx parameter
   still receives the token and sends progress this way, refuting the
   "must add ctx to the signature" claim (M1, G6). Sink protocol:
   `Callable[[float, str], Awaitable[None]]` — the egress owns the
   counter and passes both progress value and message; the sink awaits
   `ctx.report_progress(progress=n, total=None, message=msg)` (an
   `async def`, context.py:385 — verified). The tool body opens a
   request-scoped anyio task group around the invoke and hands its
   `start_soon` to the egress as the spawn callable (H1). Lifecycle
   branches are explicit (G2): normal return AND non-cancellation
   exceptions await a NON-RAISING `aclose` before FastMCP can emit any
   response (success or error); `CancelledError` calls `abort()` and
   re-raises; `finally` calls idempotent, non-raising `abort()` as a
   backstop. `abort()`
   requests cancellation but does not claim synchronous settlement, and
   never masks the active exception. Module docstring line 4 updated —
   the "no progress notifications" stance is superseded by this doc
   (no-daemon / no-shim untouched).
4. config.py: `liveness_progress: bool = True` — dataclass field WITH
   default so existing direct `ServerConfig(...)` constructions keep
   working (F7); env `PRISM_LIVENESS_PROGRESS` via `_bool_env`. Off → no
   egress constructed, `on_liveness=None`, zero per-milestone calls.

## Invariants (r7)

1. **Observer only, no logical coupling.** Kill decisions read no egress
   state; no awaits are added to `_drain_stderr` or the watchdog loop;
   `note()` is sync O(1) (throttle check + injected spawn). The wall-clock
   claim is a no-coupling contract plus a jitter regression (a sink doing
   synchronous pre-await work must not shift a short-threshold kill beyond
   the existing poll-floor tolerance), NOT bit-identity (F1).
2. **Evidence-gated.** Every notification is caused by a stdout tick or an
   observed rollout growth. A wedged codex stops producing both; prism
   kills at 1200 s and answers long before the host's 1800 s.
3. **Degradation is bounded (not "never worse") (F6).** A hung sink parks
   the single-flight slot → forwarding stops → exactly today's behavior. A
   pathological indefinitely-appending non-terminal wedge (never observed;
   class includes shared-rollout misattribution from concurrent
   same-SESSION_ID calls — parked item 4 of the watchdog follow-ups) can
   extend request occupation beyond today's 1800 s host kill, but
   `egress_max_s` caps it at ≤ ~overall_timeout (7200 s). This trade is
   deliberate and documented.
4. **Protocol conformance is barrier-enforced.** Strictly-increasing
   progress per request (egress counter, request-scoped; wire gaps from
   failed attempts are spec-legal — "MUST increase" constrains order, not
   density); stop-before-response via awaited `aclose` on every
   response-producing path (F3/G2); ≥60 s min interval (rate limiting);
   ordinary token-less FOREGROUND clients no-op inside fastmcp
   (context.py:399-408) — fastmcp's Docket task-execution branch is out
   of scope, prism does not use fastmcp task execution (G6).
5. **Layering.** dispatcher: plain sync callable in, nothing out.

## Non-goals

- MCP Tasks extension (SEP-2663) — needs host negotiation + durable task
  store; re-evaluate when CC negotiates it.
- Unconditional heartbeats; any change to kill semantics or cancellation.
- Serializing concurrent same-SESSION_ID calls (parked item 4 — blast
  radius now bounded by EGRESS_MAX_S, see invariant 3).
- Automating host-abort recovery (rollout-tail forensics stays manual).

## Residual risk

- ~~CC's timer reset is proven only for a FOREGROUND call (< 120 s)~~ —
  CLOSED by the live gate: a 838 s call survived a 90 s idle cap with
  progress landing on both sides of the 120 s auto-background transition.
- A sink that resists cancellation delays the RESPONSE by however long it
  resists. `DRAIN_TIMEOUT_S` is only the grace before `aclose` cancels; the
  wait AFTER the cancel is unbounded by design (codex I2 BLOCKER-2): the
  request-scoped task group joins the same child regardless, so a deadline
  there bought no end-to-end bound and only made the postcondition a lie.
  The real bound is `SEND_TIMEOUT_S` for any normally-cancellable sink, and
  unbounded only for one that shields itself (`ctx.report_progress` does
  not). Pinned by test, not argued. The join is inherent to the H1
  mechanism: abandoning a child is what a task group forbids.
- A sink raising `CancelledError` with none actually pending is treated as
  a broken sink and swallowed, discriminated by `Task.cancelling()` (anyio
  calls `task.cancel()` before delivering scope cancellation, so a real one
  always has a pending request there). Left unguarded it cancelled the
  tool's whole task group, whose `__aexit__` then SWALLOWED that
  cancellation and let the handler fall through to an unbound result
  (codex I2 BLOCKER-1, reproduced). Both halves are fixed — the escape and
  the fall-through — because the fall-through is reachable from any future
  child that dies by cancellation, not just from the sink.
- That fall-through backstop raises `RuntimeError`, NOT `CancelledError`
  (codex I2 r2 BLOCKER, reproduced). The SDK reads a handler cancellation
  the client never asked for as transport shutdown and re-raises it into
  the server task group (`mcp/server/lowlevel/server.py`:773-782), killing
  the whole session; the mutation test gets `anyio.EndOfStream` and a dead
  wire. The corrected form fails one request and answers the next `ping`.
  (The earlier "trips `Request already responded to`" rationale was wrong:
  `_completed` is written only by `respond()` and `cancel()`.)
- `MIN_INTERVAL_S` is a fixed 60 s floor, NOT derived from the host's cap
  the way `egress_max_s` is. A host configured with an idle cap below
  ~75 s would out-run the attempt cadence and kill healthy calls anyway.
  Deliberate: the floor is MCP rate-limiting hygiene, and the feature is
  scoped to the default 1800 s cap.
- ~~fastmcp/mcp-SDK session-writer serialization~~ — VERIFIED in codex R2
  against the vendored SDK (fastmcp 3.2.4 / mcp 1.27.0): notifications and
  responses share the same `_write_stream.send`
  (mcp/shared/session.py:316-349) and stdio has a single sequential
  JSON-line writer (mcp/server/stdio.py:75-81). Frame interleaving is
  structurally impossible; ordering is what the aclose barrier governs.
- Implementation plumbing note: `_drain_stderr` and `_apply_rollout_probe`
  are module-level functions — `on_liveness` must be threaded to them as an
  explicit parameter from `Dispatcher._run_worker`; it is not ambiently
  available (gemini pass, unverified-list item, confirmed by inspection).

## Default-on stance (SETTLED — both reviewers ACCEPT)

Codex R1 asked for the live backgrounded-path gate BEFORE default-on.
Counter-position: default ON at merge, backgrounded-path proof as the
FIRST soak checkpoint — a default-off knob historically never gets flipped
(the 08-19 `PRISM_PRE_TERMINAL_IDLE=3600` knob blinded B-class detection
and was withdrawn for exactly this reason; operator precedent is early
exposure). Bounded worst cases (invariant 3) make this affordable: if the
CC-side assumption fails, the feature is ineffective (today's behavior),
not an unbounded server failure. Adjudicated ACCEPT by gemini (R1') and
codex (R2); stance final, conditional on the deterministic unit /
dispatcher / protocol gates passing.

## Acceptance (r7) — all gates PASSED

Each bullet names the test that proves it. Where a test could plausibly
pass on broken code, a mutation was run to show it does not (M1-M4 below).

- Unit (`tests/test_liveness.py`): throttle-on-attempt incl. raising-sink
  storm at 2 s cadence over 118 simulated seconds → exactly 2 attempts;
  single-flight (proven with the throttle disabled, so only single-flight
  can drop); monotonic counter across simulated re-dispatch; `aclose`
  waits for an in-flight send / cancels one that overruns; note-after-close;
  double-close; abort-after-aclose and aclose-after-abort settlement; sync
  abort during a cancel unwind (asserts the sink is STILL RUNNING when
  `abort` returns — cancellation requested, not settled — and that
  `sys.exc_info()` is untouched); a raising sink cannot cancel the
  enclosing task group (a sibling child survives and
  `tg.cancel_scope.cancel_called` stays False); abort-before-run;
  spawn-rejection (counter AND throttle still consumed, slot cleared,
  `aclose` does not hang); only-if-current settle; `aclose`+`abort` under
  an active non-cancellation exception without masking it (G2);
  attempt-deadline cutoff at the derived `egress_max_s`, incl. its
  permanence (G4); bounded send proven independent of the drain budget
  (`aclose` given a 100× larger budget still returns via the send
  timeout); R5 — the runner awaits the sink in ITS OWN task (task-identity
  + no new task appears) and `asyncio.wait_for` is patched to explode.
- Dispatcher (`tests/test_dispatcher.py`, fake-worker harness):
  `stdout_tick` is the ONLY forwarded stderr milestone (first_codex_output
  and terminal_event_seen are consumed but not forwarded, G3); credited
  rollout growth forwards exactly `rollout_ticks` times (the baseline stat
  earns none); gemini and `pre_terminal_idle=0` still forward stdout-tier;
  `on_liveness=None` and an ACTIVE observer produce an identical kill
  envelope, with the pre-egress raw string still pinned byte-for-byte; a
  callback raising at the stdout site cannot stop the drain (the LATER
  milestones still land) and at the rollout site cannot stop the watchdog
  (growth still credited, stall verdict still reaches the raw) — G1;
  a callback raising `CancelledError` — which is NOT an `Exception` and
  would otherwise leave the watchdog through its own cancellation handler,
  silently disarming the kill until overall_timeout — is contained at both
  sites too; jitter/invariant-1 regression across four observer variants
  (none / synchronous work / raising / permanently hung sink): every kill
  lands in `[threshold, threshold + poll_floor + slack]` AND within
  `poll_floor` of the observer-free baseline; a 50-tick burst arriving in
  ONE stderr chunk must be consumed whole, which catches a per-tick stall
  in the milestone branch deterministically (no wall clock involved) —
  the count is load-bearing, since `_read_framed_stdout` reads its 23-byte
  header a byte at a time and needs ~30 scheduler turns to win the race,
  so at 20 ticks a 10 ms stall still passes while at 50 a 1 ms one fails.
  Scope note on invariant 1: those cover its jitter half plus per-tick
  stalls in the drain, and `note` being a plain `def` that returns `None`
  makes awaiting the egress from a kill path unrepresentable. "No awaits
  anywhere in the watchdog loop" remains a review-enforced code contract
  as the design words it: an await added there shifts the kill by less
  than the loop's own poll floor, i.e. inside the tolerance the design
  explicitly permits, and one added in the source moves every observer
  variant equally.
- Server lifecycle (`tests/test_server.py`): a dispatcher tick reaches a
  REAL client as a progress notification through the registered tool —
  the only test spanning the tool-body → `_invoke_*` hop, and therefore
  the one that catches `on_liveness=note` being dropped from the
  `build_server` lambdas, a mutation that disables the feature outright
  while everything else stays green; the REAL Dispatcher's pre-flight
  reject sends nothing (the other three no-spawn SHAPES are faked, since
  only `request_too_large` is reachable without patching); all four
  no-spawn returns (request_too_large / disk_unavailable / spawn_failed /
  prompt_file_error) create-then-`aclose` an egress that never sent, with
  the prompt-file case proven by an exploding Dispatcher constructor; a
  raising `_invoke_*` passes the awaited barrier first and reaches FastMCP
  as the ORIGINAL exception type, not a task-group ExceptionGroup (G2);
  the cancel path aborts WITHOUT a drain and re-raises CancelledError;
  normal return settles the send before the envelope leaves; the knob off
  hands the invoke `None`; a missing Context degrades to the silent path;
  `build_server` threads the knob into both tool bodies; the observer
  actually reaches `Dispatcher(...)` — and gemini's re-dispatch reuses the
  SAME one, so `progress` never restarts (F2); a shielded sink delays the
  response by exactly its shield and no longer.
- Protocol (`tests/test_liveness_protocol.py`, real `mcp` SDK over memory
  streams, frames observed raw): blocked writer (buffer 0) — while the
  tool's invoke has already returned and only `aclose` is holding, exactly
  ONE sender is parked (the notification; a queued response would mean the
  barrier failed), the progress frame comes out first, the result second,
  and nothing trails it. Progress also proven to reach the wire when
  `note()` is called from a NESTED asyncio task, which is the only shape
  production uses. SDK writer-serialization sources: `mcp/shared/session.py`
  :316-349 (notifications and responses share `_write_stream.send`) and
  `mcp/server/stdio.py`:75-81 (single sequential JSON-line writer).
- Protocol cancellation (H1, mandatory): with the real SDK, an in-flight
  send is held, `notifications/cancelled` is issued, and no progress frame
  follows the SDK's cancellation error response — with the EXACT shipped
  spawn mechanism. The handler parks in a SHIELDED scope so its cleanup
  provably cannot run, isolating the structural claim from the
  handler-cleanup race (which wins under a plain await and would otherwise
  mask a broken mechanism). A negative control swaps ONLY the spawn for
  the rejected detached one and shows a progress frame landing after the
  response.
- Mutations run against the shipped code, each caught by the intended
  test: M1 detached `create_task` spawn → H1 gate fails with a trailing
  progress frame; M2 drop `aclose` on the success path → blocked-writer +
  server barrier tests fail; M3 `asyncio.wait_for` for `anyio.fail_after`
  → R5 test fails; M4 nested task given an empty contextvars Context →
  the nested-task protocol test fails (no frame at all); M5 drop the
  spurious-cancel guard → both spurious-sink tests fail; M6 re-bound
  `aclose`'s post-cancel wait → the resisting-sink barrier test fails;
  M7 drop the caller's `settled` backstop → the rogue-child test fails
  with `UnboundLocalError`; M8 backstop raises `CancelledError` → the
  session-survival protocol test fails with `anyio.EndOfStream`; M9 guard
  over-swallows a REAL cancellation → the propagation test fails on
  `cancelled_caught`. From the codex I2 r4 coverage audit: delay `abort()`
  by 0.25 s, make `abort()` settle the flight, set `_closed` only when
  idle, hardcode the send timeout, and return from `_send` before the sink
  — each now fails its named test (all five verified). From the I2 r8
  audit of the previously un-reviewed tiers: M-L drop `CancelledError`
  containment in `_emit_liveness` → both `[cancel]` G1 cases fail (the
  rollout one only after hanging to overall_timeout, which IS the
  symptom); M-M delete `on_liveness=note` from the tool bodies → only the
  new end-to-end client test fails; M-N move a diagnostic only when an
  observer exists → the envelope-equality test fails; M-O emit from the
  real pre-flight reject → the real-Dispatcher silence test fails; M-P
  construct a Dispatcher before prompt-file validation → that test fails;
  M-R keep `note` a plain `def` but return the sink coroutine → the
  structural guard fails on `is None`.
- Two claims are argued rather than tested, deliberately: "no notification
  can EVER trail a response" (a finite quiet window cannot prove absence —
  the tests instead assert the structural state that makes a later frame
  impossible: egress closed with no live flight, or the send CANCELLED),
  and "stdio uses one sequential writer" (source-cited, plus the live gate
  ran 14 progress frames then the result over real stdio with nothing
  interleaved).
- Full run: `pytest -m ""` → 506 passed, 2 skipped.
- Live (F8 gate) — **PASSED before merge**, 2026-08-21, twice. Scratch
  .mcp.json behind a stdio tee, `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=90000`,
  real `codex` call driven by `claude -p`.
  - Run 1 (pre-review-fix tree): 838 s call (t=11.2 → 849.3), 14 frames
    1.0 → 14.0; max wire gap 73.8 s.
  - Run 2, re-run because the I2 r1/r2 BLOCKER fixes landed after run 1:
    782 s call (t=13.9 → 795.6), 13 frames 1.0 → 13.0; gaps 68.5 s then
    60.0-61.3 s. Exactly one source change post-dates it — r8's
    `CancelledError` containment in `_emit_liveness`, an added `except`
    clause that cannot execute when the callback returns normally — so it
    is unreachable on this path and the gate's evidence carries.
  Both runs: progress captured BEFORE the 120 s auto-background transition
  and AFTER it; BOTH evidence tiers exercised live — `stdout activity`
  first, then `rollout activity` once codex went stdout-silent, which is
  exactly the pure-reasoning case the rollout tier exists for; every gap
  under the 90 s cap; `success=true` with no host abort; and the result
  frame arrived AFTER the last notification with nothing following it —
  the aclose barrier holding on a real stdio wire. Without the feature
  either call dies at 90 s.
- Soak: next ACF/shushu 30-min-class review passes 1800 s un-killed.

## Review log

- R1 (codex 01a01fa1-c7c3, 2026-08-20): NEEDS-REVISION. F1 wording+jitter
  test (accepted); F2 request-scoped egress (accepted, design change); F3
  awaited aclose barrier (accepted); F4 throttle state machine (accepted);
  F5 rollout attribution (partially — bounded by the egress lifetime
  instead of serializing SESSION_ID, which stays parked item 4); F6
  lifetime bound (accepted); F7 config default + wording (accepted); F8
  acceptance gates (accepted; default-on stance held AGAINST the "gate
  before default-on" ask — see Default-on stance).
- R1' (gemini fc07d2ab, 2026-08-20, independent lens): NEEDS-REVISION.
  M1 "ctx must be in the tool signature" — REFUTED as blocker by live
  probe evidence (get_context() without a ctx param captured the token);
  folded as an explicit mechanism note. M2 hardcoded-5400 brittleness —
  accepted (derived single-source egress_max_s). M3 cancel-unwind bypasses
  the awaited barrier — accepted and elevated (drain/abort split).
  Default-on stance: ACCEPT.
- R2 (codex, same session, 2026-08-20): NEEDS-REVISION, G1-G6 — all
  accepted: G1 call-site containment (dispatcher does not trust the
  egress's no-raise promise); G2 awaited aclose on ALL response-producing
  paths incl. unexpected exceptions; G3 first_codex_output demoted to
  arming-only (emitted pre-transient-classification); G4 attempt deadline
  subtracts SEND_TIMEOUT_S and the bound claim is scoped to the host's
  default idle cap; G5 live gate: idle 90 s / call >210 s / progress
  captured across the background transition; G6 sink protocol
  `(float, str)`, capture in tool bodies, Docket-branch scoping. M1
  REFUTED / M2 CONFIRMED / M3 CONFIRMED. Bonus verification: SDK writer
  serialization proven (session.py:316-349, stdio.py:75-81). Default-on:
  ACCEPT — adjudicated by both reviewers; stance final.
- R3 (codex, same session, 2026-08-20): G1/G3/G4/G5/G6 RESOLVED; G2
  reframed into H1 (BLOCKER, accepted): the r4 claim "no response on the
  cancel path" is false — `RequestResponder.cancel()` sends a cancellation
  error response independently of handler cleanup
  (mcp/shared/session.py:138-150; mcp/server/lowlevel/server.py:773-781),
  so a detached send task could write after it. Fix: sends spawn into a
  request-scoped anyio task group (scope-cancel gives structural
  ordering) + mandatory protocol-cancellation gate; acceptance wording
  corrected (cancellation is a request, not settlement); stale r2
  section labels fixed.
- R4 (codex, same session, 2026-08-21): H1 STILL-OPEN as first folded —
  the spawner contract was unimplementable: anyio `start_soon()` returns
  None (no task handle for done-callback/cancel/settlement), and an
  uncaught child exception cancels the whole task group. Accepted:
  `_Flight` record (completion event + cancellation state + child-local
  CancelScope), runner swallows all non-cancellation exceptions,
  finally-signals + only-if-current slot clear, abort-before-run flag,
  spawn-rejection completes the slot while consuming counter+throttle;
  Invariant-1 `create_task` contradiction fixed.
- R5 (codex, same session, 2026-08-21): H1 STILL-OPEN on one point —
  `asyncio.wait_for` internally creates a separate asyncio task
  (tasks.py:426-460) that escapes the request-scoped task group,
  reopening the cancellation-response race. Accepted: bounded send via
  direct `anyio.fail_after` await inside the runner (timeout cancels the
  runner itself, in-scope); gate wording pinned to that mechanism; stale
  r5 labels fixed. No other substantive issue found.
- R6 (codex, same session, 2026-08-21): H1 RESOLVED — direct
  `anyio.fail_after` keeps the sink in the request-scoped runner; the
  mandatory real-SDK gate validates cancellation ordering. **Verdict:
  DESIGN: APPROVE**; ready for implementation review, the diff to receive
  its own multi-round pass.

### Implementation review (the diff's own pass, 2026-08-21)

- I1 (gemini fc07d2ab → 575508c5, independent lens): APPROVE with one
  MAJOR — the `aclose` deadline bounds the function, not the handler,
  because the request-scoped task group joins the straggler regardless.
  Accepted and folded into residual risk, then superseded by I2's sharper
  reading. MINOR (`MIN_INTERVAL_S` is a fixed floor, not derived from the
  host cap) recorded as residual risk. Confirmed no task/`_Flight`/event
  leak, bounded state, and spec-legal progress semantics.
- I2 r1 (codex 01a02068): NEEDS-REVISION, two BLOCKERs, both reproduced
  locally before acceptance. (a) A sink-raised `CancelledError` is not an
  `Exception`, so it escaped `_send` and cancelled the tool's task group;
  `__aexit__` then swallowed that cancellation and the handler fell
  through to `return result` with `result` unbound — `UnboundLocalError`
  on a call that had SUCCEEDED. (b) `aclose`'s second bounded wait made
  the barrier a lie: it could return with the flight still live. Fixed by
  the `Task.cancelling()` discriminator plus a caller-side `settled`
  backstop, and by making the post-cancel wait unbounded.
- I2 r2: NEEDS-REVISION, one BLOCKER — my own backstop raised
  `CancelledError`, which the SDK reads as transport shutdown when the
  client never cancelled. Verified against the vendored SDK and by
  mutation (`anyio.EndOfStream`, dead session). Now `RuntimeError`, with a
  protocol test asserting the next `ping` still answers. Codex also
  corrected the rationale I had written: `_completed` is set only by
  `respond()` and `cancel()`.
- I2 r3: APPROVE, no findings at any severity, after re-verifying the
  `cancelling()` discriminator against anyio's cancel/uncancel bookkeeping
  and every path that could raise `CancelledError`.
- I2 r4 (coverage audit, claim by claim): NEEDS-REVISION. One MAJOR (the
  abort test proved neither "undelayed" nor "requested, not settled") plus
  a set of test smells: assertions placed after a task-group join that the
  join forces anyway, negatives that pass if their subject never runs, a
  send-timeout bound loose enough to admit a hardcoded value, a "throttle"
  test that was really a single-flight test, and a fixed sleep where
  polling belongs. All applied; each fix verified by the mutation codex
  named.
- I2 r5: NEEDS-REVISION, two MINOR — the behavioural timeout tests proved
  cancellation but not that the CONFIGURED budgets were wired into the
  anyio scopes. Added a test recording the delay actually passed to
  `fail_after`/`move_on_after` for both injected and default values.
  Also flagged the 50 ms abort-latency bound as scheduling-sensitive
  (widened, and demoted to an operational probe behind the structural
  assertions). The warning was immediately borne out elsewhere: an
  `elapsed >= 0.4` lower bound after `anyio.sleep(0.4)` flaked in a full
  run, since the loop may return inside its own clock resolution.
- I2 r6: NEEDS-REVISION, one MINOR — the last stopwatch lower bound, both
  flake-prone and redundant. Replaced with `flight.done.is_set()` +
  `flight.scope.cancel_called`, which also catches a mutation the
  stopwatch form passed (aclose waiting without ever cancelling).
- I2 r7: APPROVE — nothing left to change, **scoped to what codex had
  actually been sent**: rounds 1-7 only ever received `_liveness.py`,
  `_run_with_liveness`, `test_liveness.py` and `test_liveness_protocol.py`.
- I2 r8, the slice nobody had reviewed (`dispatcher.py` + `config.py`
  source, and the ~770 test lines in `test_dispatcher.py` /
  `test_server.py` / `test_config.py` — codex had flagged this exclusion
  itself in r4): NEEDS-REVISION, two MAJORs, both real.
  (a) `_emit_liveness` caught `Exception`, so a callback-raised
  `CancelledError` — not an `Exception` — escaped. At the rollout site it
  leaves `_arm_pre_terminal_idle` through its own cancellation handler:
  no `kill_event`, no kill, the call hangs to overall_timeout instead of
  dying at the threshold. At the stdout site it ends the drain. Contained
  explicitly now: `cb` is SYNCHRONOUS, so it has no suspension point and
  any `CancelledError` out of it is necessarily callback-originated —
  the same class of bug as r1's sink guard, at a second site.
  (b) The tool-body → `_invoke_*` hop had NO test: deleting
  `on_liveness=note` from both `build_server` lambdas disabled progress
  entirely and the whole suite stayed green. The knob test only observed
  the `enabled` flag; the constructor-recording tests started one hop too
  late. Closed with an end-to-end test through a real `Client` with a
  `progress_handler`. Also: envelope-equality widened to the diagnostics
  key set plus closeness on measured durations; a real-Dispatcher
  pre-flight case added beside the faked shapes; the prompt-file case now
  uses an exploding constructor; and the jitter test's spread bound —
  which I had independently proven VACUOUS by arithmetic (the per-run
  bounds already implied it) — replaced by a delta against the
  observer-free baseline, with bounds re-derived from measurement.
- I2 r9-r12: convergence on the reopened slice. r9 APPROVE on the fixes,
  and agreed that no behavioural test can prove the absence of an
  arbitrary future `await`. r10-r11 then reviewed the two tests I wrote
  AFTER r9 (shipping those unreviewed would have repeated r8's gap): the
  `note` guard proved only the DECLARATION, so a plain `def` returning the
  sink coroutine passed it — now it asserts the return is `None`; and the
  burst test's wall-clock half was redundant and flake-prone, so it is
  gone, leaving a purely deterministic ordering check. One codex claim was
  refuted with measurement — "twenty ticks are ample" is false, since at
  20 ticks a 10 ms per-tick stall still passes; the sweep fixed the count
  at 50 and codex accepted the mechanism. r12: **APPROVE, nothing left.**
