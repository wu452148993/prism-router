"""§4.4 stream parser — the regression-critical module.

Each test pins a specific failure mode from `mcp-audit/README.md`. Test names
encode the audit-bucket the test guards.
"""

from __future__ import annotations

import json

from prism_router.events import CODEX_SPEC, GEMINI_SPEC
from prism_router.parser import NOISE_LOG_CAP, StreamParser


def _gemini_event(t: str, **fields) -> str:
    return json.dumps({"type": t, **fields})


def _codex_event(t: str, **fields) -> str:
    return json.dumps({"type": t, **fields})


# ---- Gemini happy path ----------------------------------------------------


def test_gemini_clean_stream_extracts_session_and_messages() -> None:
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            _gemini_event("init", session_id="2b3a-uuid", model="gemini-3.1-pro"),
            _gemini_event("message", role="assistant", content="hello"),
            _gemini_event("message", role="assistant", content="world"),
            _gemini_event("result", status="ok"),
        ]
    )
    r = p.result()
    assert r.session_id == "2b3a-uuid"
    assert r.agent_messages == "hello\nworld"
    assert r.saw_terminal is True
    assert r.noise_log == []
    assert r.error_events == []


def test_gemini_user_message_not_in_agent_messages() -> None:
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            _gemini_event("init", session_id="s", model="m"),
            _gemini_event("message", role="user", content="Reply with exactly: ok"),
            _gemini_event("message", role="assistant", content="ok", delta=True),
            _gemini_event("result", status="ok"),
        ]
    )
    r = p.result()
    admitted = [e for e in r.all_messages if e["type"] == "message"]
    assert [e["role"] for e in admitted] == ["user", "assistant"]
    assert r.agent_messages == "ok"
    assert r.noise_log == []


# ---- Gemini delta-stream concatenation (multi-chunk assistant utterance) -


def test_gemini_single_delta_message_extracts_text() -> None:
    """Short single-delta responses pass pre-fix and masked the multi-chunk
    bug; pin the trivial path so it cannot regress silently."""
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            _gemini_event("init", session_id="s", model="m"),
            _gemini_event("message", role="assistant", content="ok", delta=True),
            _gemini_event("result", status="ok"),
        ]
    )
    assert p.result().agent_messages == "ok"


def test_gemini_delta_chunks_concatenate_without_newlines() -> None:
    """Core regression: gemini-cli emits one event per response-stream chunk;
    chunks of one utterance must concat with '', not '\\n'."""
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            _gemini_event("init", session_id="s", model="m"),
            _gemini_event("message", role="assistant", content="Hello", delta=True),
            _gemini_event("message", role="assistant", content=" World", delta=True),
            _gemini_event("result", status="ok"),
        ]
    )
    assert p.result().agent_messages == "Hello World"


def test_gemini_non_delta_event_separates_delta_utterances() -> None:
    """tool_use between delta runs is a semantic boundary; the two utterances
    materialize as separate messages joined by '\\n'."""
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            _gemini_event("init", session_id="s", model="m"),
            _gemini_event("message", role="assistant", content="a", delta=True),
            _gemini_event("message", role="assistant", content="b", delta=True),
            _gemini_event("message", role="assistant", content="c", delta=True),
            _gemini_event("tool_use", tool_name="grep", tool_id="t1", parameters={}),
            _gemini_event("message", role="assistant", content="x", delta=True),
            _gemini_event("message", role="assistant", content="y", delta=True),
            _gemini_event("message", role="assistant", content="z", delta=True),
            _gemini_event("result", status="ok"),
        ]
    )
    assert p.result().agent_messages == "abc\nxyz"


def test_gemini_terminal_events_separate_delta_turns() -> None:
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            _gemini_event("init", session_id="s", model="m"),
            _gemini_event("message", role="assistant", content="first", delta=True),
            _gemini_event("result", status="ok"),
            _gemini_event("message", role="assistant", content="second", delta=True),
            _gemini_event("result", status="ok"),
        ]
    )
    assert p.result().agent_messages == "first\nsecond"


def test_gemini_user_delta_does_not_break_assistant_delta_buffer() -> None:
    """role-guard makes the extractor return None for the user delta, so the
    open buffer survives across it and assistant chunks keep accumulating."""
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            _gemini_event("init", session_id="s", model="m"),
            _gemini_event("message", role="assistant", content="Hel", delta=True),
            _gemini_event("message", role="user", content="ignored", delta=True),
            _gemini_event("message", role="assistant", content="lo", delta=True),
            _gemini_event("result", status="ok"),
        ]
    )
    assert p.result().agent_messages == "Hello"


def test_gemini_result_peek_does_not_split_open_delta_buffer() -> None:
    """result() must be non-mutating: a mid-stream diagnostic call cannot
    finalize an open delta utterance; later chunks still belong to it."""
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            _gemini_event("init", session_id="s", model="m"),
            _gemini_event("message", role="assistant", content="Hel", delta=True),
        ]
    )
    assert p.result().agent_messages == "Hel"
    p.feed_lines(
        [
            _gemini_event("message", role="assistant", content="lo", delta=True),
            _gemini_event("result", status="ok"),
        ]
    )
    assert p.result().agent_messages == "Hello"


def test_gemini_non_boolean_delta_not_treated_as_streaming() -> None:
    """Strict `is True` discrimination: `delta=1` / `delta="true"` are NOT
    streaming chunks. A `bool()` simplification would silently merge them."""
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            _gemini_event("init", session_id="s", model="m"),
            _gemini_event("message", role="assistant", content="a", delta=1),
            _gemini_event("message", role="assistant", content="b", delta="true"),
            _gemini_event("result", status="ok"),
        ]
    )
    assert p.result().agent_messages == "a\nb"


def test_gemini_empty_content_delta_does_not_break_buffer() -> None:
    """delta=True with content="" is admitted (per `_EMPTY_STR_LEGAL`) but the
    extractor returns None; the open buffer must survive across it."""
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            _gemini_event("init", session_id="s", model="m"),
            _gemini_event("message", role="assistant", content="Hel", delta=True),
            _gemini_event("message", role="assistant", content="", delta=True),
            _gemini_event("message", role="assistant", content="lo", delta=True),
            _gemini_event("result", status="ok"),
        ]
    )
    assert p.result().agent_messages == "Hello"


def test_gemini_error_event_separates_delta_runs() -> None:
    """error is admitted, is_streaming_delta=False, and flushes the open
    buffer; deltas on either side become separate messages joined by '\\n'."""
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            _gemini_event("init", session_id="s", model="m"),
            _gemini_event("message", role="assistant", content="part1", delta=True),
            _gemini_event("error", severity="warn", message="rate limit retry"),
            _gemini_event("message", role="assistant", content="part2", delta=True),
            _gemini_event("result", status="ok"),
        ]
    )
    r = p.result()
    assert r.agent_messages == "part1\npart2"
    assert len(r.error_events) == 1


def test_gemini_noise_lines_between_deltas_do_not_break_buffer() -> None:
    """Noise paths (invalid JSON / unknown type / missing required field) all
    return via `_add_noise()` BEFORE the buffer logic; the open buffer must
    survive across noise interleaving."""
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            _gemini_event("init", session_id="s", model="m"),
            _gemini_event("message", role="assistant", content="Hel", delta=True),
            "{not valid json",
            json.dumps({"type": "telemetry", "session_id": "x", "model": "y"}),
            json.dumps({"type": "init", "session_id": "fake"}),
            _gemini_event("message", role="assistant", content="lo", delta=True),
            _gemini_event("result", status="ok"),
        ]
    )
    r = p.result()
    assert r.agent_messages == "Hello"
    assert len(r.noise_log) == 3


def test_gemini_blank_lines_ignored() -> None:
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            "",
            "   ",
            _gemini_event("init", session_id="s", model="m"),
            "\r\n",
            _gemini_event("result", status="ok"),
        ]
    )
    r = p.result()
    assert r.session_id == "s"
    assert r.noise_log == []


# ---- Gemini augment-stdout-pollution (bucket 15) --------------------------


def test_gemini_augment_pollution_lines_go_to_noise_log() -> None:
    """The 15-case 'augment-context-engine' bucket. Plain-text log lines must
    not crash session-id extraction."""
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            "Registering notification handlers for server 'augment-context-engine'. Capabilities: { logging: {}, tools: {} }",
            "Server 'augment-context-engine' has tools but did not declare 'listChanged' capability. Listening anyway for robustness...",
            "Scheduling MCP context refresh...",
            _gemini_event("init", session_id="abc", model="gemini-3.1-pro"),
            _gemini_event("message", role="assistant", content="payload"),
            _gemini_event("result", status="ok"),
        ]
    )
    r = p.result()
    assert r.session_id == "abc"
    assert r.agent_messages == "payload"
    assert len(r.noise_log) == 3
    assert r.noise_log[0].startswith("Registering notification handlers")


def test_gemini_first_line_brace_fallback_recovers_init() -> None:
    """CCG 6988f64 — gemini's first init line may have augment prefix garbage
    glued in front of valid JSON. Use a realistic pollution prefix that itself
    contains stray `{` substrings to make the test load-bearing."""
    p = StreamParser(GEMINI_SPEC)
    polluted = (
        "Registering notification handlers for server 'augment-context-engine'. "
        "Capabilities: { logging: {}, tools: {} }"
        + json.dumps({"type": "init", "session_id": "fallback-id", "model": "g"})
    )
    p.feed_lines(
        [polluted, _gemini_event("message", role="assistant", content="ok")]
    )
    r = p.result()
    assert r.session_id == "fallback-id"
    assert r.agent_messages == "ok"


def test_brace_fallback_closes_after_first_accepted_event() -> None:
    """Once any event is accepted, fallback shuts. A later prefix-polluted
    line must NOT be silently fixed — that would drift parser semantics."""
    p = StreamParser(GEMINI_SPEC)
    polluted_later = (
        "log prefix " + json.dumps({"type": "message", "role": "assistant", "content": "x"})
    )
    p.feed_lines(
        [
            _gemini_event("init", session_id="s", model="m"),
            polluted_later,
        ]
    )
    r = p.result()
    assert r.session_id == "s"
    assert r.agent_messages == ""
    assert r.noise_log == [polluted_later]


def test_brace_fallback_stays_open_through_pollution_lines_until_init() -> None:
    """Codex round-2 BLOCKER regression: pollution lines arrive BEFORE init.

    Each pollution line may itself contain stray `{` substrings (e.g.
    `Capabilities: { logging: {}, tools: {} }`). The fallback must NOT be
    consumed by these — it stays open until a valid init event lands, which
    may itself be on a polluted line."""
    p = StreamParser(GEMINI_SPEC)
    polluted_init = (
        "garbage prefix"
        + json.dumps({"type": "init", "session_id": "late-init", "model": "g"})
    )
    p.feed_lines(
        [
            "Registering notification handlers for server 'augment-context-engine'. Capabilities: { logging: {}, tools: {} }",
            "Server 'augment-context-engine' has tools but did not declare 'listChanged'...",
            "Scheduling MCP context refresh...",
            polluted_init,
            _gemini_event("message", role="assistant", content="recovered"),
            _gemini_event("result", status="ok"),
        ]
    )
    r = p.result()
    assert r.session_id == "late-init"
    assert r.agent_messages == "recovered"
    assert len(r.noise_log) == 3
    assert r.noise_log[0].startswith("Registering notification handlers")


def test_brace_fallback_rejects_stray_braces_in_pollution_lines() -> None:
    """A pollution line containing `{}` substrings must NOT be promoted to
    an event. `{}` parses as a dict but is not a valid init event."""
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            "log noise: Capabilities: { logging: {}, tools: {} } and more text",
            _gemini_event("init", session_id="real-init", model="g"),
        ]
    )
    r = p.result()
    assert r.session_id == "real-init"
    assert r.all_messages[0]["session_id"] == "real-init"
    assert len(r.noise_log) == 1


def test_brace_fallback_walks_multiple_brace_positions_in_one_line() -> None:
    """Earlier `{` positions inside a polluted prefix may parse to junk dicts;
    the walker must continue to a later `{` that is the real init."""
    p = StreamParser(GEMINI_SPEC)
    polluted = (
        '{"unrelated": 1}garbage'  # idx 0 dict, missing type → reject
        + json.dumps({"type": "init", "session_id": "late", "model": "g"})
    )
    p.feed_lines([polluted, _gemini_event("result", status="ok")])
    r = p.result()
    assert r.session_id == "late"


# ---- Required-field validation (closes type-only loophole) ---------------


def test_gemini_init_missing_model_is_noise_not_event() -> None:
    """type='init' alone is not enough — a noise line forging that type with
    no `model` key must NOT produce a session_id."""
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            json.dumps({"type": "init", "session_id": "fake", "timestamp": "now"}),
        ]
    )
    r = p.result()
    assert r.session_id is None
    assert len(r.noise_log) == 1


def test_gemini_tool_result_status_required_payload_optional() -> None:
    """ToolResultEvent discriminator is `status`; output/error are optional
    payload per google-gemini/gemini-cli@main types.ts. `output:""` is a legal
    successful-no-op shape (regression: previously rejected as parse_error)."""
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            _gemini_event("init", session_id="s", model="m"),
            _gemini_event("tool_use", tool_name="grep", tool_id="t1", parameters={}),
            _gemini_event("tool_result", tool_id="t1", status="success", output=""),
            _gemini_event("tool_result", tool_id="t2", status="success"),
            _gemini_event(
                "tool_result", tool_id="t3", status="error",
                error={"type": "x", "message": "boom"},
            ),
            _gemini_event("tool_result", tool_id="t4", output="x"),       # missing status
            _gemini_event("tool_result", status="success", output="x"),    # missing tool_id
        ]
    )
    r = p.result()
    admitted = [e for e in r.all_messages if e["type"] == "tool_result"]
    assert [e["tool_id"] for e in admitted] == ["t1", "t2", "t3"]
    assert admitted[0]["output"] == ""
    assert len(r.noise_log) == 2


def test_gemini_tool_use_requires_parameters_empty_dict_legal() -> None:
    """ToolUseEvent.parameters is upstream-required (Record<string, unknown>);
    {} is legal for no-arg tools but absence is malformed."""
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            _gemini_event("init", session_id="s", model="m"),
            _gemini_event("tool_use", tool_name="ls", tool_id="t1", parameters={}),
            _gemini_event("tool_use", tool_name="ls", tool_id="t2"),  # missing parameters
        ]
    )
    r = p.result()
    admitted = [e for e in r.all_messages if e["type"] == "tool_use"]
    assert [e["tool_id"] for e in admitted] == ["t1"]
    assert len(r.noise_log) == 1


def test_gemini_message_empty_content_admitted_missing_content_rejected() -> None:
    """MessageEvent.content is upstream-required string; empty appears on
    streaming delta chunks. Missing key is still malformed. Extractor filters
    empty deltas so agent_messages is unaffected."""
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            _gemini_event("init", session_id="s", model="m"),
            _gemini_event("message", role="assistant", content=""),
            _gemini_event("message", role="assistant", content="hello"),
            json.dumps({"type": "message", "role": "assistant"}),  # missing content key
        ]
    )
    r = p.result()
    admitted = [e for e in r.all_messages if e["type"] == "message"]
    assert [e.get("content") for e in admitted] == ["", "hello"]
    assert r.agent_messages == "hello"
    assert len(r.noise_log) == 1


def test_gemini_error_event_recorded() -> None:
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            _gemini_event("init", session_id="s", model="m"),
            _gemini_event("error", severity="fatal", message="quota exhausted"),
        ]
    )
    r = p.result()
    assert len(r.error_events) == 1
    assert r.error_events[0]["message"] == "quota exhausted"


# ---- Whitelist rejection --------------------------------------------------


def test_gemini_unknown_type_goes_to_noise() -> None:
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            json.dumps({"type": "telemetry", "session_id": "x", "model": "y"}),
            _gemini_event("init", session_id="real", model="m"),
        ]
    )
    r = p.result()
    assert r.session_id == "real"
    assert len(r.noise_log) == 1


def test_invalid_json_goes_to_noise_not_parse_error() -> None:
    p = StreamParser(GEMINI_SPEC)
    p.feed_line("{not valid json")
    p.feed_line(_gemini_event("init", session_id="s", model="m"))
    r = p.result()
    assert r.session_id == "s"
    assert r.noise_log == ["{not valid json"]


def test_json_array_or_scalar_is_noise() -> None:
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(['[1,2,3]', '"hello"', "42"])
    r = p.result()
    assert r.session_id is None
    assert len(r.noise_log) == 3


# ---- Codex events ----------------------------------------------------------


def test_codex_thread_started_extracts_thread_id() -> None:
    p = StreamParser(CODEX_SPEC)
    p.feed_line(_codex_event("thread.started", thread_id="019d-uuid"))
    r = p.result()
    assert r.session_id == "019d-uuid"


def test_session_id_property_exposes_capture_mid_stream() -> None:
    """Runner needs to read session_id BEFORE terminal so it can emit a
    thread_id milestone for dispatcher's rollout recovery path
    (docs/bugs/rollout-recovery.md)."""
    p = StreamParser(CODEX_SPEC)
    assert p.session_id is None
    p.feed_line(_codex_event("thread.started", thread_id="019d-uuid"))
    assert p.session_id == "019d-uuid"
    # Subsequent events do not clobber the latch.
    p.feed_line(_codex_event("turn.started"))
    assert p.session_id == "019d-uuid"


def test_session_id_property_unset_when_init_missing_field() -> None:
    p = StreamParser(CODEX_SPEC)
    # Required-field validation rejects this; init not admitted.
    p.feed_line(json.dumps({"type": "thread.started"}))
    assert p.session_id is None


def test_codex_item_completed_yields_message_text_via_nested_item() -> None:
    """§4.4 codex bullet — agent text arrives nested as item.completed → item.text."""
    p = StreamParser(CODEX_SPEC)
    p.feed_lines(
        [
            _codex_event("thread.started", thread_id="t1"),
            _codex_event("turn.started"),
            _codex_event("item.completed", item={"text": "hello", "type": "agent_message"}),
            _codex_event("item.completed", item={"text": "world", "type": "agent_message"}),
            _codex_event("turn.completed"),
        ]
    )
    r = p.result()
    assert r.agent_messages == "hello\nworld"
    assert r.saw_terminal is True


def test_codex_reconnecting_error_is_transient_not_failure() -> None:
    """§4.4 step 6 — `Reconnecting... N/M` is SSE retry bookkeeping."""
    p = StreamParser(CODEX_SPEC)
    p.feed_lines(
        [
            _codex_event("thread.started", thread_id="t1"),
            _codex_event("error", message="Reconnecting... 1/5"),
            _codex_event("error", message="Reconnecting... 2/5"),
            _codex_event("item.completed", item={"type": "agent_message", "text": "recovered"}),
            _codex_event("turn.completed"),
        ]
    )
    r = p.result()
    assert r.agent_messages == "recovered"
    assert len(r.transient_lines) == 2
    assert r.error_events == []
    assert r.saw_terminal is True


def test_transient_count_tracks_only_siphoned_lines() -> None:
    """Runner compares `transient_count` across feed_line to decide whether a
    stdout line is tick-eligible for the pre_terminal_idle watchdog."""
    p = StreamParser(CODEX_SPEC)
    assert p.transient_count == 0
    p.feed_line(_codex_event("thread.started", thread_id="t1"))
    assert p.transient_count == 0
    p.feed_line(_codex_event("error", message="Reconnecting... 1/5"))
    assert p.transient_count == 1
    p.feed_line("not json at all")            # noise, not transient
    p.feed_line(_codex_event("error", message="boom"))  # real error, not transient
    assert p.transient_count == 1


def test_codex_non_agent_message_items_excluded_from_agent_messages() -> None:
    """Regression: M1 parser (commit 2c5664d) admitted any item.text into
    agent_messages, leaking codex `reasoning` payloads. Whitelist must filter
    by item.type == agent_message; reasoning / mcp_tool_call / unknown types
    must not contribute to assistant text."""
    p = StreamParser(CODEX_SPEC)
    p.feed_lines(
        [
            _codex_event("thread.started", thread_id="t1"),
            _codex_event("turn.started"),
            _codex_event("item.completed", item={"type": "reasoning", "text": "**Thinking**\nstep-by-step..."}),
            _codex_event("item.completed", item={"type": "mcp_tool_call", "text": "noise", "tool": "ace"}),
            _codex_event("item.completed", item={"type": "agent_message", "text": "final answer"}),
            _codex_event("turn.completed"),
        ]
    )
    r = p.result()
    assert r.agent_messages == "final answer"
    assert r.saw_terminal is True
    assert r.error_events == []


def test_codex_item_started_missing_item_is_noise() -> None:
    """`item.started` requires non-empty `item` field. Malformed starts
    pollute `pr.all_messages` and weaken downstream extractor invariants
    if admitted; route them to `noise_log` instead."""
    p = StreamParser(CODEX_SPEC)
    p.feed_lines(
        [
            _codex_event("thread.started", thread_id="t1"),
            _codex_event("turn.started"),
            _codex_event("item.started"),  # missing item
            _codex_event("item.started", item={"id": "item_0", "type": "mcp_tool_call"}),
            _codex_event("item.completed", item={"id": "item_0", "type": "mcp_tool_call"}),
            _codex_event("turn.completed"),
        ]
    )
    r = p.result()
    started_events = [e for e in r.all_messages if e["type"] == "item.started"]
    assert len(started_events) == 1
    assert started_events[0]["item"]["id"] == "item_0"
    assert r.noise_log == ['{"type": "item.started"}']


def test_codex_item_completed_missing_item_is_noise() -> None:
    """Backfill: the `item.completed` required-field rule existed since M1
    but had no parser-level regression test pinning the noise-routing path."""
    p = StreamParser(CODEX_SPEC)
    p.feed_lines(
        [
            _codex_event("thread.started", thread_id="t1"),
            _codex_event("turn.started"),
            _codex_event("item.completed"),  # missing item
            _codex_event("item.completed", item={"type": "agent_message", "text": "ok"}),
            _codex_event("turn.completed"),
        ]
    )
    r = p.result()
    completed_events = [e for e in r.all_messages if e["type"] == "item.completed"]
    assert len(completed_events) == 1
    assert r.agent_messages == "ok"
    assert r.noise_log == ['{"type": "item.completed"}']


def test_codex_genuine_error_event_recorded() -> None:
    p = StreamParser(CODEX_SPEC)
    p.feed_lines(
        [
            _codex_event("thread.started", thread_id="t1"),
            _codex_event(
                "error",
                message="unexpected status 503 Service Unavailable: No available providers",
            ),
        ]
    )
    r = p.result()
    assert len(r.error_events) == 1
    assert "503" in r.error_events[0]["message"]
    assert r.transient_lines == []


# ---- noise_log cap --------------------------------------------------------


def test_noise_log_capped_at_100() -> None:
    p = StreamParser(GEMINI_SPEC)
    for i in range(150):
        p.feed_line(f"noise line {i}")
    r = p.result()
    assert len(r.noise_log) == NOISE_LOG_CAP
    # First 100 retained; later ones dropped (FIFO discard on overflow).
    assert r.noise_log[0] == "noise line 0"
    assert r.noise_log[-1] == f"noise line {NOISE_LOG_CAP - 1}"


# ---- Multi-message join (H2) ---------------------------------------------


def test_multi_message_join_uses_newline_not_empty_string() -> None:
    """mcp-audit/README.md H2 — successful codex responses concatenate
    multiple item.completed messages; join with `\\n`."""
    p = StreamParser(CODEX_SPEC)
    p.feed_lines(
        [
            _codex_event("thread.started", thread_id="t1"),
            _codex_event("item.completed", item={"type": "agent_message", "text": "first paragraph"}),
            _codex_event("item.completed", item={"type": "agent_message", "text": "second paragraph"}),
            _codex_event("item.completed", item={"type": "agent_message", "text": "third paragraph"}),
            _codex_event("turn.completed"),
        ]
    )
    r = p.result()
    assert r.agent_messages == "first paragraph\nsecond paragraph\nthird paragraph"
    assert "\n" in r.agent_messages


# ---- Session-id stability under repeat init ------------------------------


def test_first_init_wins_over_later_init() -> None:
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            _gemini_event("init", session_id="first", model="m"),
            _gemini_event("init", session_id="second", model="m"),
        ]
    )
    r = p.result()
    assert r.session_id == "first"


# ---- saw_terminal semantics ----------------------------------------------


def test_saw_terminal_false_when_stream_truncated_before_result() -> None:
    p = StreamParser(GEMINI_SPEC)
    p.feed_lines(
        [
            _gemini_event("init", session_id="s", model="m"),
            _gemini_event("message", role="assistant", content="partial"),
        ]
    )
    r = p.result()
    assert r.saw_terminal is False
    assert r.agent_messages == "partial"
