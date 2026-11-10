# Gemini at-command scan hang — escape every at-sign on the wire

## Symptom

Before this fix, `mcp__prism__gemini` stalled indefinitely on payloads carrying
ordinary quoted text with at-signs in it — package.json-shaped diffs, dependency
tables, review packs. The backend stayed alive but did no work: stdout stopped at
exactly 140 B (the `init` frame) and went silent, one CPU core stayed pegged, and
the request was never sent upstream. Nothing bounded the call except the 7200 s
`overall_timeout`, so a single delegation burned the whole budget and returned no
answer.

Observed on gemini-cli 0.59.0 / node v22.22.2 / Windows, through a bare pipe
(`printf … | cat - FILE | gemini -o stream-json`) as well as through prism — so
the wrapper was never implicated.

## Root cause (NOT a prism bug)

Verified against the installed bundle,
`@google/gemini-cli` 0.59.0, `bundle/chunk-CUTHWG3H.js`
(transpiled from `packages/cli/src/ui/hooks/atCommandProcessor.ts`):

```js
66618: function escapeAtSymbols(text) {
66619:   return text.replace(/(?<!\\)@/g, "\\@");
66620: }
...
66630: var AT_COMMAND_PATH_REGEX_SOURCE = '(?:(?:"(?:[^"]*)")|(?:\\\\.|[^ \\t\\n\\r,;!?()\\[\\]{}.]|\\.(?!$|[ \\t\\n\\r])))+';
66631: function parseAllAtCommands(query, escapePastedAtSymbols = false) {
66634:   const atCommandRegex = new RegExp(
66635:     `(?<!\\\\)@${AT_COMMAND_PATH_REGEX_SOURCE}`,
66636:     "g"
66637:   );
66639:   while ((match = atCommandRegex.exec(query)) !== null) {
```

The regex is assembled at runtime, not written as a literal; after JS string
unescaping its source is
`(?<!\\)@(?:(?:"(?:[^"]*)")|(?:\\.|[^ \t\n\r,;!?()\[\]{}.]|\.(?!$|[ \t\n\r])))+`.

It scans the WHOLE prompt. The first alternative `"(?:[^"]*)"` runs to the next
`"` — **across lines** — and the outer `+` then keeps going. In
`"@scope/pkg": "workspace:*",` one at-sign swallows thousands of characters over
a dozen lines and yields one pseudo-path, which enters `resolveAtCommandPath` →
`validatePath` (fails) → `tryExtractPath`, which re-splits on whitespace and
re-strips, re-matches and re-validates every segment, recursively — on top of the
two recursive workspace globs per at-path that upstream describes in PR #28581.
The cost is paid before any network call. No growth bound was established; what
was measured is the saturation, not its order.

### Measurements

Taken during the 2026-09-15 investigation session, on the same CLI version. They
are the basis for the fix; they were **not** re-run against the committed code
(what was re-run is in [Verification](#verification) below).

| Observation | Result |
|---|---|
| node CPU over 12 wall-clock seconds | 2.78s → 17.70s (+14.9 CPU-s, >1 core saturated) |
| stdout | exactly 140 B = `init` frame, then silence |
| `icon-system-crosscheck-pack.md`, 255,765 B, verbatim | HANG |
| same file, every `@` → `A` (byte count unchanged) | PASS |
| same file, every `@` → `\@` | PASS |
| same file, only the single cross-line escape point escaped | HANG — escaping that one site was not sufficient |
| **8,168 B** payload holding one runaway region | **HANG** |
| 255,719 B of uniform ASCII filler, zero at-signs | PASS |
| 17-file empty repo as the workspace | HANG |
| the regex alone over an 87 KB payload | 1 ms / 38 matches — **not backtracking, not ReDoS** |

`g-half1.txt` (87,258 B; 45 at-paths totalling 677 chars) PASSES;
`g-half2.txt` (87,140 B; 38 at-paths totalling 8,366 chars, longest 8,112) HANGS.

Two negative conclusions are solid and both matter for the design: **payload size
does not explain the outcomes** (8,168 B reproduces, 255,719 B of at-free filler
does not), and **the regex is not the expensive part** (1 ms over 87 KB). Match
length alone does not explain them either — see [Residual risk](#residual-risk).

Upstream was inspected on 2026-09-15 and was stalled then; this has not been
re-checked. Issue **#28550** (*"runs that did not quite tip over instead consumed
the full 1 hour job timeout producing no output at all"*) was closed
`not_planned` / `status/need-information` on 2026-08-22, and PR **#28581**
(`fix(cli): skip diff hunk markers during @ processing`, `Fixes #28550`) was
closed 2026-08-13 with `merged_at: null` — never merged.

## Fix

`at_escape.escape_at` replaces every `@` with `\@` in the gemini stdin payload.
The assembled regex is guarded by `(?<!\\)`, so `exec` returns null on the first
call: the scanner still runs, but there are no matches and therefore no path
resolution. This is the same transformation upstream applies in its own
`escapeAtSymbols` (line 66618 above) — prism applies it unconditionally on the
wire rather than at a UI paste boundary.

- **gemini only.** `_invoke_codex` is untouched; the codex payload stays
  byte-identical (regression-asserted).
- **Applied once**, in `_invoke_gemini`: after the `prompt_file` merge so the whole
  payload is covered, before `inject_agent_side`, and outside the transient-retry
  loop — that loop re-dispatches the same `prompt` string, and a second pass would
  ship `\\@`.
- **Unconditional, never size-gated.** 8 KB reproduces, so a threshold would only
  add a silent failure mode.
- **`AT_ESCAPE_NOTE`** is prepended when at least one at-sign was escaped, asking
  the model to read `\@` as a plain at-sign. It is prepended AFTER escaping and so
  travels verbatim — it must therefore contain no literal at-sign, or it becomes a
  fresh match start. `ui.escapePastedAtSymbols` cannot do this job: the
  non-interactive path (`gemini-CQYKWDIK.js:11216` and `:11722`) passes a hardcoded
  `escapePastedAtSymbols: false` and never reads settings, whereas the interactive
  path reads `settings.merged.ui`. That parameter is also what gates
  `unescapeLiteralAt` on the text parts (`chunk-CUTHWG3H.js:66645`), so under
  headless the backslashes reach the model verbatim by construction, not by
  accident.
- **Knob** `PRISM_GEMINI_AT_ESCAPE=0`, read into `ServerConfig.gemini_at_escape`
  at server construction, restores the unescaped payload byte-for-byte.
- **Escape count goes to the log, not the envelope.** `_envelope`'s `has_extra`
  gate admits only `recovered_from_rollout` / `internal_retries`; a new diagnostics
  key would either never appear on a normal success — invisible exactly when
  someone is asking why their `@path` stopped working — or force that gate wider,
  giving every at-bearing gemini call a diagnostics block it does not have today.

### Verification

Run against the committed revision, through a freshly spawned prism MCP server
(the editing session's own server holds pre-change code in memory):

- `g-half2.txt` (87,140 B, previously hung every time) — success in 17.1 s, 97
  at-signs escaped.
- `g-half1.txt` (87,258 B) — still succeeds, 18.4 s.
- `icon-system-crosscheck-pack.md` (255,765 B) — success in 208.3 s,
  `truncated:false`, first heading verbatim and the deduplicated `### ` count
  correct at 94. `prompt_file_receipt` still `a2ecb6c7…` / 255,765 B: the escape
  runs in memory after `load_prompt_file` has hashed the raw bytes, so the
  receipt's coverage domain does not move.
- `pytest -m ""`: 532 passed, 2 pre-existing platform skips.

Each acceptance payload was run once; these are existence proofs that the fix
clears the previously-hanging inputs, not a repetition study.

### Rejected alternative: parse-aware selective escaping

To preserve `@path` expansion for callers, escaping only the at-signs whose token
does not resolve to a real file under `cd` was evaluated and **measured unsafe**.
What decides the hang is how far the upstream regex actually runs, not whether the
token we sliced out exists. An 8,191 B probe built around `@marker.txt": "…"` —
`marker.txt` real, therefore passed through by that rule, every other at-sign
escaped — **hung** (100 s timeout, stdout 140 B). One missed at-sign reinstates the
bug. Doing it safely would mean re-implementing the upstream regex wrapper-side and
validating per match, which couples prism to a bundled regex that can change
silently. The cost stopped being incidental.

## Semantics / edge cases

- **The model sees `\@`.** The note asks for the intended reading. One probe
  supports that it works: asked to list every scoped dependency in an escaped
  package.json diff, the model returned all 6 expected names with no extras —
  including 2 a grep baseline had missed. That is one observation, not a general
  guarantee of semantic fidelity.
- **`@path` expansion is disabled for prism callers by default.** It was never part
  of the contract: `ORCHESTRATOR_SIDE` documents exactly two ways to supply file
  content — inline under bundle headers (§2) or `prompt_file` (§5). What is lost is
  a shortcut, not a capability: `read_file` / `glob` / `grep_search` /
  `list_directory` stay declared even without `-y`
  ([gemini-default-write-capability.md](gemini-default-write-capability.md)), so a
  caller who wants the model to open a file can just ask for it in PROMPT.
- **Already-escaped input is escaped again** (`\@` → `\\@`). A deliberate policy
  choice, not an oversight: the lookbehind inspects only the character immediately
  before the at-sign, so both forms suppress the match, and making the helper
  idempotent would add a branch whose only beneficiary is a payload that already
  carries escapes.
- `AGENT_SIDE` / `ORCHESTRATOR_SIDE` contain zero at-signs, so the injected protocol
  text needs no special handling.
- **Payload growth vs. `STDIN_PREFLIGHT_CAP`.** With the escape active and `n > 0`,
  stdin grows by `n + 165` UTF-8 bytes and the dispatcher's JSON request by
  `2n + 167` (measured) — each inserted backslash is doubled again by JSON. Zero
  growth when nothing is escaped. The 1 MiB `prompt_file` cap does not bound this:
  inline PROMPT is uncapped, so a merged request can cross the 4 MiB cap and fail
  `request_too_large`. That is a fail-closed pre-flight error, not a hang.

## Residual risk

- **The trigger boundary is not fully mapped.** In the measurements above, an
  at-sign was necessary — every payload that hung passed once escaped or
  substituted — but the cost model is incomplete: a synthesized 16,176 B payload
  producing a *larger* cross-line runaway match (16,082 chars) **passed**, while
  g-half2's 8,112-char match hangs. Match length is therefore not the discriminant
  either, and other hang shapes whose cause is not at-signs cannot be ruled out.
  This fix should not be assumed to cover them.
- **The gemini arm has never had a stdout-silence watchdog.** `pre_terminal_idle`
  has been codex-gated since the commit that introduced it (`e1b8247`,
  `dispatcher.py:464-466`), and in any case only arms at the `first_codex_output`
  milestone, whereas this hang begins right after the `init` frame. This change
  neither creates nor narrows that gap — it is listed here because the preceding
  point makes closing it more urgent, not less. `overall_timeout` remains the only
  ceiling on a gemini arm that has stopped producing output. A backstop that does
  not depend on recognizing the trigger is tracked separately.
- **No upstream report filed.** Every payload that reliably reproduces carries
  private repository content, and synthetic reproduction has not succeeded (see the
  16 KB counterexample above). Given #28550 was closed for `need-information`,
  filing without a minimal public reproducer would likely repeat that outcome.
