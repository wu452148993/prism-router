"""MCP Bundle Protocol — single source of truth for both sides.

Two-sided behavioral protocol that fixes the codex re-read storm caused by
literal shell-substitution syntax in PROMPT (see docs/bugs/codex-prompt-
bundle-protocol.md):

- ORCHESTRATOR_SIDE — published via FastMCP `instructions=`. Tells the
  caller (claude / other LLM consumer) how to construct PROMPT.
- AGENT_SIDE — prepended to PROMPT before it reaches codex/gemini stdin.
  Tells the spawned agent that bundle-headered content is authoritative
  and redundant re-reading wastes context budget.

Both layers are prompt-level nudges. prism-router cannot enforce tool
choices on the spawned agent; it can only document intent.
"""

from __future__ import annotations

import logging
from pathlib import Path

_LOG = logging.getLogger("prism_router")

OVERSIZED_THRESHOLD_BYTES = 200_000


ORCHESTRATOR_SIDE = """\
When delegating code generation or review to this tool:

1. PROMPT is a string field. Do NOT write shell-substitution syntax
   ($(...), `...`, ${...}) — they will not expand and the agent will
   receive literal text.

2. When sending file content as context, structure it with these
   headers so the agent treats it as authoritative and skips redundant
   reads:
     ## Existing files     (full content of files in scope)
     ## Diff               (git diff for review tasks)
     ## New files          (full content of newly-created files)

   Under each header, label each file with a `### <path/to/file>`
   subheader followed by the content. The agent only recognizes files
   that carry an explicit path subheader.

3. Practical PROMPT size limit: ~200KB. Above this, split the task
   instead of relying on agent-side compaction.

4. SESSION_ID: pass the value from a previous response to continue
   the same conversation in the same backend.

5. prompt_file: to attach a large evidence file (e.g. under the repo's
   .git/acf/ scratch dir), pass its path instead of inlining — the
   wrapper reads it and appends "\\n\\n" + content to PROMPT on stdin,
   so the model needs no filesystem access to it. The file must resolve
   under `cd` or its git dir. Failures return a pre-spawn
   `prompt_file_error` envelope (backend never spawned, no SESSION_ID);
   success adds `prompt_file_receipt` {path, sha256, bytes}.
"""


AGENT_SIDE = """\
[prism-router protocol]

If this PROMPT contains bundle headers (## Existing files / ## Diff /
## New files), treat the content under those headers as authoritative:
`## Existing files` and `## New files` are the current state of the
listed files; `## Diff` is the authoritative diff for the change under
review. The listed file contents are already in your context — re-reading
them yields the same bytes and consumes context budget without adding
information, so answer from the bundle instead.

Do not spend tool calls verifying listed files unless the task
explicitly requires checking post-edit filesystem state.

Bundle payload format: under each bundle header, individual files MUST
appear as `### <path/to/file>` subheaders followed by their content.
A bare bundle header without `### <path>` subheaders does NOT activate
the protocol — treat such text as ordinary prose.

Authority applies to the CURRENT prompt only. If this is a SESSION_ID
continuation, bundle headers from earlier turns are stale — use only
what appears in this message.

Files not listed under bundle headers are unaffected — read them
normally if needed.

If no bundle headers are present, this protocol is inactive.
"""


def inject_agent_side(prompt: str) -> str:
    """Prepend AGENT_SIDE to PROMPT.

    Pure function so the protocol behavior is unit-testable without
    asyncio / subprocess plumbing. The horizontal-rule separator is
    safe: YAML frontmatter requires document-first-line position,
    which AGENT_SIDE occupies; CommonMark treats non-first-line `---`
    as a thematic break.
    """
    return f"{AGENT_SIDE}\n\n---\n\n{prompt}"


def warn_if_oversized(prompt: str, *, backend_name: str, cwd: Path) -> None:
    """Emit a WARNING when the encoded PROMPT exceeds the soft limit.

    Measured in UTF-8 bytes (not code points) so the count matches what
    codex/gemini actually receives on stdin. Diagnostic only — no
    truncation, no rejection. Logged in the main MCP process so an
    operator investigating codex self-compaction can see the trigger.
    """
    stdin_bytes = len(prompt.encode("utf-8"))
    if stdin_bytes > OVERSIZED_THRESHOLD_BYTES:
        _LOG.warning(
            "oversized prompt: %d bytes (backend=%s, cwd=%s)",
            stdin_bytes, backend_name, cwd,
        )
