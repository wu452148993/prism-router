#!/usr/bin/env python3
"""Manual canary for the MCP Bundle Protocol.

NOT in CI: LLM behavior is non-deterministic and any threshold is arbitrary.
Run after AGENT_SIDE / ORCHESTRATOR_SIDE text changes (or codex CLI version
bumps); compare against the baseline numbers in
docs/bugs/codex-prompt-bundle-protocol.md.

Usage:
    python scripts/manual_verify_bundle_protocol.py

Behavior: spawns codex with the multi-file bundle fixture, prepends AGENT_SIDE
the same way server.py does, then scans the all_messages stream for shell
file-read tool calls. Exits non-zero on suspected regression.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from prism_router.auth import preflight_codex_auth
from prism_router.backend import SpawnConfig
from prism_router.dispatcher import Dispatcher
from prism_router.protocol import inject_agent_side

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "bundle-prompt.md"

# Heuristic markers for "codex reached for a shell-style file read". Imperfect:
# LLM tool-call schemas vary by codex version. The script is a canary, not a
# contract; tune the needle list when codex changes its tool naming.
SHELL_READ_NEEDLES = (
    "get-content", "read_file", "cat src/calc", "type src/calc",
)

# Item types that legitimately carry file paths in their content (MCP query
# fields, agent reasoning prose, final answer text) but do NOT represent a
# shell-style file read. Excluded from needle scanning to suppress false
# positives like an ace-tool search_context query containing "src/calc".
NON_SHELL_ITEM_TYPES = frozenset({
    "mcp_tool_call", "agent_message", "reasoning",
})

# Baseline pre-fix was ~125 reads / session, single file x24. A working
# protocol drops this to single digits. 20 leaves headroom for one-off probes.
REGRESSION_THRESHOLD = 20


def count_shell_reads(messages: list) -> tuple[int, list[str]]:
    hits: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        item = m.get("item")
        if isinstance(item, dict) and item.get("type") in NON_SHELL_ITEM_TYPES:
            continue
        blob = json.dumps(m, ensure_ascii=False).lower()
        if any(needle in blob for needle in SHELL_READ_NEEDLES):
            hits.append(blob[:200])
    return len(hits), hits


async def main() -> int:
    err = preflight_codex_auth()
    if err is not None:
        print(f"auth pre-flight failed: {err['code']} {err.get('raw')}", file=sys.stderr)
        return 1

    prompt = inject_agent_side(FIXTURE.read_text(encoding="utf-8"))
    cfg = SpawnConfig(
        cwd=REPO, sandbox="read-only", session_id=None,
        model="", profile="", skip_git_repo_check=True, yolo=True, images=(),
    )

    print(f"invoking codex with bundle ({len(prompt.encode('utf-8'))} bytes)...",
          file=sys.stderr)
    result = await Dispatcher().invoke(backend_name="codex", prompt=prompt, cfg=cfg)
    if result.error is not None:
        print(f"FAIL: {result.error['code']} — {result.error.get('raw')}", file=sys.stderr)
        return 1

    msgs = result.outcome.parse_result.all_messages
    count, samples = count_shell_reads(msgs)

    print(f"messages observed     : {len(msgs)}")
    print(f"shell-read tool calls : {count}")
    print(f"baseline (pre-fix)    : ~125/session, single file x24")
    if samples:
        print("samples (first 3):")
        for s in samples[:3]:
            print(f"  - {s}")
    if count > REGRESSION_THRESHOLD:
        print(f"FAIL: {count} > {REGRESSION_THRESHOLD} — protocol may have regressed",
              file=sys.stderr)
        return 2
    print(f"OK: under threshold ({count} <= {REGRESSION_THRESHOLD})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
