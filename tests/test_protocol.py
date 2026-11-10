"""Protocol-layer unit tests — pure, no asyncio / subprocess plumbing.

Asserts the constant text carries the markers that downstream consumers
(claude orchestrator, codex agent) need to recognize the protocol, and
that `inject_agent_side` is a side-effect-free prepend.

Server-level wiring (instructions field, prompt prepend, oversize warning)
is covered in tests/test_server.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prism_router.protocol import (
    AGENT_SIDE,
    ORCHESTRATOR_SIDE,
    OVERSIZED_THRESHOLD_BYTES,
    inject_agent_side,
    warn_if_oversized,
)


# ───── constants carry the contract ─────


@pytest.mark.parametrize(
    "marker",
    [
        "[prism-router protocol]",
        "## Existing files",
        "## Diff",
        "## New files",
        "### <path/to/file>",
        "already in your context",
        "context budget",
        "verifying listed files",
        "CURRENT prompt",
        "earlier turns are stale",
    ],
)
def test_agent_side_contains_required_marker(marker: str) -> None:
    assert marker in AGENT_SIDE, f"AGENT_SIDE missing marker: {marker!r}"


@pytest.mark.parametrize(
    "marker",
    [
        "shell-substitution",
        "$(...)",
        "## Existing files",
        "## Diff",
        "## New files",
        "### <path/to/file>",
        "200KB",
        "SESSION_ID",
    ],
)
def test_orchestrator_side_contains_required_marker(marker: str) -> None:
    assert marker in ORCHESTRATOR_SIDE, f"ORCHESTRATOR_SIDE missing marker: {marker!r}"


# ───── inject_agent_side is a pure prepend ─────


def test_inject_agent_side_prepends_to_non_empty() -> None:
    out = inject_agent_side("user prompt body")
    assert out.startswith(AGENT_SIDE)
    assert out.endswith("user prompt body")
    assert "\n\n---\n\n" in out


def test_inject_agent_side_prepends_to_empty() -> None:
    """Empty prompt is a degenerate but legal input — still prepend."""
    out = inject_agent_side("")
    assert out.startswith(AGENT_SIDE)
    assert out.endswith("---\n\n")


# ───── warn_if_oversized — byte-level threshold ─────


def test_warn_if_oversized_fires_above_threshold(caplog: pytest.LogCaptureFixture) -> None:
    big = "x" * (OVERSIZED_THRESHOLD_BYTES + 1)
    with caplog.at_level("WARNING", logger="prism_router"):
        warn_if_oversized(big, backend_name="codex", cwd=Path("/tmp/x"))
    assert any("oversized prompt" in r.message for r in caplog.records)
    assert any("backend=codex" in r.getMessage() for r in caplog.records)


def test_warn_if_oversized_silent_at_or_below_threshold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Boundary: exactly OVERSIZED_THRESHOLD_BYTES must NOT warn (strict `>`)."""
    at_threshold = "x" * OVERSIZED_THRESHOLD_BYTES
    with caplog.at_level("WARNING", logger="prism_router"):
        warn_if_oversized(at_threshold, backend_name="codex", cwd=Path("/tmp/x"))
    assert not any("oversized prompt" in r.message for r in caplog.records)


def test_warn_if_oversized_measures_utf8_bytes_not_codepoints(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A `—` em-dash is 3 UTF-8 bytes but 1 code point. The threshold must
    fire on the byte count, not `len(str)`."""
    # 70_000 em-dashes = 70_000 code points = 210_000 UTF-8 bytes → over.
    payload = "—" * 70_000
    assert len(payload) < OVERSIZED_THRESHOLD_BYTES  # would NOT trip code-point counter
    with caplog.at_level("WARNING", logger="prism_router"):
        warn_if_oversized(payload, backend_name="codex", cwd=Path("/tmp/x"))
    assert any("oversized prompt" in r.message for r in caplog.records)
