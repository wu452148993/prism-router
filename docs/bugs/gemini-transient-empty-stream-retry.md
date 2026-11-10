# Gemini transient empty/malformed stream — bounded internal retry

## Symptom

On Chinese Windows (codepage 936), gemini calls intermittently fail with:

```json
{"code": "gemini_error", "stage": "classify", "retryable": false,
 "raw": "Invalid stream: The model returned an empty response or malformed tool call."}
```

The raw text originates from gemini-cli (empty assistant turn caused by
shell-pipe encoding mojibake upstream). Identical calls succeed on immediate
re-invocation. The encoding root cause is mitigated at the prompt layer
(global GEMINI.md guardrail, verified 5/5); this retry is availability
hardening for residual intermittent failures.

## Fix

- `classify.is_transient_empty_stream()` — pure predicate: `gemini_error` +
  `stage=classify` + raw matching (case-insensitive) `empty response` /
  `malformed tool call` / `invalid stream`. The matching catch-all envelope
  is marked `retryable=True`; all other catch-alls stay `retryable=False`.
- `server._invoke_gemini` re-issues the identical job (same prompt, flags,
  cwd, SESSION_ID) at most once, after `_GEMINI_TRANSIENT_RETRY_DELAY_S`
  (1 s). Gemini-only; codex paths untouched.
- On exhaustion the last error envelope is returned shape-unchanged with
  `diagnostics.internal_retries = 1`. Retried successes force diagnostics
  emission (same gate as `recovered_from_rollout`) so the retry stays
  visible on the default flag-off envelope.
- The error-FREE variant of the same upstream defect (gemini-cli swallows
  InvalidStreamError → clean `success` with empty `agent_messages`) shares
  this one-retry budget and fails closed as `empty_final` on exhaustion —
  see [gemini-empty-final-fail-closed.md](gemini-empty-final-fail-closed.md).
