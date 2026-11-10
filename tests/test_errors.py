"""§4.2 ErrorEnvelope shape + audit-bucket coverage."""

from __future__ import annotations

from prism_router.errors import (
    ALL_ERROR_CODES,
    ALL_STAGES,
    Diagnostics,
    ErrorEnvelope,
    make_error,
)


def test_make_error_default_stderr_tail_is_none() -> None:
    env = make_error(
        "proxy_503",
        "stream",
        retryable=True,
        raw="503 Service Unavailable: No available providers",
    )
    assert env["code"] == "proxy_503"
    assert env["stage"] == "stream"
    assert env["retryable"] is True
    assert env["raw"].startswith("503")
    assert env["stderr_tail"] is None


def test_make_error_with_stderr_tail() -> None:
    env = make_error(
        "codex_error",
        "classify",
        retryable=False,
        raw="bare failure",
        stderr_tail="line1\nline2",
    )
    assert env["stderr_tail"] == "line1\nline2"


def test_envelope_keys_are_exactly_h3_quintuple() -> None:
    env = make_error("timeout", "shutdown", retryable=True, raw="killed after 7200s")
    assert set(env.keys()) == {"code", "stage", "retryable", "raw", "stderr_tail"}


def test_all_16_audit_buckets_addressed_by_code_or_c7() -> None:
    """§4.2 audit-bucket map — explicit per-bucket assignment.

    16 audit buckets in `mcp-audit/README.md` §1+§2:
    - 15 produce envelopes (mapped to codes here)
    - 1 (`codex no-such-tool`) is intentionally **not** an envelope: C7 smoke
      probe in v1 prevents tool registration, so Claude Code's native "no
      such tool" surface fires first and no call envelope is produced.

    `daemon_unavailable` is a v2-shim-only code (§4.2 / §8.8); not a v1 bucket.
    """
    bucket_codes: dict[str, frozenset[str]] = {
        "codex 503 backend": frozenset({"proxy_503"}),
        "codex stream-disconnected": frozenset({"stream_disconnected"}),
        "codex 401": frozenset({"auth_401"}),
        "codex bare-failure": frozenset({"codex_error"}),
        "codex cli-too-long": frozenset({"cli_too_long"}),
        "codex no-such-tool": frozenset(),  # C7: no envelope by design
        "oversize-truncation": frozenset({"oversize_dropped"}),
        "codex MISSING": frozenset({
            "timeout", "spawn_failed", "daemon_lost",
            "drain_timeout", "idle_after_terminal", "pre_terminal_idle",
            "post_terminal_grace", "transport_failure",
            "request_too_large", "disk_unavailable", "body_idle_timeout",
        }),
        "gemini no_session_id": frozenset({"no_session"}),
        "gemini augment-stdout-pollution": frozenset({"parse_error"}),
        "gemini stop-self": frozenset({"model_refused"}),
        "gemini WinError 206": frozenset({"cli_too_long"}),
        "gemini vertex-misconfig": frozenset({"auth_401"}),
        "gemini invalid-session-id": frozenset({"no_session"}),
        "gemini MISSING": frozenset({
            "timeout", "gemini_error", "empty_final",
            "drain_timeout", "idle_after_terminal",
        }),
        "transient (Reconnecting…)": frozenset({"transient"}),
    }
    assert len(bucket_codes) == 16
    mapped = frozenset().union(*bucket_codes.values())
    # Every mapped code must exist in the canonical code set.
    assert mapped <= ALL_ERROR_CODES
    # Canonical codes NOT tied to a v1 audit bucket: `daemon_unavailable`
    # (v2 shim/daemon split, §8.8) and `prompt_file_error` (pre-flight
    # prompt_file ingestion — new capability, not an audit failure mode).
    # Asserting this exact diff prevents silent drift if someone adds or
    # removes a code without updating buckets.
    assert ALL_ERROR_CODES - mapped == frozenset(
        {"daemon_unavailable", "prompt_file_error"}
    )


def test_all_stages_covered() -> None:
    assert ALL_STAGES == frozenset(
        {"pre_flight", "shim", "spawn", "stream", "parse", "classify", "shutdown"}
    )


def test_diagnostics_typeddict_shape() -> None:
    diag: Diagnostics = {
        "noise_log": ["Registering notification handlers..."],
        "kill_chain": [{"signal": "SIGTERM", "pid": 1234, "ts_ms": 100}],
        "backend_pid": 1234,
        "backend_chosen": "codex",
    }
    assert diag["backend_chosen"] == "codex"
    assert diag["noise_log"][0].startswith("Registering")


def test_diagnostics_accepts_optional_rollout_recovery_keys() -> None:
    """NotRequired fields used by dispatcher's rollout-recovery path
    (docs/bugs/rollout-recovery.md). Absent on normal success; present
    only when the answer came from codex's per-session JSONL ledger."""
    diag: Diagnostics = {
        "noise_log": [],
        "kill_chain": None,
        "backend_pid": None,
        "backend_chosen": "codex",
        "recovered_from_rollout": True,
        "rollout_path": "C:/Users/x/.codex/sessions/2026/05/18/rollout-...-uuid.jsonl",
        "max_stdout_silence_s": 1200.4,
    }
    assert diag["recovered_from_rollout"] is True
    assert diag["rollout_path"].endswith(".jsonl")
    assert diag["max_stdout_silence_s"] > 0


def test_envelope_typeddict_assignable_from_helper() -> None:
    env: ErrorEnvelope = make_error(
        "no_session", "stream", retryable=True, raw="no init frame"
    )
    assert env["code"] == "no_session"
