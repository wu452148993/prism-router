"""Local-only integration smokes against real codex/gemini binaries.

Run with `pytest -m integration` (excluded by default; not in CI).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from prism_router.backend import SpawnConfig
from prism_router.backend_codex import CodexBackend
from prism_router.backend_gemini import GeminiBackend
from prism_router.runner import run

pytestmark = pytest.mark.integration


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


@pytest.mark.skipif(not _have("codex"), reason="codex CLI not on PATH")
async def test_codex_short_prompt(tmp_path: Path):
    cfg = SpawnConfig(
        cwd=tmp_path, sandbox="read-only", session_id=None, model="",
        profile="", skip_git_repo_check=True, yolo=True, images=(),
    )
    outcome = await run(CodexBackend(), "Reply with exactly: LGTM", cfg)
    assert outcome.returncode is not None
    assert "LGTM" in outcome.parse_result.agent_messages


@pytest.mark.skipif(not _have("gemini"), reason="gemini CLI not on PATH")
async def test_gemini_short_prompt(tmp_path: Path):
    # yolo=False mirrors the shipped default, so this is the real-binary path
    # callers actually get. It asserts the call still answers — not what the
    # CLI declares. docs/bugs/gemini-default-write-capability.md
    cfg = SpawnConfig(
        cwd=tmp_path, sandbox="", session_id=None, model="",
        profile="", skip_git_repo_check=False, yolo=False, images=(),
    )
    outcome = await run(GeminiBackend(), "Reply with exactly: LGTM", cfg)
    assert outcome.returncode is not None
    assert "LGTM" in outcome.parse_result.agent_messages


# Overall-timeout coverage moved to dispatcher unit tests
# (tests/test_dispatcher.py::test_dispatch_overall_timeout_kills_returns_timeout)
# since wall-clock watchdogs live in the main process, not in runner.run().
