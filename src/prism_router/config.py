"""§4.3 env-driven config. Single read at startup; immutable thereafter.

`RunTimeouts` already lives in `runner.py` (M2). This module owns the
server-lifetime knobs: GEMINI_MODEL default, REQUIRE_APPROVAL flag, log level,
and the C7 smoke-probe budgets. Kept separate from `RunTimeouts` because the
runner runs per-call but these are fixed once at startup.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass


_LOG = logging.getLogger("prism_router")

_LOG_LEVEL_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def _bool_env(env: dict[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _float_env(env: dict[str, str], key: str, default: float) -> float:
    """Positive, finite float or fall back to `default` with a warning.

    Operator knob: a malformed / non-finite / non-positive value must not
    crash startup or yield a probe that times out instantly.
    """
    raw = env.get(key)
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        _LOG.warning("%s=%r is not a number; using default %s", key, raw, default)
        return default
    if not math.isfinite(val) or val <= 0:
        _LOG.warning("%s=%r must be finite and > 0; using default %s", key, raw, default)
        return default
    return val


@dataclass(frozen=True)
class ServerConfig:
    """Server-lifetime knobs. Per-call timeouts live in `runner.RunTimeouts`."""

    require_approval: bool          # CODEX_REQUIRE_APPROVAL — reverse flag
    gemini_require_approval: bool   # GEMINI_REQUIRE_APPROVAL — reverse flag, default true
    gemini_model: str               # GEMINI_MODEL — default model when tool arg empty
    log_level: int                  # PRISM_LOG_LEVEL → logging level int
    codex_probe_timeout: float      # PRISM_PROBE_TIMEOUT_CODEX — C7 smoke-probe budget
    gemini_probe_timeout: float     # PRISM_PROBE_TIMEOUT_GEMINI — wider: Node cold start
    # PRISM_LIVENESS_PROGRESS — forward liveness evidence as MCP progress so
    # the host's idle timeout cannot cap a healthy long call. Defaulted so
    # existing direct ServerConfig(...) constructions keep working.
    # docs/bugs/host-idle-progress-egress.md
    liveness_progress: bool = True
    # PRISM_GEMINI_AT_ESCAPE — escape every at-sign in the gemini stdin payload
    # so gemini-cli's at-command scanner cannot hang on it. Set 0 to restore the
    # unescaped payload byte-for-byte. Defaulted so existing direct
    # ServerConfig(...) constructions keep working.
    # docs/bugs/gemini-at-command-hang.md
    gemini_at_escape: bool = True

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ServerConfig":
        env = os.environ if env is None else env
        level_raw = env.get("PRISM_LOG_LEVEL", "info").strip().lower()
        level = _LOG_LEVEL_MAP.get(level_raw, logging.INFO)
        return cls(
            require_approval=_bool_env(env, "CODEX_REQUIRE_APPROVAL", False),
            # Defaults true (unlike its codex twin): `-y` selects gemini's YOLO
            # approval mode, which headless also widens the TOOL DECLARATION
            # SET — withholding it leaves write_file/replace/run_shell_command
            # undeclared. A safer default, not an enforced boundary: operator
            # settings (`defaultApprovalMode`, `--approval-mode`, policy files)
            # can still declare mutating tools, and prism does not control them.
            # docs/bugs/gemini-default-write-capability.md
            gemini_require_approval=_bool_env(env, "GEMINI_REQUIRE_APPROVAL", True),
            gemini_model=env.get("GEMINI_MODEL", "").strip(),
            log_level=level,
            codex_probe_timeout=_float_env(env, "PRISM_PROBE_TIMEOUT_CODEX", 5.0),
            gemini_probe_timeout=_float_env(env, "PRISM_PROBE_TIMEOUT_GEMINI", 12.0),
            liveness_progress=_bool_env(env, "PRISM_LIVENESS_PROGRESS", True),
            gemini_at_escape=_bool_env(env, "PRISM_GEMINI_AT_ESCAPE", True),
        )
