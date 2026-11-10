# prompt_file git-dir check froze the server: helper spawns inherited the MCP stdin

## Symptom

`codex` / `gemini` calls with `prompt_file` in a linked worktree's git dir
(outside `cd`) stalled in `_git_dir()` before backend dispatch. They waited
until Claude Code's idle cap (`sent no response or progress for 1800s;
aborting`) or a connection drop, and concurrent requests stopped progressing
while the event loop was blocked. Paths resolving inside `cd` skip `_git_dir()`
and do not trigger the stall.

| when (UTC) | calls (`prompt_file` under `.git/worktrees/<wt>/acf/`) | outcome | source |
|---|---|---|---|
| 2026-09-12 20:54–21:34 | codex, gemini, gemini | first two failed at exactly 30:00 (1800 s cap); third stopped by the operator, then `Connection closed` | CC transcript `56ffd113…jsonl` |
| 2026-10-05 16:29–17:00 | codex, gemini | codex failed at 30:00; gemini stopped; `Connection closed` | CC transcript `4512a89b…jsonl` |
| 2026-10-05 19:04–19:09 | codex, then gemini 2 min later | nothing until `STDIO connection dropped`; then, within 9 ms, codex's `prompt_file_error` (`outside allowed roots`) arrived for a request id the host had already dropped, and the gemini call completed | MCP log `mcp-logs-prism/2026-10-05T18-58-53-713Z.jsonl` lines 6–24 |

Environment: Windows 10, FastMCP 3.2.4 / mcp 1.27.0, Python 3.11.3, Git for
Windows 2.30.0. On the host's PATH `git` resolves to the `Git\cmd\git.exe`
launcher, which runs the real `mingw64\bin\git.exe` as a child.

## Root cause

1. **git inherited the JSON-RPC pipe.** `_git_dir()` called `subprocess.run`
   with `capture_output=True` and no `stdin=`, so CPython handed git a duplicate
   of the server's own stdin (`GetStdHandle(STD_INPUT_HANDLE)` in `_get_handles`).
   Under MCP stdio that handle is the request pipe; the transport wraps
   `sys.stdin.buffer` in a `TextIOWrapper` and awaits each `readline` in a worker
   thread (`mcp/server/stdio.py`). I/O on that synchronous file object is
   serialized [1], so a child's startup operation on an inherited duplicate can
   wait behind a pending read until a message or EOF completes it. Which startup
   operation blocks was not traced.
2. **The 5 s timeout was no bound.** On `TimeoutExpired`, `run()` kills only the
   direct child, then calls `communicate()` without a timeout to drain the pipes
   (CPython 3.11.3 `Lib/subprocess.py:548-563`; open upstream as [2]). Through the
   launcher, the real git — still blocked — keeps stdout/stderr open; in the
   probes `run()` returned only after EOF. With mingw64's `git.exe` first on PATH
   the probes timed out after 5.02 s and the check failed closed.
3. **The stall ran on the event loop.** `_invoke_codex` / `_invoke_gemini` are
   `async` but called `load_prompt_file()` inline, so one stalled `_git_dir()`
   blocked the loop that serves requests, responses and liveness notifications.

A new request message completes the pending read, which can release a stalled
git; a subsequent `_git_dir()` can block the loop again before earlier responses
are written, as in the pre-fix MCP probe below. EOF ended the observed stall —
consistent with the 19:09 responses arriving together after the drop.

### Measurements (2026-10-06, the affected machine)

Probe: a child process whose daemon thread parks in `sys.stdin.buffer.read(1)`
on a pipe the parent never writes; a Node host, standing in for Claude Code,
closes it at +15 s for the git probes and +25 s for gemini. `stdin=DEVNULL` was
measured first, while the read was still pending.

| child under test | inherited stdin | `stdin=DEVNULL` |
|---|---|---|
| `_git_dir()` pre-fix, mingw64 git | `None` after 5.02 s | — |
| `_git_dir()` pre-fix, `Git\cmd` launcher | `None`, only once the host closed stdin | — |
| `git rev-parse --absolute-git-dir`, mingw64 | timeout, 5.02 s | 0.03 s |
| same via the `Git\cmd` launcher | stuck until the host closed stdin | 0.05 s |
| `python -c "print(1)"` (base interpreter) | timeout, 5.02 s | 0.05 s |
| `gemini --version` (0.62.0, npm `.CMD` shim) | stuck until the host closed stdin | 2.02 s |
| `codex --version` (0.159.3) | 0.27 s | 0.23 s |
| `node -e "console.log(1)"` | 0.14 s | 0.14 s |

The hazard is not git-specific; it depends on what each child's startup does
with the inherited handle (a fast exit does not prove the handle went untouched).

MCP level — a fresh server driven over a raw JSON-RPC pipe, launcher first on
PATH, scratch linked worktree, codex then gemini 3 s apart, both with
`prompt_file` in the worktree's git dir:

- **Pre-fix** (`05fac5c` source): no response from +6 s (codex sent) until stdin
  was closed at +45 s; gemini then returned `prompt_file_error` (`outside allowed
  roots`). The server log has the gemini request dispatched the moment its
  message arrived, consistent with that message releasing codex's git before
  gemini's own `_git_dir()` blocked the loop. codex had passed the check — its
  `codex.call.success` was logged after EOF — but no codex response reached the
  client; when its backend started was not measured.
- **Post-fix**: gemini succeeded in 10.1 s and codex in 13.6 s, concurrently; both
  carry `prompt_file_receipt` (sha256 `984a2a4f…`, 32 B, matching `sha256sum`) and
  echo the evidence token.

## Fix

| file | change |
|---|---|
| `src/prism_router/prompt_file.py` `_git_dir()` | `stdin=subprocess.DEVNULL` |
| `src/prism_router/server.py` `smoke_probe()` | `stdin=subprocess.DEVNULL` (see audit) |
| `src/prism_router/server.py` `_invoke_codex` / `_invoke_gemini` | `load_prompt_file()` runs via `asyncio.to_thread` |

The `prompt_file` boundary is unchanged: `cd` or its git dir.

### Spawn-site audit

| site | stdin before this fix | verdict |
|---|---|---|
| `prompt_file._git_dir()` | inherited | the bug: runs mid-session with the read pending |
| `server.smoke_probe()` | inherited | latent: `main()` runs `build_server()` before the transport starts reading; under a parked read, `gemini --version` blocks (table above) |
| `dispatcher._default_worker_spawn()` | `PIPE` | unaffected |
| `runner._default_spawn()` (inside the worker) | `PIPE` (`run()` spawn kwargs) | unaffected; the worker has finished reading its own stdin before it spawns |

Both helpers capture stdout/stderr and keep the default `close_fds=True`, so
CPython's Windows handle list for these spawns holds only the three configured
std handles — with DEVNULL, none of them a server pipe.

### Why `asyncio.to_thread`

- `load_prompt_file()` does blocking filesystem reads and hashing, and may spawn
  git with the launcher timeout gap (cause 2). DEVNULL removes the known
  trigger; the offload keeps the event loop responsive while a load waits.
- `asyncio.to_thread` — already the idiom for blocking filesystem probes in
  `dispatcher.py` — runs on the loop's default executor, apart from the anyio
  worker threads (40-token default limiter) behind the transport's own
  `readline`/`write`. Cancelling the awaiting task leaves an already-running
  load in the background; under anyio cancel scopes `anyio.to_thread.run_sync`
  shields the wait by default and matches this only with
  `abandon_on_cancel=True`.
- The whole function moves, not just `_git_dir()`: `prompt_file.py` stays
  synchronous and its contract unchanged.

### Verification

- `tests/test_stdin_isolation.py`: a platform-independent contract test
  (`stdin=DEVNULL`, output captured) for both helpers, plus a Windows-only
  reproduction in which `_git_dir()` and `smoke_probe("git")` run in a child
  whose thread parks in a stdin read; the correct result is required in < 2.5 s
  and the child must exit within 20 s. The parent keeps stdin open until then,
  so no EOF can rescue a stall; that the read was already pending when the call
  began rests on a 0.5 s settle. The reproduction was red on the pre-fix code
  with either git first on PATH and green after.
- `tests/test_prompt_file.py::test_slow_load_does_not_block_event_loop[codex|gemini]`:
  while a stalled load sits in its thread, the loop must keep running. Red before
  the offload, green after.
- Removing either DEVNULL fails that helper's contract and reproduction tests;
  reverting either offload fails that backend's offload test.
- `pytest -m ""`: 538 passed, 2 pre-existing platform skips.
- MCP level: see Measurements.

## Residual risk

- `_git_dir()` can still outlive its timeout behind the `Git\cmd` launcher. The
  offload keeps that wait off the event loop, but the thread survives request
  cancellation, and Python 3.11's `shutdown_default_executor()` has no timeout,
  so a hung load also holds up server exit — as the inline call did before.
  Loads run ahead of the dispatcher's semaphore and overall watchdog, so
  repeated hangs could exhaust the default executor that the dispatcher's
  rollout probes share. A process-tree bound is deferred: no stall was observed
  with stdin detached.
- `smoke_probe()` has the same timeout gap at startup if the `.CMD` shim's node
  descendant survives `cmd.exe` and keeps the captured pipes.

## References

1. Raymond Chen, "All I/O on a synchronous file handle is serialized; that's why
   it's called a synchronous file handle", The Old New Thing, 2013-08-22,
   https://devblogs.microsoft.com/oldnewthing/20130822-00/?p=3433
2. CPython gh-87512 (bpo-43346), "[subprocess] run() sometimes ignores timeout in
   Windows", open, https://github.com/python/cpython/issues/87512
