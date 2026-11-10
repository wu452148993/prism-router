# transport_failure (WinError 267) hides a backend-spawn cwd failure

## Symptom

Back-to-back `codex` then `gemini` calls (same `cd`, Windows) both returned:

```
error.code=transport_failure  error.stage=shutdown  retryable=true
raw="reason=transport_failure rc=0 err=frame parse: RunOutcome missing parse_result"
stderr_tail: {"ev":"terminating","error":"[WinError 267] 目录名称无效。"}
             {"ev":"terminated","error":"exception"}
```

`[WinError 267]` = `ERROR_DIRECTORY`. A later `gemini` retry (same cwd) succeeded.

## Root cause

The backend CLI spawn inside the worker — `runner.run`'s
`create_subprocess_exec(cwd=str(cfg.cwd))` → Windows `CreateProcessW` — raised
`[WinError 267]`. The most likely cause is that the working directory was
momentarily not a valid directory at the spawn instant: both calls hit it and a
retry recovered, consistent with a transient. This causal step is a
high-confidence inference, not proven (see Confidence below).

The exception bubbled out of `run()` → `_run_async` → `worker.main`'s generic
`except`, which wrote a frame with `parse_result=None` carrying the real
traceback only in `worker_exception`. The dispatcher's `_parse_runoutcome_dict`
raised `ValueError("RunOutcome missing parse_result")` (discarding
`worker_exception`); with no `kill_reason` set, `_build_kill_result` synthesized
the generic `transport_failure/shutdown` envelope.

Confidence: spawn-origin is high (localized to `_run_async`; the only shared
variable across both backends is the `cwd=` arg; the dispatcher spawns the
worker WITHOUT `cwd`, so 267 cannot be the worker-process spawn). The exact
transient trigger is unconfirmed — prism persists no per-run log and the
traceback was discarded. `rc=0` is not load-bearing — most likely a Job-teardown
artifact, but unproven.

## Fix

Two diagnostic hunks (codex-reviewed, 3 rounds → NO OBJECTIONS — SHIP):

- `dispatcher._parse_runoutcome_dict`: surface `worker_exception` in the raised
  error instead of the generic "missing parse_result", so the envelope `raw`
  now carries the real `[WinError 267] ...` stack.
- `worker.main`: record the full traceback (`format_exception`) instead of
  `format_exception_only`, so the surfaced message includes stack context.

The fix makes the failure self-describing; a recurrence now pins the trigger
directly. It is NOT an error-taxonomy change — a backend-spawn abort still
classifies as `transport_failure`.

## Deferred

- Dispatcher pre-flight to classify a genuinely-missing cwd as a distinct
  non-retryable error (TOCTOU caveat). A `runner`-level `isdir` guard was
  rejected: TOCTOU, redundant with the surfacing fix, and it breaks
  `test_runner_pure` (uses a nonexistent `cwd`).
- Regression tests: `parse_result=None + worker_exception`; full-traceback
  preservation.
