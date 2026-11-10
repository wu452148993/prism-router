# BUG (low-priority hygiene): `_GatheringFuture exception was never retrieved`

> Standalone brief for a fresh session. Self-contained — no prior context needed.
> All timestamps in **local UTC+8** unless tagged `Z` (UTC).

## 1. Symptom

When an MCP cancel triggers `runner.run()`'s `CancelledError` path, stderr
emits exactly one warning per cancel:

```
_GatheringFuture exception was never retrieved
future: <_GatheringFuture finished exception=CancelledError()>
Traceback (most recent call last):
  File "C:\Python311\Lib\asyncio\subprocess.py", line 134, in wait
    return await self._transport._wait()
  File "C:\Python311\Lib\asyncio\base_subprocess.py", line 230, in _wait
    return await waiter
asyncio.exceptions.CancelledError
```

Functional impact: **none**. The cancel-leak fix
(`docs/bugs/mcp-cancel-process-kill.md`) lands cleanly; readers EOF, kill chain
runs, `await task` returns, descendants reaped. This is a cosmetic asyncio
hygiene warning surfaced to stderr.

## 2. Root cause (suspected, code-read)

`runner.py:285-289`:

```python
await asyncio.wait_for(
    asyncio.gather(_write_stdin(), proc.wait()),
    timeout=t.overall,
)
```

On `CancelledError`, the inner `gather(_write_stdin(), proc.wait())` is
cancelled. `proc.wait()` raises `CancelledError`. The gather absorbs it (the
gather itself also cancels, propagating up), but the resulting `_GatheringFuture`
holds a `CancelledError` that no one ever calls `.result()` on — asyncio's
default `__del__` complains.

The warning is pre-existing; it was masked before because cancel never reached
this codepath cleanly. The shield-loop fix exposes it.

## 3. Candidate fixes (not yet evaluated)

### 3.1 Decouple write-stdin from proc.wait

```python
write_task = asyncio.create_task(_write_stdin())
try:
    await asyncio.wait_for(proc.wait(), timeout=t.overall)
except asyncio.TimeoutError:
    ...
except asyncio.CancelledError:
    ...
finally:
    if not write_task.done():
        write_task.cancel()
    await asyncio.gather(write_task, return_exceptions=True)
    ...
```

Trade-off: simpler control-flow, kills the warning, but adds a fourth task
to the orchestration (stdin writer + 2 readers + waiter). Test surface grows.

### 3.2 Retrieve the exception explicitly

Wrap the gather in a task and call `.exception()` in finally to mark it
retrieved. Mechanical but ugly.

### 3.3 Use `return_exceptions=True` on the inner gather

Would silence the warning but masks real `_write_stdin` errors. Reject.

## 4. When to act

Low priority. Surfaces only on cancel paths (timeout / user-cancel), one
line of stderr per event. Does not affect the envelope, kill chain, or test
suite. Fix when touching `runner.run()` for another reason, or when this
warning starts being noisy in production logs.

## 5. Out of scope

- The cancel-leak bug itself (closed in `mcp-cancel-process-kill.md`).
- Codex post-exit hang (separate, `codex-post-exit-hang.md`).
