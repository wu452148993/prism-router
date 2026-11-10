# Gemini empty-success ("empty final") — bounded re-dispatch, fail closed

## Symptom

`mcp__prism__gemini` intermittently returns `success:true, agent_messages:"",
error:null, diagnostics:null` with a real SESSION_ID (2026-07-10, project
agent-control-framework; gemini sessions `90a094b1-…` and `ae835484-…` first
call). An immediate same-session manual retry succeeded. Callers cannot
distinguish this from a legitimately silent model — in review pipelines an
empty "success" risks being misread as a no-findings verdict.

## Root cause (pinned in installed gemini-cli 0.35.3; NOT a prism capture bug)

Three-link swallow chain, all verified locally:

1. `@google/gemini-cli-core/dist/src/core/geminiChat.js:697-706` — an
   empty/malformed FINAL model round (after internal bounded retries) throws
   `InvalidStreamError` (`NO_RESPONSE_TEXT` / `NO_FINISH_REASON` /
   `MALFORMED_FUNCTION_CALL` / `UNEXPECTED_TOOL_CALL`).
2. `core/turn.js` `Turn.run` catch: `if (e instanceof InvalidStreamError)` →
   yields a `GeminiEventType.InvalidStream` event and returns early — never
   calls `reportError`, so stderr stays clean.
3. `gemini-cli/dist/src/nonInteractiveCli.js` — the event switch has NO case
   for `InvalidStream` (grep = zero hits). The event is silently dropped; the
   loop falls into the "no more tool calls" branch → emits
   `result: {status:"success"}` → exit 0.

CLI transcripts confirm failing turns recorded `content:""` with tool calls
EXECUTED (`~/.gemini/tmp/agent-control-framework/chats/` sessions above). A
live probe (fresh session `5336d539-…`, `return_all_messages:true`)
reproduced the signature: tool_use → tool_result → 5m16s silence →
result:success, zero assistant messages.

The `InvalidStream` event never reaches stdout (dropped before
StreamJsonFormatter), so detection MUST be inferential at the prism layer:
gemini-only, classify returns no error AND assembled `agent_messages` is
empty/whitespace.

## Fix

- `server._invoke_gemini`: an error-free outcome with empty `agent_messages`
  triggers the same single re-dispatch loop as the transient empty-stream
  error ([gemini-transient-empty-stream-retry.md](gemini-transient-empty-stream-retry.md))
  — SHARED one-retry budget and `_GEMINI_TRANSIENT_RETRY_DELAY_S`; at most
  one internal retry per invocation regardless of which trigger fires, or
  both sequentially.
- Exhaustion (still empty after the retry) fails closed:
  `code=empty_final, stage=classify, retryable=True`, raw naming the swallow
  chain + pointing here. SESSION_ID from the final outcome stays on the
  envelope; `diagnostics.internal_retries=1` is forced (same gate as
  `recovered_from_rollout`).
- Gate stays WIDE (no tool_use-presence narrowing): `NO_RESPONSE_TEXT` can
  fire on round 1 too (the cp936-era empty-turn bug was single-round).
  Worst-case false positive cost = one bounded duplicate call.
- Codex paths untouched. (Superseded 2026-08-11: codex now fails closed on the
  same signature but does NOT re-dispatch —
  [codex-transient-retry-storm.md](codex-transient-retry-storm.md).)

## Semantics / edge cases

- Fresh-session retry creates a second backend session; the envelope carries
  the retry's SESSION_ID.
- `-r <sid>` continuation retry appends a duplicate user turn to the session
  (benign — this is exactly the manual recovery that worked in production).
- Degenerate prompts whose legitimate answer is empty ("reply with nothing")
  now surface as `empty_final` after one duplicate call — accepted semantic
  tightening: for prism's delegation use cases an empty final message is
  never a usable answer, and `retryable=True` routes it into downstream
  unavailable→STOP disciplines.
