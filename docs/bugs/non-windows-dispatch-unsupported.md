# Non-Windows hosts cannot dispatch: the Job Object kill switch has no POSIX path

Status: known legacy defect, deferred by operator decision on 2026-10-08 while
adding call lineage. Recorded so non-Windows support can be restored on purpose.

## Symptom

On Linux / macOS every `codex` / `gemini` call fails. By code reading the
server itself should start — nothing Windows-only runs before the first call,
and `ctypes.wintypes` imports off Windows (CPython 3.11 `cfield.c` registers
its `v` format unconditionally) — and each tool registers if its own
`--version` probe passes. No non-Windows host was available to run any of it.

The failing hop: `_default_job_assign` → `_job_object.assign_worker` →
`_get_kernel32()` raises `OSError("Job Object support requires Windows")`.
`_run_worker` only catches `JobAssignError`, a plain `Exception` subclass
(`_job_object.py`), so the error escapes the tool body. By code reading, the
client then gets FastMCP's generic tool error instead of a §4.2 envelope,
and the worker spawned just before is left to garbage collection rather than
terminated.

Reproduced on Windows by faking the platform for the assign hop (2026-10-08):

```
sys.platform = "linux"; dispatcher._default_job_assign(12345)
→ OSError: Job Object support requires Windows   # escapes `except JobAssignError`
```

## Root cause

`83ce238` (2026-05-12, per-call worker subprocess) made `CloseHandle(job)` on a
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` job the universal kill switch and added no
POSIX counterpart. `docs/mcp-audit/design-rationale.md` still states
"Platform: Linux + Windows".

Windows-only by the same decision: call lineage (`docs/lineage.md`) appends
through a `FILE_APPEND_DATA` handle and walks parents with Toolhelp;
`lineage.configure()` leaves it off elsewhere.

## Restoring non-Windows support

1. A POSIX kill switch with the Job Object's guarantee (worker, backend and
   every grandchild die together). A process-group kill is the obvious start,
   but descendants that call `setsid` leave the group; verify the choice on a
   real host.
2. Fail closed: any assign failure must terminate the worker and return
   `spawn_failed`, whatever the exception type.
3. Lineage: append with `os.open(O_APPEND)` — POSIX requires the end-of-file
   positioning and the write to happen with no intervening modification — and
   read parents from `/proc/<pid>/stat` (ppid is field 4; start time is field
   22 in clock ticks since boot, plus `btime` from `/proc/stat`, so roughly
   1 s absolute precision). macOS needs its own walk.
4. Run the suite on a real non-Windows host (the lineage tests skip off
   Windows) and correct the platform line in `design-rationale.md`.
