# A cancel during the request write leaves the stderr drain running

Status: known legacy defect, deferred by operator decision on 2026-10-08
(found during the call-lineage review). Lineage ignores child ids that arrive
after its call ended.

## Symptom

`_run_worker` starts `stderr_task` (`_drain_stderr`) before it writes the
framed request to the worker. If the MCP call is cancelled while that write is
in flight, `CancelledError` leaves through the outer `try/finally`, which only
closes the job. The cancel handler around the later `asyncio.wait(...)` — the
one that cancels `stdout_task`, `stderr_task`, `timeout_task` and `kill_wait`
— is never reached, so the drain outlives the call until the killed worker's
stderr reaches EOF.

Found by code reading (codex review, confirmed against `_run_worker`); not
reproduced.

## Impact

`job.close()` kills the worker tree, and the drain ends when stderr reaches
EOF; `_drain_stderr` cancels any watchdog it armed when it exits. How long the
stray task lives has not been measured. The cancel can land while the write
is still awaiting backpressure after the whole frame already reached the
worker, so a backend may have started and milestones may still be buffered.
Lineage's call record is sealed once its `end` row is written
(`lineage._Call.child` ignores later ids).

## Fix sketch

Cancel and await `stderr_task` on every exit path of `_run_worker` (for
example in the outer `finally`) — `cancel()` only requests it — without letting
a cleanup cancel or failure replace the call's result or original exception.
Add a test that cancels during a stalled request write and asserts the drain
task is done before `_run_worker` returns.
