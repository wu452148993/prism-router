# BUG: prism hangs after codex CLI has already exited cleanly

> Standalone brief for a fresh session. Self-contained — no prior context needed.
> All timestamps in **local UTC+8** unless tagged `Z` (UTC).

## 1. Symptom

`mcp__prism__codex` call does NOT return after the underlying `codex exec --json`
process has finished and codex's own session ledger shows `outcome=completed`.
prism keeps the MCP tool call pending until either:

- the user manually cancels (we observed **~34 min** of dead hang before cancel), or
- prism's overall 7200 s (2 h) timeout fires and force-kills the (already-exited) tree.

User-visible effect: "codex looks idle in the background, but the MCP call is still
yellow/pending in claude-code." Looks like codex hung; codex actually finished.

## 2. Evidence (the call we tracked)

### claude-code session (caller side)
- Session id: `e7dec736-fed5-4894-9389-4ae5dddcc737`
- agentsview URL: <http://127.0.0.1:8080/sessions/e7dec736-fed5-4894-9389-4ae5dddcc737>
- Tool call ordinal **37** — `mcp__prism__codex` — started **15:45:16** (`2026-05-08T07:45:16.745Z`)
- User cancel ordinal **39** — `[Request interrupted by user for tool use]` — at **17:13:04** (`2026-05-08T09:13:04.555Z`)

### codex session (subprocess side)
- Session id: `codex:019e068c-46e9-7e51-882d-a83f93d27905`
- agentsview URL: <http://127.0.0.1:8080/sessions/codex%3A019e068c-46e9-7e51-882d-a83f93d27905>
- `started_at`: **15:45:20** (`2026-05-08T07:45:20.478Z`)
- `ended_at`: **16:39:13** (`2026-05-08T08:39:13.992Z`)
- `outcome`: `completed`, `health_grade`: `A`, `ended_with_role`: `assistant`
- 34 tool calls, last one (ordinal 40) at **16:38:28** (`2026-05-08T08:38:28.609Z`)
- 10 806 output tokens, 42 messages — codex finished real work, not a crash.

### The gap
```
codex ended_at         16:39:13
user manual cancel     17:13:04
─────────────────────  ───────────
dead-hang in prism     ≈ 33 min 51 s
```

During this window codex was already gone (per its own ledger). prism just sat
on the MCP tool call. If the user hadn't cancelled, prism would have waited
until the 7200 s overall timeout (`runner.py:37`).

## 3. Suspected root cause locations

All paths relative to repo root `D:\projects\prism-router\`.

### 3.1 `src/prism_router/runner.py` — the spawn/drain loop
Three candidate hangs, any of which is consistent with the symptom:

**(a) `_read_stdout` / `_read_stderr` never see EOF** (`runner.py:243-263`)
```python
while True:
    line = await proc.stdout.readline()
    if not line:           # ← waits for pipe EOF
        return
```
On Windows, `codex` resolves via `shutil.which` to a `.cmd` (npm shim), spawned
through `cmd.exe` → `node.exe` → possibly more children (codex's own shell
tool subprocesses). When the codex **main** node exits, grandchildren can keep
the stdout pipe handle open → no EOF → reader hangs forever.

**(b) `proc.wait()` never returns** (`runner.py:269-272`)
```python
await asyncio.wait_for(
    asyncio.gather(_write_stdin(), proc.wait()),
    timeout=t.overall,        # 7200 s
)
```
The `asyncio.gather` only completes when BOTH legs finish. If the cmd.exe
wrapper waits for all descendants and any of them lingers, `proc.wait()`
doesn't return. Then we sit here for up to 2 h.

**(c) `_post_terminal_kill` never schedules** (`runner.py:239-254, 252-254`)
The kill chain that would mask (a)/(b) only fires when the StreamParser
flips `saw_terminal=True`, i.e. it sees `thread.completed` or
`turn.completed` events (per `events.py:CODEX_SPEC.terminal_event_types`).
If codex on this session emitted some other terminal pattern (or buffered
the terminal event past the moment where pipe EOF would otherwise have
unblocked us), the kill chain is dormant.

### 3.2 `src/prism_router/events.py` — what counts as terminal
```python
terminal_event_types=frozenset({"thread.completed", "turn.completed"}),
```
If codex's `--json` stream for a long-running review session ends with
something else (e.g., `error`, plain stdout EOF, or a final `agent_message`
not followed by `turn.completed`), `saw_terminal` stays False forever, and
the post-terminal kill never schedules.

### 3.3 No idle timeout exists
Only `CODEX_TIMEOUT` (default 7200 s overall) is enforced. There is no
"N seconds since last stdout byte → kill" guard. Any combination of (a)/(b)/(c)
above will silently waste up to 2 h before the user notices.

## 4. Reproduction hint

Use a prompt that makes codex do many `shell` tool calls and produce a long
final message. The original triggering prompt is preserved at:

- claude-code session `e7dec736-...`, ordinal 37 — full payload available via
  `python tmp-extract.py get e7dec736-fed5-4894-9389-4ae5dddcc737 --raw`
  in `D:\projects\agent-control-framework\` (see §6 for tooling).

A cheaper repro:
```
Read these files line-by-line via the shell tool, one at a time, then
write a 2-page review: src/prism_router/{events.py,server.py,errors.py,
runner.py,parser.py,backend_codex.py}. After each file, summarize.
```

While running, in another terminal:
```powershell
# Watch for codex/node descendants of the prism-spawned process tree
Get-Process | Where-Object { $_.Name -in 'codex','node','cmd' } |
  Format-Table Name,Id,StartTime,@{N='ParentId';E={(Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").ParentProcessId}}
```
Note the moment codex's own logs (under `~\.codex\sessions\` if applicable
or via agentsview) flip to `outcome=completed`, and compare with whether the
MCP call has returned in claude-code.

## 5. What to verify in the new session

1. **Confirm the gap is reproducible.** Is "codex done, prism hanging" deterministic
   for sufficiently long codex prompts, or did our session hit a corner case?

2. **Identify which leg hangs.** Add elapsed-time debug logging to:
   - `_read_stdout` (after `readline()` returns / before the next iteration)
   - `_read_stderr`
   - `proc.wait()` (wrap the gather call)
   - the parser's `saw_terminal` transition
   Determine which one fails to complete after codex exits.

3. **Inspect codex stream tail.** Capture the last ~50 events of a hanging run
   (set `return_all_messages=True` in the MCP args, or temporarily save the raw
   stdout). Check whether `turn.completed` / `thread.completed` is actually
   present at the tail. If not, `events.py:CODEX_SPEC.terminal_event_types`
   needs to grow.

4. **Process tree forensics.** When prism is stuck post-codex-exit, list the
   live descendants of prism's spawned PID (taskkill `/T` walks via WMI parent
   pointers). If grandchildren exist after the codex main exits, that's the
   pipe-EOF-leak culprit.

## 6. Tooling that worked

`D:\projects\agent-control-framework\tmp-extract.py` — wraps the local
agentsview HTTP server (`http://127.0.0.1:8080`):

```bash
# session metadata
python tmp-extract.py meta <session_id>

# tool-call list with timestamps
python tmp-extract.py tools <session_id>

# clean transcript (strip XML wrappers)
python tmp-extract.py get <session_id> --out <file>

# raw XML-tagged md (preserves <tool_call>/<tool_result> blocks)
python tmp-extract.py get <session_id> --raw --out <file>

# search inside one session
python tmp-extract.py search-in <session_id> <token>
```

For codex sessions the id has a `codex:` prefix; URL-encode the colon as `%3A`
when hitting the HTTP API directly. The companion doc with full endpoint
shapes is `D:\projects\agent-control-framework\agentsview-api.md`.

## 7. Out of scope for this bug

- The 54-min codex review duration itself is not a bug; it's expected for a
  heavy review task. The bug is purely the **post-exit dead hang**.

## 8. Followups deferred from this PR

- **F1 — codex `exec --json` single `turn.completed` assumption.**
  **[verified 2026-05-09 vs openai/codex, pinned at commit `d807d44a`;
  cross-checked against `main`]** At most one `task_id`-matching
  `turn.completed` reaches stdout per exec invocation. Safety is enforced
  by the exec event loop in `codex-rs/exec/src/lib.rs`, not the enum
  shape alone:
  - Turn-scoped server notifications are filtered by the exec's
    `task_id` before forwarding. The typed path uses
    `should_process_notification(notification, primary_thread_id, task_id)`;
    the legacy bridge does `if turn_id != task_id { continue }` on the
    `TurnComplete` arm. `task_id` is bound once per exec invocation
    (set from `response.turn.id` after the prompt is sent).
  - `EventProcessorWithJsonOutput::process_server_notification` returns
    `CodexStatus::InitiateShutdown` on `TurnCompleted` / `TurnFailed` /
    `Interrupted`. The loop then awaits `request_shutdown(...)` and
    `break`s — it never polls `next_event()` again, so no buffered
    duplicate same-`task_id` `TurnCompleted` can reach stdout.
  - `codex-rs/exec/src/exec_events.rs` confirms `TurnCompleted` /
    `TurnFailed` are the only turn terminals on the JSONL surface
    (`d807d44a` + `main`).
  Therefore `runner.py` `post_terminal_kill` and the
  `idle_after_terminal` watchdog arming on the first `saw_terminal` flip
  cannot kill a healthy long-running exec mid-session. Assumption pinned
  in `events.py` next to `CODEX_SPEC.terminal_event_types`. Line numbers
  intentionally omitted — re-verify against the pinned commit if upstream
  refactors the loop structure.
  Side-finding (separate from F1, no action): `thread.completed` does
  **not** appear in the upstream `ThreadEvent` enum at `d807d44a` or
  verified `main`. The whitelist entry in `CODEX_SPEC` is harmless
  defensive forward-compat — it currently never fires.
- **F2 — Windows Job Object descendant cleanup.** `taskkill /T /F` on a
  dead-root PID is best-effort; orphaned grandchildren may not be reachable.
  A Windows Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` would close
  the gap atomically. Defer due to ctypes/pywin32 surface; reader cancellation
  via `drain_grace` already guarantees the call returns.
- **F3 — `_terminate_windows_tree` swallows non-zero `taskkill` exit codes**
  (`runner.py:142`). If taskkill misses descendants we lose the signal.
  Either capture the exit code into the kill chain or escalate to job objects
  (F2 supersedes this).
- **F4 — repeat-cancel during `finally` cleanup.** The drain `wait_for` and
  watchdog-join `await`s in `finally` are not shielded. A second cancel
  arriving mid-cleanup (e.g. caller times out the call after we already
  raised `CancelledError`) can interrupt them, skipping reader-cancel /
  taskkill / watchdog-join. Existing `_terminate_with_shield` only protects
  the kill chain itself, not the post-chain hygiene. Pre-existing pattern;
  shielding finally awaits would close the gap but adds complexity.
