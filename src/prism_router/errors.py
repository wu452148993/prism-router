"""§4.2 ErrorEnvelope + Diagnostics + tool result shapes. Pure types, no IO."""

from __future__ import annotations

from typing import Any, Literal

# Pydantic v2 requires typing_extensions.TypedDict on Python < 3.12 for schema gen.
# `NotRequired` lives in typing_extensions for the same compat reason; available
# in stdlib `typing` on 3.11+, re-exported here for consistency.
from typing_extensions import NotRequired, TypedDict

ErrorCode = Literal[
    "cli_too_long",
    "spawn_failed",
    "daemon_unavailable",
    "daemon_lost",
    "timeout",
    "drain_timeout",
    "idle_after_terminal",
    "pre_terminal_idle",
    "post_terminal_grace",
    "transport_failure",
    "request_too_large",
    "disk_unavailable",
    "body_idle_timeout",
    "proxy_503",
    "auth_401",
    "stream_disconnected",
    "model_refused",
    "empty_final",
    "transient",
    "codex_error",
    "gemini_error",
    "parse_error",
    "no_session",
    "oversize_dropped",
    "prompt_file_error",
]

Stage = Literal[
    "pre_flight",
    "shim",
    "spawn",
    "stream",
    "parse",
    "classify",
    "shutdown",
]

ALL_ERROR_CODES: frozenset[str] = frozenset(
    {
        "cli_too_long",
        "spawn_failed",
        "daemon_unavailable",
        "daemon_lost",
        "timeout",
        "drain_timeout",
        "idle_after_terminal",
        "pre_terminal_idle",
        "post_terminal_grace",
        "transport_failure",
        "request_too_large",
        "disk_unavailable",
        "body_idle_timeout",
        "proxy_503",
        "auth_401",
        "stream_disconnected",
        "model_refused",
        "empty_final",
        "transient",
        "codex_error",
        "gemini_error",
        "parse_error",
        "no_session",
        "oversize_dropped",
        "prompt_file_error",
    }
)

ALL_STAGES: frozenset[str] = frozenset(
    {"pre_flight", "shim", "spawn", "stream", "parse", "classify", "shutdown"}
)


class ErrorEnvelope(TypedDict):
    code: ErrorCode
    stage: Stage
    retryable: bool
    raw: str
    stderr_tail: str | None


class Diagnostics(TypedDict):
    noise_log: list[str]
    kill_chain: list[dict[str, Any]] | None
    backend_pid: int | None
    backend_chosen: str
    # Set by dispatcher's rollout-recovery path (docs/bugs/rollout-recovery.md).
    # Absent for normal success and for non-recovered errors so the wire shape
    # stays minimal; present only when the answer came from codex's per-session
    # JSONL ledger instead of stdout.
    recovered_from_rollout: NotRequired[bool]
    rollout_path: NotRequired[str]
    max_stdout_silence_s: NotRequired[float]
    # Rollout-liveness tick observability (docs/bugs/rollout-liveness-tick.md):
    # growth-tick count, watch-window length, and age of the last observed
    # growth at envelope build. Present only when the pre_terminal_idle
    # watchdog actually tracked the rollout file.
    rollout_ticks: NotRequired[int]
    rollout_watched_s: NotRequired[float]
    last_rollout_growth_age_s: NotRequired[float | None]
    # Present (True) only when the probes had gone dark by kill time — such a
    # kill carries no rollout stall verdict and must not count as a B-class
    # fingerprint.
    rollout_observation_stale: NotRequired[bool]
    # Set by server's gemini transient empty-stream retry path: number of
    # internal re-dispatches performed (currently 0 or 1).
    # docs/bugs/gemini-transient-empty-stream-retry.md
    internal_retries: NotRequired[int]


class PromptFileReceipt(TypedDict):
    """Wrapper-computed proof of `prompt_file` injection: resolved absolute
    path, sha256 hex of the raw FILE bytes (not the combined payload), and
    byte count. Present on the envelope iff the file was read and injected."""

    path: str
    sha256: str
    bytes: int


class CodexResult(TypedDict):
    """§4.2 codex tool return shape. Field order matches GuDaStudio audit data."""

    success: bool
    SESSION_ID: str | None
    agent_messages: str
    truncated: bool
    error: ErrorEnvelope | None
    all_messages: list[dict[str, Any]] | None
    diagnostics: Diagnostics | None
    prompt_file_receipt: NotRequired[PromptFileReceipt]


class GeminiResult(TypedDict):
    """§4.2 gemini tool return shape — identical fields to CodexResult."""

    success: bool
    SESSION_ID: str | None
    agent_messages: str
    truncated: bool
    error: ErrorEnvelope | None
    all_messages: list[dict[str, Any]] | None
    diagnostics: Diagnostics | None
    prompt_file_receipt: NotRequired[PromptFileReceipt]


def make_error(
    code: ErrorCode,
    stage: Stage,
    *,
    retryable: bool,
    raw: str,
    stderr_tail: str | None = None,
) -> ErrorEnvelope:
    return ErrorEnvelope(
        code=code,
        stage=stage,
        retryable=retryable,
        raw=raw,
        stderr_tail=stderr_tail,
    )
