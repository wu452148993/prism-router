# Gemini default `-y` handed every review write + shell capability

## Symptom

Three recorded incidents where a gemini call whose PROMPT said "read-only,
return a unified diff" mutated the repository it was reviewing:

| Date | Repo | Action |
|---|---|---|
| 2026-06-14 | agent-control-framework | edited `run-exec.mjs` + `run.md` in place ("I have applied…") |
| 2026-07-15 | godot/shushu-idle | `git stash -u` moved 110 lines of untracked in-progress work out of the tree |
| 2026-08-11 | agent-control-framework | `git checkout 139e99a` succeeded — repo left in detached HEAD |

The 2026-08-11 log is unambiguous (`~/.gemini/tmp/agent-control-framework/
chats/session-2026-08-11T13-43-4f53bd1b.jsonl`): `run_shell_command` →
`"HEAD is now at 139e99a"`. Prompt-level "READ-ONLY" instructions do not bind
the agent; the caller only finds out via `git status` afterwards.

## Root cause

`-y/--yolo` selects gemini's YOLO approval mode (`approvalMode = "yolo"` in
the installed bundle) — and headless that mode also decides the **tool
declaration set**, which is the part that matters here: the flag does not
merely pre-answer prompts, it changes what the model is offered. Measured on
gemini-cli 0.57.0, headless
`-o stream-json`, prompt on stdin, asking the model to enumerate its own
declarations:

- **without `-y`** (8 tools, all read-only): `update_topic`, `list_directory`,
  `read_file`, `grep_search`, `glob`, `google_web_search`, `enter_plan_mode`,
  `invoke_agent`
- **with `-y`** (17): the above plus `replace`, `write_file`, `web_fetch`,
  `run_shell_command`, `list_background_processes`, `read_background_output`,
  MCP tools, `activate_skill`

A behavioural probe confirms the mechanism rather than mere refusal: asked to
create a file, the non-yolo run answered "`write_file` and
`run_shell_command` are not in my tool declarations", exited `success`, and
left the directory untouched. `security.folderTrust.enabled=false` in the
operator's `~/.gemini/settings.json`, so `-y` was fully in force — with
folderTrust enabled an untrusted workspace would have forced
`approvalMode=DEFAULT` and masked the exposure on that machine only.

**Withholding `-y` is a safer default, not an enforced boundary.** The
declaration set is also reachable from the operator side, which prism neither
sets nor inspects: the `defaultApprovalMode` setting, `--approval-mode
auto_edit|yolo`, and the policy-engine files behind `--policy` /
`--admin-policy` (all present in gemini-cli 0.57.0 `--help` and the installed
bundle's settings schema). The measurement above holds for a stock config; it
does not make a mutating review impossible.

`ServerConfig.gemini_require_approval` defaulted to `False`, so
`server.py` computed `yolo = yolo or not require_approval` → always `True`,
and `backend_gemini` appended `-y` to every call.

## Decision — default the reverse flag to true

`GEMINI_REQUIRE_APPROVAL` now defaults to `True`, so the default argv carries
no `-y`. Nothing else moves: the `yolo` tool parameter still wins when a
caller explicitly asks for write capability, and an operator can restore the
old behaviour with `GEMINI_REQUIRE_APPROVAL=false` in the MCP server env —
no code change, no redeploy of the six consuming `.mcp.json` files.

Cost, measured before landing rather than assumed. 872 real
`mcp__prism__gemini` calls in Claude Code transcripts (2026-07-15 →
2026-09-03); 187 distinct SESSION_IDs were recoverable from the captured
envelopes, and 121 of those still had gemini-side session logs on disk
(gemini prunes after 30 days). The tool-usage figures below therefore
describe 121 sessions, biased toward recent ones — they characterize the
disruption, they do not bound or enumerate it. The two call-level figures
cover all 872:

- `yolo=true` passed explicitly: **0 of 872**; `sandbox=true`: **0 of 872**.
  No caller had ever declared a need for write or shell capability, so none
  breaks at the API level and anyone who needs one must now add a visible
  parameter. That is not the same as losing nothing: a caller that leaned on
  the implicit `-y` does lose the capability, and — per the paragraph below —
  can be told so only in prose.
- `write_file` / `replace`: **0 of 121**
- `web_fetch`, `activate_skill`, MCP tools: **0 of 121**
- `run_shell_command`: **6 of 121 (5 %)** — of which 2 used the shell purely
  as a read substitute (`cat`/`grep`/`sed`, covered by `read_file` /
  `grep_search`), 1 was the destructive `git checkout` above, and 3 (one ACF
  session cluster on 2026-08-03) genuinely executed `node …test.mjs`

Within this sample only that last class regresses. It degrades legibly but
not structurally:
gemini names the missing tool and the text lands in `agent_messages`
(measured: `Tool "run_shell_command" not found`), yet the envelope still
carries `success:true` with `error:null`. A consumer that branches on the
typed `success` field alone cannot tell a completed task from one whose
command never ran — it has to read the prose. Surfacing this structurally
(e.g. a diagnostics counter) was considered and deferred: the wrapper would
have to pattern-match model prose, and the same blind spot already applies to
any tool the agent declines to use.

## Implementation

- `config.py` — `_bool_env(env, "GEMINI_REQUIRE_APPROVAL", True)`
- `server.py` — gemini tool docstring documents the read-only default and
  when to pass `yolo=true` (the MCP tool description is the only channel a
  calling agent reads before choosing parameters)
- `tests/test_config.py` — default assertion
- `tests/test_server.py` — `test_gemini_default_env_emits_no_yolo_flag`
  drives the registered MCP tool through `build_server` + `Client`, so the
  closure capture in the tool body is covered; the pre-existing matrix hits
  `_invoke_gemini` directly and `test_argv` only SpawnConfig → argv, leaving
  that hop untested
- `tests/integration/test_smoke.py` — the real-binary gemini smoke now runs
  `yolo=False`, i.e. the configuration that actually ships

## Limitations

- Codex is deliberately untouched. Its `--sandbox read-only` blocks *every*
  `CreateProcess` on Windows (workspace-write too), so there is no
  read-only-with-tools tier to move it to, and no comparable incident is on
  record. See the sandbox investigation notes before revisiting.
- prism's `yolo` argument is per invocation, not per conversation: resuming a
  SESSION_ID first started with `yolo=true` does not re-send the flag.
  Whether the tools come back is then decided by the same operator-side
  surfaces as any other call, not by the earlier turn.
- Read reach is unchanged by this patch, and it is not "anything the account
  can open": gemini's file tools go through `validatePathAccess` (72 call
  sites in the 0.57.0 bundle) against the configured workspace / allowed
  paths, independently of `sandbox`. What the patch removes is mutation from
  the default declaration set, nothing about read.
- The declaration-set measurement is from gemini-cli 0.57.0. `docs/README.md`
  advertises support from v0.36+, and no evidence was gathered for older
  releases; the smoke test checks that a non-yolo call answers, not what it
  declares.
