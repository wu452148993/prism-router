"""§4.2 audit-bucket → ErrorCode classifier — full priority-ladder coverage.

ParseResult is constructed directly (no parser run) — these are pure-function
tests of the mapping logic.
"""

from __future__ import annotations

import pytest

from prism_router.classify import classify, is_transient_empty_stream
from prism_router.errors import make_error
from prism_router.events import CODEX_SPEC, GEMINI_SPEC
from prism_router.parser import ParseResult


def _pr(
    *,
    session_id: str | None = "sid-1",
    agent_messages: str = "ok",
    all_messages: list[dict] | None = None,
    noise_log: list[str] | None = None,
    transient_lines: list[str] | None = None,
    saw_terminal: bool = True,
    error_events: list[dict] | None = None,
) -> ParseResult:
    return ParseResult(
        session_id=session_id,
        agent_messages=agent_messages,
        all_messages=all_messages or [],
        noise_log=noise_log or [],
        transient_lines=transient_lines or [],
        saw_terminal=saw_terminal,
        error_events=error_events or [],
    )


# ---------- happy path ----------

def test_success_returns_none():
    pr = _pr(all_messages=[{"type": "thread.completed"}])
    assert classify(pr, spec=CODEX_SPEC, returncode=0, stderr_tail="", timed_out=False) is None


# ---------- priority 1: timeout ----------

def test_timeout_dominates_over_error_events():
    pr = _pr(error_events=[{"type": "error", "message": "503 Service Unavailable"}])
    env = classify(pr, spec=CODEX_SPEC, returncode=None, stderr_tail="", timed_out=True)
    assert env is not None
    assert env["code"] == "timeout"
    assert env["stage"] == "shutdown"
    assert env["retryable"] is True
    assert env["stderr_tail"] is None  # empty tail → None


def test_timeout_carries_stderr_tail_when_present():
    env = classify(_pr(), spec=CODEX_SPEC, returncode=-9, stderr_tail="killed", timed_out=True)
    assert env["stderr_tail"] == "killed"


# ---------- priority 2: drain_timeout (post-exit pipe held by grandchildren) ----------

def test_drain_timeout_classified_as_shutdown_error():
    """Wrapper bounded the post-exit pipe drain (Patch A'). Classifier must
    surface this as a typed error, not silently route to success."""
    pr = _pr(saw_terminal=True, agent_messages="hi")
    env = classify(
        pr, spec=CODEX_SPEC, returncode=0, stderr_tail="", timed_out=False,
        kill_reasons={"drain_timeout"},
    )
    assert env is not None
    assert env["code"] == "drain_timeout"
    assert env["stage"] == "shutdown"
    assert env["retryable"] is True


def test_timed_out_dominates_drain_timeout_when_both_present():
    """Pathological: overall_timeout fires first AND drain_grace later
    appends a drain_timeout KillStep. timed_out=True wins per ladder."""
    env = classify(
        _pr(), spec=CODEX_SPEC, returncode=-9, stderr_tail="", timed_out=True,
        kill_reasons={"overall_timeout", "drain_timeout"},
    )
    assert env["code"] == "timeout"


# ---------- priority 3: idle_after_terminal (watchdog forced kill) ----------

def test_idle_after_terminal_classified_as_shutdown_error():
    """Patch C watchdog fired: saw_terminal + no fresh stdout for the
    arm window → wrapper-induced kill, must classify as shutdown error."""
    pr = _pr(saw_terminal=True, agent_messages="partial")
    env = classify(
        pr, spec=CODEX_SPEC, returncode=-9, stderr_tail="", timed_out=False,
        kill_reasons={"idle_after_terminal"},
    )
    assert env is not None
    assert env["code"] == "idle_after_terminal"
    assert env["stage"] == "shutdown"
    assert env["retryable"] is True


def test_drain_timeout_dominates_idle_after_terminal():
    """Both reasons in kill_chain (defense-in-depth): the more specific
    drain_timeout (post-exit) wins — ladder ordering pin."""
    env = classify(
        _pr(saw_terminal=True), spec=CODEX_SPEC, returncode=0, stderr_tail="",
        timed_out=False,
        kill_reasons={"drain_timeout", "idle_after_terminal"},
    )
    assert env["code"] == "drain_timeout"


# ---------- priority 2: pre_terminal_idle (pre-terminal stdout-idle watchdog) ----------

def test_pre_terminal_idle_classified_as_shutdown_error():
    """Dispatcher pre_terminal_idle watchdog fired: stdout silent past
    threshold before any terminal event seen. Wrapper-induced kill must
    surface as a typed retryable error with explicit retry guidance in raw."""
    pr = _pr(saw_terminal=False, agent_messages="")
    env = classify(
        pr, spec=CODEX_SPEC, returncode=-9, stderr_tail="", timed_out=False,
        kill_reasons={"pre_terminal_idle"},
    )
    assert env is not None
    assert env["code"] == "pre_terminal_idle"
    assert env["stage"] == "shutdown"
    assert env["retryable"] is True
    assert "PRISM_PRE_TERMINAL_IDLE" in env["raw"]


def test_pre_terminal_idle_dominates_drain_timeout_and_idle_after_terminal():
    """If pre_terminal_idle is in kill_reasons, it outranks the post-terminal
    codes: the wrapper killed BEFORE any terminal event, so any later
    drain/idle reason is non-canonical noise."""
    env = classify(
        _pr(saw_terminal=False), spec=CODEX_SPEC, returncode=-9, stderr_tail="",
        timed_out=False,
        kill_reasons={"pre_terminal_idle", "drain_timeout", "idle_after_terminal"},
    )
    assert env["code"] == "pre_terminal_idle"


def test_timed_out_dominates_pre_terminal_idle():
    """timed_out is the top of the ladder; overall_timeout race wins."""
    env = classify(
        _pr(saw_terminal=False), spec=CODEX_SPEC, returncode=-9, stderr_tail="",
        timed_out=True,
        kill_reasons={"pre_terminal_idle"},
    )
    assert env["code"] == "timeout"


# ---------- priority 5: cli_too_long ----------

def test_cli_too_long_winerror206():
    env = classify(
        _pr(session_id=None, saw_terminal=False),
        spec=GEMINI_SPEC, returncode=1,
        stderr_tail="OSError: [WinError 206] The filename or extension is too long",
        timed_out=False,
    )
    assert env["code"] == "cli_too_long"
    assert env["stage"] == "pre_flight"
    assert env["retryable"] is False


def test_cli_too_long_posix():
    env = classify(
        _pr(session_id=None, saw_terminal=False),
        spec=CODEX_SPEC, returncode=126,
        stderr_tail="execve: argument list too long",
        timed_out=False,
    )
    assert env["code"] == "cli_too_long"


# ---------- priority 3: error_events specific patterns ----------

def test_proxy_503_from_codex_error_event():
    pr = _pr(
        session_id=None,
        saw_terminal=False,
        error_events=[{"type": "error", "message": "stream error: 503 Service Unavailable"}],
    )
    env = classify(pr, spec=CODEX_SPEC, returncode=1, stderr_tail="", timed_out=False)
    assert env["code"] == "proxy_503"
    assert env["stage"] == "stream"
    assert env["retryable"] is True


def test_proxy_503_bad_gateway_variant():
    pr = _pr(error_events=[{"type": "error", "message": "upstream error 502 Bad Gateway"}])
    env = classify(pr, spec=CODEX_SPEC, returncode=1, stderr_tail="", timed_out=False)
    assert env["code"] == "proxy_503"


def test_auth_401_from_error_event():
    pr = _pr(
        session_id=None, saw_terminal=False,
        error_events=[{"type": "error", "message": "401 Unauthorized: invalid api key"}],
    )
    env = classify(pr, spec=CODEX_SPEC, returncode=1, stderr_tail="", timed_out=False)
    assert env["code"] == "auth_401"
    assert env["stage"] == "stream"
    assert env["retryable"] is False


def test_proxy_503_from_stderr_tail_fallback():
    """codex bare-failure stderr-only path: 503 in stderr_tail must classify
    as proxy_503/stream, not collapse into spawn_failed/codex_error."""
    pr = _pr(session_id=None, saw_terminal=False)
    env = classify(
        pr, spec=CODEX_SPEC, returncode=1,
        stderr_tail="[codex error] unexpected status 503 Service Unavailable: No available providers",
        timed_out=False,
    )
    assert env["code"] == "proxy_503"
    assert env["stage"] == "stream"


def test_auth_401_from_stderr_tail_fallback():
    pr = _pr(session_id=None, saw_terminal=False)
    env = classify(
        pr, spec=CODEX_SPEC, returncode=1,
        stderr_tail="HTTP error: 401 Unauthorized, auth error code: invalid_api_key",
        timed_out=False,
    )
    assert env["code"] == "auth_401"
    assert env["stage"] == "stream"


def test_stream_disconnected_from_stderr_tail_fallback():
    pr = _pr(session_id=None, saw_terminal=False)
    env = classify(
        pr, spec=CODEX_SPEC, returncode=1,
        stderr_tail="SSE closed before response.completed",
        timed_out=False,
    )
    assert env["code"] == "stream_disconnected"


def test_stream_disconnected_econnreset():
    pr = _pr(error_events=[{"type": "error", "message": "ECONNRESET while reading SSE"}])
    env = classify(pr, spec=CODEX_SPEC, returncode=1, stderr_tail="", timed_out=False)
    assert env["code"] == "stream_disconnected"
    assert env["retryable"] is True


def test_stream_disconnected_premature_end():
    pr = _pr(error_events=[{"type": "fail", "message": "premature end of stream"}])
    env = classify(pr, spec=CODEX_SPEC, returncode=1, stderr_tail="", timed_out=False)
    assert env["code"] == "stream_disconnected"


def test_503_takes_precedence_over_disconnect_in_first_event():
    """First matching event wins; ladder per-event-then-per-pattern."""
    pr = _pr(error_events=[
        {"type": "error", "message": "503 Service Unavailable"},
        {"type": "error", "message": "stream closed"},
    ])
    env = classify(pr, spec=CODEX_SPEC, returncode=1, stderr_tail="", timed_out=False)
    assert env["code"] == "proxy_503"


# ---------- priority 4: gemini noise_log proxy chatter ----------

def test_gemini_noise_503_falls_back_to_proxy_503():
    pr = _pr(
        session_id=None, saw_terminal=False,
        noise_log=["proxy upstream returned 503 Service Unavailable"],
    )
    env = classify(pr, spec=GEMINI_SPEC, returncode=1, stderr_tail="", timed_out=False)
    assert env["code"] == "proxy_503"


def test_gemini_noise_504_also_proxy_503():
    pr = _pr(noise_log=["504 Gateway Timeout from cdn"])
    env = classify(pr, spec=GEMINI_SPEC, returncode=1, stderr_tail="", timed_out=False)
    assert env["code"] == "proxy_503"


def test_codex_noise_log_does_NOT_trigger_proxy_503():
    """noise_log proxy chatter is gemini-specific (the upstream proxy logs are
    co-mingled with gemini's stdout). Codex stream is cleaner — only typed
    error events should drive proxy_503 there."""
    pr = _pr(
        session_id=None, saw_terminal=False,
        noise_log=["503 Service Unavailable garbage"],
    )
    env = classify(pr, spec=CODEX_SPEC, returncode=1, stderr_tail="", timed_out=False)
    assert env["code"] != "proxy_503"


# ---------- priority 5: model_refused ----------

def test_gemini_refused_via_terminal_status():
    pr = _pr(
        all_messages=[
            {"type": "init", "session_id": "s1", "model": "x"},
            {"type": "result", "status": "stopped_by_self"},
        ],
    )
    env = classify(pr, spec=GEMINI_SPEC, returncode=0, stderr_tail="", timed_out=False)
    assert env["code"] == "model_refused"
    assert env["stage"] == "classify"
    assert env["retryable"] is False


def test_gemini_normal_status_not_refusal():
    pr = _pr(all_messages=[{"type": "result", "status": "ok"}])
    env = classify(pr, spec=GEMINI_SPEC, returncode=0, stderr_tail="", timed_out=False)
    assert env is None


def test_only_last_terminal_evaluated():
    """Earlier 'cancelled' followed by later 'completed' (artificial) — ladder
    walks reversed; first non-terminal-or-non-refusal stops the search."""
    pr = _pr(all_messages=[
        {"type": "result", "status": "stopped"},
        {"type": "result", "status": "completed"},
    ])
    env = classify(pr, spec=GEMINI_SPEC, returncode=0, stderr_tail="", timed_out=False)
    assert env is None  # last terminal "completed" not in refusal set


# ---------- priority 6: error_events catch-all ----------

def test_codex_error_catchall_when_no_pattern_matches():
    pr = _pr(error_events=[{"type": "error", "message": "internal exec failure XYZ"}])
    env = classify(pr, spec=CODEX_SPEC, returncode=1, stderr_tail="", timed_out=False)
    assert env["code"] == "codex_error"
    assert env["stage"] == "classify"


def test_gemini_error_catchall():
    pr = _pr(error_events=[{"type": "error", "severity": "fatal", "message": "tool runtime crashed"}])
    env = classify(pr, spec=GEMINI_SPEC, returncode=1, stderr_tail="", timed_out=False)
    assert env["code"] == "gemini_error"


def test_catchall_with_empty_message_field():
    pr = _pr(error_events=[{"type": "error"}])  # no message
    env = classify(pr, spec=CODEX_SPEC, returncode=1, stderr_tail="", timed_out=False)
    assert env["code"] == "codex_error"
    assert "<empty error event>" in env["raw"]


_EMPTY_STREAM_MSG = (
    "Invalid stream: The model returned an empty response or malformed tool call."
)


def test_gemini_empty_stream_catchall_is_retryable():
    """Transient CP936 empty/malformed-stream failure: same catch-all code/
    stage/raw, but retryable=True so server may re-issue once."""
    pr = _pr(error_events=[{"type": "error", "message": _EMPTY_STREAM_MSG}])
    env = classify(pr, spec=GEMINI_SPEC, returncode=0, stderr_tail="", timed_out=False)
    assert env["code"] == "gemini_error"
    assert env["stage"] == "classify"
    assert env["retryable"] is True
    assert env["raw"] == _EMPTY_STREAM_MSG


def test_codex_empty_stream_catchall_stays_non_retryable():
    pr = _pr(error_events=[{"type": "error", "message": _EMPTY_STREAM_MSG}])
    env = classify(pr, spec=CODEX_SPEC, returncode=0, stderr_tail="", timed_out=False)
    assert env["code"] == "codex_error"
    assert env["retryable"] is False


def test_gemini_other_catchall_stays_non_retryable():
    pr = _pr(error_events=[{"type": "error", "message": "tool runtime crashed"}])
    env = classify(pr, spec=GEMINI_SPEC, returncode=0, stderr_tail="", timed_out=False)
    assert env["code"] == "gemini_error"
    assert env["retryable"] is False


def test_is_transient_empty_stream_predicate():
    match = make_error("gemini_error", "classify", retryable=False, raw=_EMPTY_STREAM_MSG)
    assert is_transient_empty_stream(match) is True
    assert is_transient_empty_stream(None) is False
    assert is_transient_empty_stream(
        make_error("gemini_error", "classify", retryable=False, raw="exit 1")
    ) is False
    assert is_transient_empty_stream(
        make_error("gemini_error", "stream", retryable=False, raw=_EMPTY_STREAM_MSG)
    ) is False
    assert is_transient_empty_stream(
        make_error("codex_error", "classify", retryable=False, raw=_EMPTY_STREAM_MSG)
    ) is False


# ---------- priority 7: no_session ----------

def test_gemini_no_session():
    pr = _pr(session_id=None, saw_terminal=True)
    env = classify(pr, spec=GEMINI_SPEC, returncode=0, stderr_tail="", timed_out=False)
    assert env["code"] == "no_session"
    assert env["stage"] == "stream"


def test_gemini_invalid_session_maps_to_no_session_spawn():
    """gemini-invalid-session-id (audit §1, 1 case) → no_session/spawn,
    distinct from gemini's stream-end no_session/stream."""
    pr = _pr(
        session_id=None, saw_terminal=False,
        noise_log=['Error resuming session: Invalid session identifier "abc".'],
    )
    env = classify(pr, spec=GEMINI_SPEC, returncode=1, stderr_tail="", timed_out=False)
    assert env["code"] == "no_session"
    assert env["stage"] == "spawn"


def test_codex_missing_thread_id_collapses_to_codex_error():
    """For codex the no-session analog (no thread.started ever observed) is
    classified as codex_error/stream — the literal no_session code is reserved
    for the gemini path per audit table."""
    pr = _pr(session_id=None, saw_terminal=True)
    env = classify(pr, spec=CODEX_SPEC, returncode=0, stderr_tail="", timed_out=False)
    assert env["code"] == "codex_error"
    assert env["stage"] == "stream"


# ---------- priority 8: parse_error ----------

def test_parse_error_when_noise_holds_corrupt_typed_json():
    """JSON-shaped line, whitelisted type, but missing required field
    (gemini error event lacking severity)."""
    pr = _pr(noise_log=['{"type":"error","message":"bad"}'])  # missing severity
    env = classify(pr, spec=GEMINI_SPEC, returncode=0, stderr_tail="", timed_out=False)
    assert env["code"] == "parse_error"
    assert env["stage"] == "parse"


def test_plain_log_noise_does_not_trigger_parse_error():
    pr = _pr(noise_log=["INFO: starting up", "Reconnecting... 1/5"])
    env = classify(pr, spec=GEMINI_SPEC, returncode=0, stderr_tail="", timed_out=False)
    assert env is None  # plain noise is fine


def test_unknown_type_in_json_is_NOT_parse_error():
    pr = _pr(noise_log=['{"type":"made_up","foo":"bar"}'])
    env = classify(pr, spec=GEMINI_SPEC, returncode=0, stderr_tail="", timed_out=False)
    assert env is None


def test_codex_malformed_item_started_in_noise_triggers_parse_error():
    """`item.started` required-field rule (parser ingress) routes a malformed
    start to noise_log. An otherwise-clean codex stream then surfaces it as
    `parse_error/parse` here — same blast radius as the M1 `item.completed`
    rule. Pin the behavior so future loosening of either rule is intentional."""
    pr = _pr(noise_log=['{"type":"item.started"}'])  # missing item
    env = classify(pr, spec=CODEX_SPEC, returncode=0, stderr_tail="", timed_out=False)
    assert env["code"] == "parse_error"
    assert env["stage"] == "parse"


# ---------- priority 9: spawn_failed ----------

def test_spawn_failed_when_rc_nonzero_and_no_terminal():
    pr = _pr(session_id=None, saw_terminal=False)
    env = classify(pr, spec=CODEX_SPEC, returncode=2, stderr_tail="bad flag", timed_out=False)
    assert env["code"] == "spawn_failed"
    assert env["stage"] == "spawn"
    assert env["stderr_tail"] == "bad flag"


# ---------- priority 10: rc-nonzero catchall (terminal seen but no content) ----------

def test_rc_nonzero_with_terminal_but_empty_content_classifies_as_backend_error():
    pr = _pr(saw_terminal=True, agent_messages="")
    env = classify(pr, spec=GEMINI_SPEC, returncode=1, stderr_tail="", timed_out=False)
    assert env["code"] == "gemini_error"
    assert env["stage"] == "classify"


@pytest.mark.parametrize("backend_spec", [CODEX_SPEC, GEMINI_SPEC])
def test_rc_nonzero_with_terminal_and_content_after_post_terminal_kill_is_success(backend_spec):
    # runner's post-terminal kill chain forces rc!=0 after a clean stream
    # (codex 0.80 --json non-exit fix); classify must not flip a real success
    # into *_error.
    pr = _pr(saw_terminal=True, agent_messages="hello")
    assert classify(
        pr, spec=backend_spec, returncode=1, stderr_tail="", timed_out=False,
        post_terminal_killed=True,
    ) is None


@pytest.mark.parametrize("backend_spec", [CODEX_SPEC, GEMINI_SPEC])
def test_rc_nonzero_with_terminal_and_content_without_post_terminal_kill_still_errors(backend_spec):
    # No post_terminal kill recorded → rc!=0 came from the backend itself,
    # not the wrapper. Must still surface as *_error to avoid swallowing
    # a real backend failure that left partial content + terminal.
    pr = _pr(saw_terminal=True, agent_messages="hello")
    env = classify(
        pr, spec=backend_spec, returncode=1, stderr_tail="", timed_out=False,
        post_terminal_killed=False,
    )
    assert env is not None
    assert env["code"] in {"codex_error", "gemini_error"}
    assert env["stage"] == "classify"


@pytest.mark.parametrize("backend_spec", [CODEX_SPEC, GEMINI_SPEC])
def test_rc_nonzero_after_post_terminal_kill_but_empty_content_still_errors(backend_spec):
    # Locks the `agent_messages` leg of the AND guard: post_terminal kill +
    # terminal seen alone are insufficient — content must also be captured.
    pr = _pr(saw_terminal=True, agent_messages="")
    env = classify(
        pr, spec=backend_spec, returncode=1, stderr_tail="", timed_out=False,
        post_terminal_killed=True,
    )
    assert env is not None
    assert env["code"] in {"codex_error", "gemini_error"}


def test_timeout_dominates_post_terminal_kill_success_guard():
    # Priority 1 (timeout) must win even when the post_terminal kill flag
    # would otherwise downgrade rc!=0 to success.
    env = classify(
        _pr(saw_terminal=True, agent_messages="hello"),
        spec=CODEX_SPEC, returncode=1, stderr_tail="", timed_out=True,
        post_terminal_killed=True,
    )
    assert env["code"] == "timeout"


@pytest.mark.parametrize("backend_spec", [CODEX_SPEC, GEMINI_SPEC])
def test_error_event_dominates_post_terminal_kill_success_guard(backend_spec):
    # Priority 3 (proxy_503 from error_events) must win — a 503 mid-stream
    # is a real failure even if the wrapper later post-terminal-killed.
    pr = _pr(error_events=[{"type": "error", "message": "503 Service Unavailable"}])
    env = classify(
        pr, spec=backend_spec, returncode=1, stderr_tail="", timed_out=False,
        post_terminal_killed=True,
    )
    assert env["code"] == "proxy_503"


# ---------- envelope shape contract ----------

def test_envelope_keys_strict_five():
    pr = _pr(error_events=[{"type": "error", "message": "503"}])
    env = classify(pr, spec=CODEX_SPEC, returncode=1, stderr_tail="last lines", timed_out=False)
    assert set(env.keys()) == {"code", "stage", "retryable", "raw", "stderr_tail"}


@pytest.mark.parametrize("backend_spec", [CODEX_SPEC, GEMINI_SPEC])
def test_no_error_when_returncode_zero_and_terminal_and_session(backend_spec):
    pr = _pr(session_id="s1", saw_terminal=True)
    assert classify(pr, spec=backend_spec, returncode=0, stderr_tail="", timed_out=False) is None
