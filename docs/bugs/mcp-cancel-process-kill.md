# BUG (suspected, unverified): MCP cancel may not kill codex/gemini subprocess tree

> Standalone brief for a fresh session. Self-contained — no prior context needed.
> All timestamps in **local UTC+8** unless tagged `Z` (UTC).

## 1. What we suspect

When an MCP client (claude-code, etc.) cancels an in-flight `mcp__prism__codex`
or `mcp__prism__gemini` tool call, prism is **supposed** to:

1. Receive an asyncio `CancelledError` inside the FastMCP tool function.
2. Unwind through `runner.run`'s `finally` blocks.
3. Kick `_terminate_chain(...)` — `runner.py:194-235`.
4. On Windows: walk the process tree via `taskkill.exe /PID <pid> /T /F`
   (`runner.py:116-144`); on POSIX: `proc.terminate()` → `proc.kill()`.
5. Result: codex/node/cmd.exe descendants gone within ~5 s of cancel.

**Hypothesis to verify**: this chain does NOT actually fire on cancel, or fires
but fails to reap all descendants. Symptom would be: cancel returns
`MCP error -32001: user-cancel` to the client immediately, but `codex.exe` /
`node.exe` keeps burning CPU/tokens in the background until codex finishes
on its own (or is manually killed).

## 2. Why this is **unverified, not confirmed**

We initially thought the original incident proved it. We were wrong.

The original case (see `docs/bugs/codex-post-exit-hang.md` §2):
- Tool call started:  **15:45:16**
- codex `ended_at`:   **16:39:13**  ← codex finished on its own
- User cancel:        **17:13:04**  ← came AFTER codex was already gone

So the cancel happened **34 minutes after** codex had already exited cleanly.
That incident says nothing about whether cancel-while-running kills codex,
because nothing was running to kill. **This is a separate bug to test in
isolation.**

## 3. Verification plan

### 3.1 Prerequisites

- prism-router MCP server running (the build under audit).
- claude-code or any MCP client that supports cancelling tool calls.
- Two terminals: one to issue the cancel, one to watch processes.

### 3.2 Repro steps

**Terminal A — start a long codex call via MCP, then cancel ~10 s in:**

Use a prompt that guarantees codex stays busy for 60+ seconds:
```
Read every .py file under src/ and tests/ via the shell tool, one file per
call, then write a 500-word summary. Do not use grep, use cat per file.
```
Issue the call, wait until you see codex's first activity (or ~10 s), then
cancel from the MCP client.

**Terminal B — process tree probe (PowerShell):**

Right BEFORE cancelling, snapshot:
```powershell
Get-Process | Where-Object { $_.Name -in 'codex','node','cmd' } |
  Select-Object Name,Id,StartTime,CPU |
  Format-Table -AutoSize
```

Right AFTER cancelling, repeat the snapshot every 2 seconds for ~30 s:
```powershell
1..15 | ForEach-Object {
  Write-Host "=== t+$($_*2)s ==="
  Get-Process | Where-Object { $_.Name -in 'codex','node','cmd' } |
    Select-Object Name,Id,StartTime,CPU | Format-Table -AutoSize
  Start-Sleep -Seconds 2
}
```

### 3.3 Pass / fail

- **Pass**: all codex/node/cmd descendants of the prism-spawned tree disappear
  within ~10 s of cancel. The kill chain is fine.
- **Fail (the bug)**: codex/node processes persist after cancel, possibly
  for minutes, with non-zero CPU. The kill chain is broken.

A subtler fail mode: the `cmd.exe` wrapper exits but `node.exe` (the actual
codex binary) survives — that's an incomplete tree-kill.

## 4. Suspected failure modes if it does fail

### 4.1 `runner.py` cleanup unreached on cancel

The cancel path goes through asyncio's exception propagation. If FastMCP
catches `CancelledError` higher up than `runner.run` and never gives the
`finally` blocks a chance to run, `_terminate_chain` is never invoked and
the process tree leaks until the next event loop iteration kills the
PIPE-blocked tasks (or never).

Check: does the `finally:` in `runner.py:276-283` actually execute on cancel?
Drop a debug log there and verify.

### 4.2 `_terminate_chain` runs but `taskkill /T` misses descendants

`runner.py:135-141`:
```python
killer = await asyncio.create_subprocess_exec(
    "taskkill.exe", "/PID", str(pid), "/T", "/F",
    ...
)
await killer.wait()
```
The PID passed is `proc.pid` — the cmd.exe wrapper's PID. `taskkill /T`
walks descendants via the parent-PID chain. On Windows this normally works,
but failures have been observed when:
- A descendant detaches from the job/parent (rare but possible).
- The PID was reused before taskkill ran (very rare in practice).
- AV / EDR software blocks taskkill silently (check exit code, not just
  whether it returned).

Check: log `killer.returncode` and stderr. Currently they're discarded
(`stderr=subprocess.DEVNULL`).

### 4.3 Cancel returns to client before kill completes

Even if the kill chain runs correctly, FastMCP may already have replied
`MCP error -32001` to the client well before the kill finishes. From the
user's perspective the call is "cancelled" but the process leak is invisible
unless they go look. This is a UX bug rather than a leak — but worth noting.

## 5. If it fails, candidate fixes

- Add `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` (already implicit
  via `CREATE_NO_WINDOW`? — verify) so the whole tree dies on signal.
- Use a Windows Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) — this is
  the bulletproof way; when the job handle drops, the kernel reaps every
  descendant. Python doesn't expose this in stdlib; need pywin32 or ctypes.
- Stop discarding `taskkill` stderr/exit code — surface it in `stderr_tail`
  or diagnostics so failures are visible.
- Add a post-kill verification pass: poll for surviving PIDs and re-kill if
  any remain after `force_kill_delay`.

## 6. Verdict (2026-05-08)

**FAIL — bug confirmed and fixed.**

Repro (`runner.run` driven directly with the same `CancelledError` path FastMCP
takes): on `task.cancel()`, `_terminate_chain` was never called and `finally`
hung forever on `asyncio.gather(stdout_task, stderr_task)` because the readers
were blocked on `proc.stdout.readline()` against a still-living child. All 7
codex descendants survived 90+ s with rising CPU.

**Failure mode** (none of §4.1/4.2/4.3 — fourth mode):

`finally` ran but only contained reader-drain logic; only the `except
TimeoutError` branch called `_terminate_chain`. CancelledError fell through
to `finally`, which then hung because the subprocess's pipes were still open.

**Fix**: `runner.py` — added `_terminate_with_shield(reason)` helper that
runs `_terminate_chain` as a shielded sub-task (so repeat cancels can't
interrupt the kill mid-flight) and routed both `TimeoutError` and
`CancelledError` through it. `KillStep.reason` extended with `"cancelled"`.

**Post-fix real-link verification**: cancel → `await task` returns in
**0.33 s** (was: never), all 7 descendants reaped within 2 s.

**Regression coverage** (`tests/test_runner_pure.py`):
- `test_cancel_kills_tree_and_unhangs_finally`
- `test_cancel_during_kill_chain_survives_repeat_cancel`

## 7. Out of scope

- The post-exit hang bug (covered separately in
  `docs/bugs/codex-post-exit-hang.md`). The two bugs are independent: the
  hang happens when codex finishes naturally and prism doesn't notice; this
  cancel bug is about whether prism actively kills a still-running codex.
