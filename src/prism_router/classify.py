"""§4.2 audit-bucket → ErrorCode classifier. Pure: ParseResult + tail → envelope.

Priority order (first match wins):
1. `timed_out` — overrides everything (force-kill happened)
2. `pre_terminal_idle` — main-dispatcher killed because no stdout activity
   within threshold after `first_codex_output` (upstream codex CLI wedged
   without emitting terminal). Outranks `drain_timeout`/`idle_after_terminal`
   because those depend on a terminal event being observed; if the kill
   chain holds BOTH `pre_terminal_idle` and one of the post-terminal codes,
   `pre_terminal_idle` is the canonical cause.
3. `drain_timeout` — wrapper bounded the post-exit pipe drain (Windows
   pipe-handle leak by codex shell-tool grandchildren)
4. `idle_after_terminal` — wrapper killed because no stdout for
   `post_terminal_idle` after a terminal event
5. `cli_too_long` — stderr leakage of argv overflow (post stdin-pipe should not fire,
   but classify catches residual)
5. error_events: 503 → proxy_503 / 401 → auth_401 / disconnect → stream_disconnected
6. gemini noise_log 503/504 fallback (proxy chatter that didn't make it into a
   typed `error` event)
7. `model_refused` — terminal event status indicates self-stop/cancel
8. `*_error` catch-all — error_events present but no specific match.
   Gemini empty/malformed-stream matches flip `retryable=True`
   (`is_transient_empty_stream`) so server can re-issue once.
9. `spawn_failed` — non-zero rc + no terminal event seen + no error events
   (CLI rejected before streaming — bad flag, missing binary, etc.)
10. `no_session` — session_id missing (gemini); for codex collapses to codex_error.
    Fires when stream produced terminal but no init OR rc==0 with empty stream.
11. `parse_error` — noise_log holds JSON-shaped lines with whitelisted type that
    repeatedly failed required-field validation (real protocol corruption, not pollution)
12. `*_error` exit-code catch-all — non-zero rc with terminal event seen.
    Skipped iff the runner's post-terminal kill chain fired AND content was
    captured: that rc!=0 was wrapper-induced (codex 0.80 `--json` non-exit
    fix), not a model failure. Without the kill-chain signal, a real backend
    failure that left partial content + terminal could be misclassified.
13. None — happy path
"""

from __future__ import annotations

import json
import re

from .errors import ErrorEnvelope, make_error
from .events import EventSpec, validate_required_fields
from .parser import ParseResult

# Pattern groups. All case-insensitive substring checks.
_RE_503 = re.compile(
    r"\b503\b|service unavailable|bad gateway|\b502\b"
    r"|no[_\s-]+available[_\s-]+providers",
    re.IGNORECASE,
)
_RE_504 = re.compile(r"\b504\b|gateway timeout", re.IGNORECASE)
_RE_401 = re.compile(
    r"\b401\b|unauthorized|authentication"
    r"|invalid[_\s-]+api[_\s-]+key|incorrect\s+api\s+key",
    re.IGNORECASE,
)
_RE_STREAM_DISCONNECT = re.compile(
    r"stream\s+(?:closed|disconnected|interrupted|ended)"
    r"|sse\s+closed"
    r"|connection\s+(?:reset|closed|aborted)"
    r"|broken\s+pipe"
    r"|econnreset"
    r"|premature\s+end",
    re.IGNORECASE,
)
_RE_INVALID_SESSION = re.compile(
    r"invalid\s+session(?:\s+(?:identifier|id))?"
    r"|error\s+resuming\s+session"
    r"|no\s+session\s+found",
    re.IGNORECASE,
)
_RE_CLI_TOO_LONG = re.compile(
    r"argument\s+list\s+too\s+long"
    r"|command\s+line\s+too\s+long"
    r"|winerror\s+206"
    r"|the\s+filename\s+or\s+extension\s+is\s+too\s+long",
    re.IGNORECASE,
)

_RE_TRANSIENT_EMPTY_STREAM = re.compile(
    r"empty response|malformed tool call|invalid stream",
    re.IGNORECASE,
)

_REFUSAL_STATUSES = frozenset(
    {"stopped", "stopped_by_self", "self_stop", "cancelled", "canceled", "aborted", "refused"}
)


def is_transient_empty_stream(error: ErrorEnvelope | None) -> bool:
    """Gemini classify-stage empty/malformed-stream error — intermittent
    (identical re-invocation succeeds); safe to re-issue once.
    docs/bugs/gemini-transient-empty-stream-retry.md."""
    return (
        error is not None
        and error["code"] == "gemini_error"
        and error["stage"] == "classify"
        and _RE_TRANSIENT_EMPTY_STREAM.search(error["raw"]) is not None
    )


def _msg(event: dict) -> str:
    raw = event.get("message")
    return raw if isinstance(raw, str) else ""


def _tail(stderr_tail: str) -> str | None:
    """Empty tail collapses to None for cleaner envelope JSON."""
    return stderr_tail if stderr_tail else None


def _diagnostic_candidates(
    parse_result: ParseResult, stderr_tail: str, *, include_noise: bool
) -> list[str]:
    """Stderr tail (always) + noise_log (gemini only — codex stream is cleaner)."""
    out = [stderr_tail] if stderr_tail else []
    if include_noise:
        out.extend(parse_result.noise_log)
    return out


def _first_match(candidates: list[str], pattern: re.Pattern[str]) -> str | None:
    for text in candidates:
        if pattern.search(text):
            return text
    return None


def _refusal_status(parse_result: ParseResult, spec: EventSpec) -> str | None:
    """Last terminal event whose `status` matches a refusal-class string."""
    for ev in reversed(parse_result.all_messages):
        if ev.get("type") not in spec.terminal_event_types:
            continue
        status = ev.get("status")
        if isinstance(status, str) and status.lower() in _REFUSAL_STATUSES:
            return status
        return None  # most-recent terminal isn't refusal — stop
    return None


def _parse_error_candidate(parse_result: ParseResult, spec: EventSpec) -> str | None:
    """A noise_log line that is JSON-shaped AND has whitelisted type AND fails
    required-field validation. Distinguishes real protocol corruption from
    plain log noise."""
    for line in parse_result.noise_log:
        stripped = line.strip()
        if not (stripped.startswith("{") and stripped.endswith("}")):
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        etype = obj.get("type")
        if not (isinstance(etype, str) and etype in spec.event_types):
            continue
        if validate_required_fields(spec, etype, obj):
            continue  # validates fine; not a parse error
        return stripped
    return None


def classify(
    parse_result: ParseResult,
    *,
    spec: EventSpec,
    returncode: int | None,
    stderr_tail: str,
    timed_out: bool,
    post_terminal_killed: bool = False,
    kill_reasons: set[str] | None = None,
) -> ErrorEnvelope | None:
    tail = _tail(stderr_tail)
    is_codex = spec.name == "codex"
    catch_all = "codex_error" if is_codex else "gemini_error"
    reasons = kill_reasons or set()

    if timed_out:
        return make_error(
            "timeout", "shutdown", retryable=True,
            raw=f"overall timeout exceeded (rc={returncode})",
            stderr_tail=tail,
        )

    if "pre_terminal_idle" in reasons:
        return make_error(
            "pre_terminal_idle", "shutdown", retryable=True,
            raw=(
                f"no tick-eligible stdout within threshold after "
                f"first_codex_output (recognized reconnect chatter excluded; "
                f"rc={returncode}); upstream codex CLI wedged. "
                f"Retry with PRISM_PRE_TERMINAL_IDLE=3600 for known long-silence tasks."
            ),
            stderr_tail=tail,
        )

    if "drain_timeout" in reasons:
        return make_error(
            "drain_timeout", "shutdown", retryable=True,
            raw=f"stdout/stderr drain exceeded grace (rc={returncode})",
            stderr_tail=tail,
        )

    if "idle_after_terminal" in reasons:
        return make_error(
            "idle_after_terminal", "shutdown", retryable=True,
            raw=f"no stdout after terminal event (rc={returncode})",
            stderr_tail=tail,
        )

    if _RE_CLI_TOO_LONG.search(stderr_tail):
        return make_error(
            "cli_too_long", "pre_flight", retryable=False,
            raw=stderr_tail, stderr_tail=tail,
        )

    for ev in parse_result.error_events:
        msg = _msg(ev)
        if _RE_503.search(msg):
            return make_error("proxy_503", "stream", retryable=True, raw=msg, stderr_tail=tail)
        if _RE_401.search(msg):
            return make_error("auth_401", "stream", retryable=False, raw=msg, stderr_tail=tail)
        if _RE_STREAM_DISCONNECT.search(msg):
            return make_error(
                "stream_disconnected", "stream", retryable=True, raw=msg, stderr_tail=tail
            )

    diagnostics = _diagnostic_candidates(parse_result, stderr_tail, include_noise=not is_codex)
    proxy_diag = _first_match(diagnostics, _RE_503) or _first_match(diagnostics, _RE_504)
    if proxy_diag is not None:
        return make_error(
            "proxy_503", "stream", retryable=True, raw=proxy_diag, stderr_tail=tail
        )
    auth_diag = _first_match(diagnostics, _RE_401)
    if auth_diag is not None:
        return make_error(
            "auth_401", "stream", retryable=False, raw=auth_diag, stderr_tail=tail
        )
    disc_diag = _first_match(diagnostics, _RE_STREAM_DISCONNECT)
    if disc_diag is not None:
        return make_error(
            "stream_disconnected", "stream", retryable=True, raw=disc_diag, stderr_tail=tail
        )
    if not is_codex:
        invalid_session = _first_match(diagnostics, _RE_INVALID_SESSION)
        if invalid_session is not None:
            return make_error(
                "no_session", "spawn", retryable=False,
                raw=invalid_session, stderr_tail=tail,
            )

    refusal = _refusal_status(parse_result, spec)
    if refusal is not None:
        return make_error(
            "model_refused", "classify", retryable=False,
            raw=f"terminal status={refusal!r}", stderr_tail=tail,
        )

    if parse_result.error_events:
        last = parse_result.error_events[-1]
        env = make_error(
            catch_all, "classify", retryable=False,
            raw=_msg(last) or "<empty error event>", stderr_tail=tail,
        )
        if is_transient_empty_stream(env):
            env["retryable"] = True
        return env

    if (
        returncode is not None
        and returncode != 0
        and not parse_result.saw_terminal
        and not parse_result.error_events
    ):
        return make_error(
            "spawn_failed", "spawn", retryable=False,
            raw=f"exit {returncode}", stderr_tail=tail,
        )

    if parse_result.session_id is None:
        if is_codex:
            return make_error(
                "codex_error", "stream", retryable=False,
                raw="<stream ended without thread_id>", stderr_tail=tail,
            )
        return make_error(
            "no_session", "stream", retryable=False,
            raw="<stream ended without session_id>", stderr_tail=tail,
        )

    parse_err = _parse_error_candidate(parse_result, spec)
    if parse_err is not None:
        return make_error(
            "parse_error", "parse", retryable=False,
            raw=parse_err, stderr_tail=tail,
        )

    if returncode is not None and returncode != 0:
        # Wrapper-induced rc!=0: when the runner's post-terminal kill chain
        # fired (runner.py:8 — codex 0.80 `--json` non-exit fix) AND content
        # was captured, the exit code is ours, not the model's. Without the
        # kill-chain signal, a backend that emitted terminal+partial-content
        # then exited non-zero on its own would be silently swallowed.
        if (
            post_terminal_killed
            and parse_result.saw_terminal
            and parse_result.agent_messages
        ):
            return None
        return make_error(
            catch_all, "classify", retryable=False,
            raw=f"exit {returncode}", stderr_tail=tail,
        )

    return None
