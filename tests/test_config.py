"""§4.3 ServerConfig — env defaults + override resolution."""

from __future__ import annotations

import logging

from prism_router.config import ServerConfig


def test_defaults_when_env_empty():
    cfg = ServerConfig.from_env({})
    assert cfg.require_approval is False
    # True by design: no `-y` → gemini declares no write/shell tools
    # (measured on gemini-cli 0.57.0).
    # docs/bugs/gemini-default-write-capability.md
    assert cfg.gemini_require_approval is True
    assert cfg.gemini_model == ""
    assert cfg.log_level == logging.INFO


def test_gemini_require_approval_variants():
    for raw in ("1", "true", "TRUE", "yes", "on", "  True  "):
        assert ServerConfig.from_env({"GEMINI_REQUIRE_APPROVAL": raw}).gemini_require_approval is True, raw
    for raw in ("0", "false", "no", "off", "", "garbage"):
        assert ServerConfig.from_env({"GEMINI_REQUIRE_APPROVAL": raw}).gemini_require_approval is False, raw


def test_require_approval_truthy_variants():
    for raw in ("1", "true", "TRUE", "yes", "on", "  True  "):
        cfg = ServerConfig.from_env({"CODEX_REQUIRE_APPROVAL": raw})
        assert cfg.require_approval is True, raw


def test_require_approval_falsy_variants():
    for raw in ("0", "false", "no", "off", "", "garbage"):
        cfg = ServerConfig.from_env({"CODEX_REQUIRE_APPROVAL": raw})
        assert cfg.require_approval is False, raw


def test_gemini_model_passthrough_and_strip():
    cfg = ServerConfig.from_env({"GEMINI_MODEL": "  gemini-2.0-flash  "})
    assert cfg.gemini_model == "gemini-2.0-flash"


def test_log_level_mapping():
    cases = {
        "debug": logging.DEBUG,
        "DEBUG": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "unknown": logging.INFO,  # falls back to info
        "": logging.INFO,
    }
    for raw, expected in cases.items():
        cfg = ServerConfig.from_env({"PRISM_LOG_LEVEL": raw})
        assert cfg.log_level == expected, raw


def test_probe_timeout_defaults():
    cfg = ServerConfig.from_env({})
    assert cfg.codex_probe_timeout == 5.0
    assert cfg.gemini_probe_timeout == 12.0


def test_probe_timeout_overrides():
    cfg = ServerConfig.from_env({
        "PRISM_PROBE_TIMEOUT_CODEX": "8",
        "PRISM_PROBE_TIMEOUT_GEMINI": "20.5",
    })
    assert cfg.codex_probe_timeout == 8.0
    assert cfg.gemini_probe_timeout == 20.5


def test_probe_timeout_invalid_falls_back_to_default(caplog):
    """Malformed / non-finite / non-positive operator knob must not crash
    startup nor yield an instant-timeout probe — fall back + warn."""
    for raw in ("abc", "", "  ", "0", "-3", "nan", "inf", "-inf"):
        with caplog.at_level("WARNING", logger="prism_router"):
            cfg = ServerConfig.from_env({"PRISM_PROBE_TIMEOUT_GEMINI": raw})
        assert cfg.gemini_probe_timeout == 12.0, raw
    assert any("PRISM_PROBE_TIMEOUT_GEMINI" in r.getMessage() for r in caplog.records)


def test_liveness_progress_defaults_on_and_honours_the_knob():
    """Default-on at merge (docs/bugs/host-idle-progress-egress.md); the knob
    is the operator's off switch."""
    assert ServerConfig.from_env({}).liveness_progress is True
    for raw in ("1", "true", "yes", "on"):
        assert ServerConfig.from_env({"PRISM_LIVENESS_PROGRESS": raw}).liveness_progress is True, raw
    for raw in ("0", "false", "no", "off", "", "garbage"):
        assert ServerConfig.from_env({"PRISM_LIVENESS_PROGRESS": raw}).liveness_progress is False, raw


def test_liveness_progress_field_has_a_default_for_direct_construction():
    """F7: existing direct `ServerConfig(...)` constructions must keep working
    without naming the new field."""
    cfg = ServerConfig(
        require_approval=False, gemini_require_approval=False, gemini_model="",
        log_level=logging.INFO, codex_probe_timeout=5.0, gemini_probe_timeout=12.0,
    )
    assert cfg.liveness_progress is True


def test_gemini_at_escape_defaults_on_and_honours_the_knob():
    """Default-on: the unescaped payload hangs gemini-cli outright
    (docs/bugs/gemini-at-command-hang.md). `0` is the rollback hatch."""
    assert ServerConfig.from_env({}).gemini_at_escape is True
    for raw in ("1", "true", "yes", "on"):
        assert ServerConfig.from_env({"PRISM_GEMINI_AT_ESCAPE": raw}).gemini_at_escape is True, raw
    for raw in ("0", "false", "no", "off", "", "garbage"):
        assert ServerConfig.from_env({"PRISM_GEMINI_AT_ESCAPE": raw}).gemini_at_escape is False, raw


def test_gemini_at_escape_field_has_a_default_for_direct_construction():
    cfg = ServerConfig(
        require_approval=False, gemini_require_approval=False, gemini_model="",
        log_level=logging.INFO, codex_probe_timeout=5.0, gemini_probe_timeout=12.0,
    )
    assert cfg.gemini_at_escape is True


def test_frozen_dataclass():
    cfg = ServerConfig.from_env({})
    try:
        cfg.require_approval = True  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("ServerConfig must be frozen")
