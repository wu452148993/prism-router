# BUG: codex re-reads bundled files via shell, wastes context budget

> Standalone brief for a fresh session. Self-contained — no prior context needed.

## 1. Symptom

During code-review tasks delegated through `mcp__prism__codex`, codex
spends a large fraction of its turn budget shelling out to PowerShell
`Get-Content` to read files whose content was already supplied in the
PROMPT. Repeated reads of the same files saturate codex's context window
and trigger its own self-compaction, which can drop the actual task
instructions before the final response is produced.

User-visible effect: long codex turns dominated by `shell` tool calls,
short or truncated final review, occasionally complete loss of the
"review the diff" intent.

## 2. Evidence (the call we tracked)

ACF `/implement` review session, single tool-call breakdown:

| Metric                                  | Observed |
| --------------------------------------- | -------- |
| `powershell.exe Get-Content` invocations | **125**  |
| Distinct files re-read                  | 11       |
| Single file (`view.ts`) re-read         | **24×**  |
| `update_plan` calls in a review task    | 11       |

`Get-Content` on Windows costs ~200–400 ms per invocation (PowerShell
cold-start + module load), so the 125 reads alone burned ~30 s of pure
process-spawn overhead before any review work.

## 3. Root cause (two layers)

### A1 — orchestrator wrote shell-substitution syntax as a literal string

The orchestrator (Claude Code) constructed the PROMPT for codex as:

```
Files attached:
$(cat src/foo.ts; cat .tmp/task.diff)
```

PROMPT is a JSON string field in the MCP request. No shell ever expands
`$(...)` here, so codex received the literal seven characters `$(cat ...)`
plus filenames — not file content.

### A2 — codex self-rescued by reading from disk

Faced with a PROMPT that *names* files but doesn't contain them, codex
correctly inferred it needed the contents and reached for its `shell`
tool. Because each tool-call result flows back into the model's context,
re-reading the same file later (codex couldn't remember which calls had
landed) inflated context with duplicate file bodies, eventually
triggering codex's own self-compaction.

A1 is the trigger; A2 is a reasonable LLM behavior given a broken input.
Fixing A1 alone removes the trigger, but A2 reinforces the symptom
whenever any consumer drops bundle content into PROMPT — so the fix
addresses both.

## 4. Fix — MCP Bundle Protocol (`src/prism_router/protocol.py`)

Two prompt-layer protocols, both behavioral nudges (prism-router cannot
enforce tool choices on a spawned LLM):

- **ORCHESTRATOR_SIDE** — published via FastMCP `instructions=`. Tells the
  caller: do not write shell-substitution syntax; structure file content
  under `## Existing files / ## Diff / ## New files` headers with
  `### <path/to/file>` subheaders; PROMPT ≤ 200 KB.

- **AGENT_SIDE** — `inject_agent_side()` prepends to PROMPT before it
  reaches the worker (and therefore codex stdin). Tells the spawned
  agent: bundle-headered content under `### <path>` is authoritative;
  re-reading wastes context budget; spend tool calls verifying files
  only when the task explicitly requires post-edit filesystem state.

Injection point: `src/prism_router/server.py` — `_invoke_codex` and
`_invoke_gemini`, immediately before `Dispatcher().invoke(...)`. This
keeps the 200 KB diagnostic warning in the main MCP process log
(`prism_router` logger) where an operator investigating codex
compaction can correlate it, and leaves `dispatcher.py` / `worker.py` /
`runner.py` transport-pure.

Both layers are non-removable nudges: a consumer that ignores the
protocol gets identical behavior to today (the bundle-headers check on
the agent side falls through to "ordinary prose"). The fix is strict
addition, no behavior change for callers that don't opt in.

## 5. Verification

### 5.1 Unit-test coverage

- `tests/test_protocol.py` — pure-function tests on the constants and
  `inject_agent_side`; covers oversize-warning byte-vs-codepoint
  semantics.
- `tests/test_server.py` — server-level wiring: instructions includes
  both preamble and `ORCHESTRATOR_SIDE`; `_invoke_codex` / `_invoke_gemini`
  prepend `AGENT_SIDE` to the prompt seen by Dispatcher;
  oversize warning fires above threshold and stays silent below.

Behavioral verification (re-read suppression) is **not** in CI. LLM
non-determinism makes any threshold arbitrary; a flaky canary degrades
into "always ignored" within a few false positives. Manual procedure
below.

### 5.2 Manual canary

```bash
python scripts/manual_verify_bundle_protocol.py
```

The script:

1. Reads `tests/fixtures/bundle-prompt.md` (multi-file unified diff
   under `## Diff`, with two `### <path>` blocks under `## Existing
   files`).
2. Calls `inject_agent_side` the same way `server.py` does.
3. Spawns codex via `Dispatcher().invoke(...)`.
4. Scans `outcome.parse_result.all_messages` for shell-read tool calls
   matching `get-content` / `read_file` / `cat src/calc` / `type src/calc`.
5. Prints the count + sample tool-call payloads.
6. Exits non-zero if shell reads exceed **20** (baseline pre-fix was 125).

Run it after any of these changes:

- AGENT_SIDE / ORCHESTRATOR_SIDE text edits
- bundle-header naming changes (`## Existing files` etc.)
- codex CLI version bump (tool-call schema may shift)

Expected output post-fix: shell reads in the 0–5 range, no single file
read more than twice.

## 6. Out of scope for this fix

- **Tool-preference guidance** (e.g. "prefer codex's native read over
  `Get-Content` on Windows"). This is consumer-repo policy, not
  router-layer policy — prism-router does not know which read tools a
  given codex install actually exposes. Document such preferences in the
  consumer repo's `AGENTS.md` (codex CLI auto-loads it from `cwd`).

- **Enforcement** of the 200 KB PROMPT limit. The current
  implementation logs a WARNING and proceeds — truncation or rejection
  would break existing large-PROMPT callers (notably ACF). The 4 MB
  dispatcher pre-flight cap (`STDIN_PREFLIGHT_CAP`) remains the only
  hard upper bound.

- **Bundle-header validation.** A consumer that writes `## Diff` without
  any `### <path>` subheader gets "protocol inactive — ordinary prose"
  per AGENT_SIDE wording. We do not parse / validate the bundle on the
  way through.

- **Bundle completeness / freshness.** AGENT_SIDE tells codex to trust
  bundled content over disk, so a *truncated* or *stale* bundle would be
  reviewed as-is — an accuracy risk that lives entirely at the
  orchestrator, not here. The router never truncates: `warn_if_oversized`
  only logs, and `STDIN_PREFLIGHT_CAP` (`dispatcher.py`) rejects the whole
  request at 4 MB rather than slicing it — codex either gets the full
  bundle or no spawn at all. For the ACF `/implement` path the upstream
  side is also covered: `commands/implement.md` §2a.1/§2c.1 forbids
  excerpting files (whole-file or nothing) and STOPs on oversize
  (>200 KB bundle / file, >50 KB non-fixture new file) instead of
  trimming — so no partial-but-authoritative bundle reaches codex. This
  guarantee does not extend to non-ACF callers; the no-excerpt rule is a
  prompt-level orchestrator policy the router cannot enforce. Residual
  (non-truncation) risks: same-turn concurrent edits after assembly
  (staleness), and codex under-reading an unbundled dependency it should
  have pulled in per AGENT_SIDE's "read them normally if needed".
