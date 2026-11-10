# Per-backend C7 smoke-probe timeout

Date: 2026-06-17
Status: shipped
Gemini CLI versions affected: confirmed at 0.46.0 (Node CLI via nvm4w shim).

## Symptom

`mcp__prism__gemini` intermittently does not appear in Claude Code's tool list —
"times out / won't load" — especially when the machine is under load. codex
(`mcp__prism__codex`) stays available. The prism server itself is otherwise
healthy; only the gemini tool is missing, and it stays missing for the whole
MCP-server lifetime (until restart).

## Root cause

C7 conditional registration (`server.py`) gates each tool on a startup
`smoke_probe`, which execs `<cmd> --version` with a 5.0s timeout. On
`subprocess.TimeoutExpired` the probe returns False and the `@mcp.tool`
registration is skipped (only a stderr `_LOG.warning`).

`gemini` is a Node CLI launched through a shell-script shim
(`<nvm4w>/gemini` → node → CLI bootstrap). Its `--version` is structurally
slow; under load its cold start exceeds the shared 5s budget, the probe times
out, and gemini is silently and permanently dropped. `codex` is a Rust native
binary and has huge headroom, which is why the failure is gemini-specific.

The probe is **one-shot** and **blocks** `mcp.run()` (it runs before the MCP
`initialize` handshake), so a single startup lag spike disables gemini until
restart.

### Measured `--version` latency (target machine, idle)

| command | latency | share of old 5s budget |
|---|---|---|
| `codex --version` | 0.47s | 9% (Rust binary) |
| `gemini --version` | 3.08s | 62% (Node CLI via shim) |

Idle already consumes 62%; under load the Node cold start blows the budget.

## Claude Code MCP startup budgets (official docs)

Source: docs.claude.com/en/docs/claude-code/env-vars.

| env | default | governs |
|---|---|---|
| `MCP_TIMEOUT` | 30000ms (30s) | per-server startup/connect ceiling |
| `MCP_CONNECT_TIMEOUT_MS` | 5000ms | blocking-startup window before the tool-list snapshot; applies to `alwaysLoad: true` servers (prism is one); late servers keep connecting in the background and appear on the next query |
| `MCP_SERVER_CONNECTION_BATCH_SIZE` | 3 | parallel stdio server connects at startup |

Implications:
- A wider gemini probe (12s) is well under the 30s `MCP_TIMEOUT` ceiling, so it
  does **not** risk killing the whole prism server (both tools). Confirmed via
  codex review (2 rounds).
- prism already overruns the 5s `MCP_CONNECT_TIMEOUT_MS` snapshot window today
  (gemini 3s probe + `uv run` startup + codex probe), so it is already deferred
  to a later query under load — unchanged by this fix.

## Decision

Decouple the probe budget per backend, keep C7 (do not drop the `--version`
exec — that would change the contract so a present-but-broken gemini registers
and fails at call time instead of via native "no such tool").

- `PRISM_PROBE_TIMEOUT_CODEX` default 5.0 (codex is fast; keep it tight).
- `PRISM_PROBE_TIMEOUT_GEMINI` default 12.0 (~4x the idle Node baseline; leaves
  margin under the 30s ceiling — codex round 2 picked 12 over 15).
- Probes run **concurrently** so the wider gemini budget does not serialize
  behind codex and stack toward the 30s ceiling: worst case `max(5, 12) + uv`,
  not `5 + 12 + uv`.
- Malformed / non-finite / non-positive knob values fall back to the default
  with a warning — never crash startup nor yield an instant-timeout probe.

This does not eliminate the one-shot/permanent-disable class (a spike beyond
12s still drops gemini until restart); it widens the window to the point the
class is rare under normal load. A runtime re-probe via
`notifications/tools/list_changed` was rejected as out of scope — it contradicts
C7's init-time-fixed registration.

## Implementation

Files touched in the same commit (project rule "bug-doc and fix code in the
same commit"):

| File | Change |
|---|---|
| `src/prism_router/config.py` | `_float_env()` guarded parser; `ServerConfig` gains `codex_probe_timeout` / `gemini_probe_timeout` (env `PRISM_PROBE_TIMEOUT_CODEX` 5.0 / `PRISM_PROBE_TIMEOUT_GEMINI` 12.0). |
| `src/prism_router/server.py` | Removed module constant `_PROBE_TIMEOUT`; `build_server` runs both probes concurrently via `ThreadPoolExecutor` and passes per-backend `timeout_sec`. |
| `tests/test_config.py` | Probe-timeout defaults, overrides, malformed/non-finite/non-positive fallback. |
| `tests/test_server.py` | Asserts codex vs gemini receive distinct probe timeouts (default + override). |

## Related (separate from this fix)

`.mcp.json` still carries the legacy GuDaStudio `codex` + `gemini` stdio entries
alongside `prism`, all `alwaysLoad: true` — three of them fill the entire
`MCP_SERVER_CONNECTION_BATCH_SIZE=3` startup batch and the legacy `uvx --from
git+...` entries add their own spawn cost. Per `docs/implementation/m4-cutover.md`
§2, those two legacy entries should be deleted now that prism supersedes them.
That is an operator `.mcp.json` edit (the file holds live tokens and is
gitignored), not part of this code change.

## How to tune on a slow machine

```bash
export PRISM_PROBE_TIMEOUT_GEMINI=20   # bash
$Env:PRISM_PROBE_TIMEOUT_GEMINI = "20" # PowerShell
```
