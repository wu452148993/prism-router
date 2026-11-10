"""§4.4 event-type whitelist + per-type required-field rules.

Both backends emit newline-delimited JSON. Strict typing ruled out by design:
- type whitelist alone is insufficient (noise lines forge `{"type":"error",...}`)
- required-field validation closes that gap

Required-field schema is `dict[event_type, list[set[str]]]` — list of AND-groups.
Each group is OR'd internally: the event must contain at least one key from each
group with a truthy value. Example: `tool_result` requires `tool_id` AND
(`output` OR `error`) → `[{"tool_id"}, {"output", "error"}]`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

# Codex transient SSE retries; preserved verbatim in stderr_tail per §4.4 step 6.
RECONNECTING_RE = re.compile(r"^Reconnecting\.\.\.\s+\d+/\d+")


@dataclass(frozen=True)
class EventSpec:
    """Per-backend parser configuration. All fields are pure data / pure functions."""

    name: str
    event_types: frozenset[str]
    required_fields: dict[str, list[frozenset[str]]]
    init_event_type: str
    session_id_field: str
    error_event_types: frozenset[str]
    terminal_event_types: frozenset[str]
    extract_message_text: Callable[[dict], str | None]
    is_streaming_delta: Callable[[dict], bool]


def _truthy(event: dict, key: str) -> bool:
    val = event.get(key)
    return val not in (None, "", [], {}, ())


# (spec_name, event_type, key) tuples whose value may legitimately be the empty
# form per upstream types but whose *absence* is still malformed. Without this
# allow-list, _truthy() conflates "empty but present" with "missing" and routes
# valid events into noise_log → spurious parse_error envelopes. Verified against
# google-gemini/gemini-cli@main packages/core/src/output/types.ts (sha 1d4159a):
#   - message.content: required string, empty appears on streaming delta chunks
#   - tool_use.parameters: required Record<string, unknown>, {} for no-arg tools
_EMPTY_STR_LEGAL: frozenset[tuple[str, str, str]] = frozenset({
    ("gemini", "message", "content"),
})
_EMPTY_DICT_LEGAL: frozenset[tuple[str, str, str]] = frozenset({
    ("gemini", "tool_use", "parameters"),
})


def _field_satisfied(spec: EventSpec, event_type: str, event: dict, key: str) -> bool:
    if key not in event:
        return False
    val = event[key]
    locator = (spec.name, event_type, key)
    if val == "" and locator in _EMPTY_STR_LEGAL:
        return True
    if val == {} and locator in _EMPTY_DICT_LEGAL:
        return True
    return _truthy(event, key)


def validate_required_fields(spec: EventSpec, event_type: str, event: dict) -> bool:
    """All AND-groups satisfied; each group satisfied iff ≥1 key is present-and-valid."""
    groups = spec.required_fields.get(event_type)
    if groups is None:
        return True  # type whitelisted but no rules pinned yet
    return all(any(_field_satisfied(spec, event_type, event, k) for k in group) for group in groups)


# Gemini stream-json events (VERIFIED §9 V3 against
# google-gemini/gemini-cli packages/core/src/output/types.ts @ e9171fd7).
GEMINI_EVENT_TYPES: frozenset[str] = frozenset(
    {"init", "message", "tool_use", "tool_result", "error", "result"}
)

GEMINI_REQUIRED_FIELDS: dict[str, list[frozenset[str]]] = {
    "init": [frozenset({"session_id"}), frozenset({"model"})],
    "message": [frozenset({"role"}), frozenset({"content"})],
    "tool_use": [frozenset({"tool_name"}), frozenset({"tool_id"}), frozenset({"parameters"})],
    "tool_result": [frozenset({"tool_id"}), frozenset({"status"})],
    "error": [frozenset({"severity"}), frozenset({"message"})],
    "result": [frozenset({"status"})],
}


def _gemini_message_text(event: dict) -> str | None:
    if event.get("type") != "message":
        return None
    if event.get("role") != "assistant":
        return None
    content = event.get("content")
    if isinstance(content, str) and content:
        return content
    return None


def _gemini_is_streaming_delta(event: dict) -> bool:
    return event.get("type") == "message" and event.get("delta") is True


GEMINI_SPEC = EventSpec(
    name="gemini",
    event_types=GEMINI_EVENT_TYPES,
    required_fields=GEMINI_REQUIRED_FIELDS,
    init_event_type="init",
    session_id_field="session_id",
    error_event_types=frozenset({"error"}),
    terminal_event_types=frozenset({"result"}),
    extract_message_text=_gemini_message_text,
    is_streaming_delta=_gemini_is_streaming_delta,
)


# Codex events. §4.4: required fields stay lenient until we observe v1 traffic;
# pinned only on extraction-critical fields. Tighten as `calls.json` exemplars
# accumulate (see design §4.4 step 2 codex bullet).
CODEX_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "thread.started",
        "turn.started",
        "item.started",
        "item.completed",
        "turn.completed",
        "thread.completed",
        "error",
        "fail",
    }
)

CODEX_REQUIRED_FIELDS: dict[str, list[frozenset[str]]] = {
    "thread.started": [frozenset({"thread_id"})],
    "item.started": [frozenset({"item"})],
    "item.completed": [frozenset({"item"})],
    "error": [frozenset({"message"})],
    "fail": [frozenset({"message"})],
}


def _codex_message_text(event: dict) -> str | None:
    """Assistant text only. `item.completed` carries multiple flavours
    (agent_message, reasoning, mcp_tool_call, …) and `reasoning.text`
    must not leak into `agent_messages` (old wrapper filtered it; M1 regressed)."""
    if event.get("type") != "item.completed":
        return None
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    if item.get("type") != "agent_message":
        return None
    text = item.get("text")
    if isinstance(text, str) and text:
        return text
    return None


def _codex_is_streaming_delta(_event: dict) -> bool:
    return False


CODEX_SPEC = EventSpec(
    name="codex",
    event_types=CODEX_EVENT_TYPES,
    required_fields=CODEX_REQUIRED_FIELDS,
    init_event_type="thread.started",
    session_id_field="thread_id",
    error_event_types=frozenset({"error", "fail"}),
    # F1 verified 2026-05-09 vs openai/codex (pinned commit d807d44a; cross-
    # checked against main). `codex exec --json` emits ≤1 task_id-matching
    # turn.completed per exec invocation. Safety lives in the exec event loop,
    # not the enum shape. In codex-rs/exec/src/lib.rs (typed and legacy-bridge
    # paths):
    #   - turn-scoped server notifications are filtered by the exec's task_id
    #     before forwarding (typed path: `should_process_notification(notification,
    #     primary_thread_id, task_id)`; legacy bridge: `if turn_id != task_id
    #     { continue }`). task_id is bound once per exec invocation.
    #   - `event_processor_with_jsonl_output.rs::process_server_notification`
    #     returns CodexStatus::InitiateShutdown on TurnCompleted/TurnFailed/
    #     Interrupted; the loop awaits `request_shutdown(...)` then `break`s
    #     and never polls `next_event()` again.
    # So a duplicate same-task TurnCompleted on the channel cannot reach
    # stdout. runner.py post_terminal_kill / idle_after_terminal arming on the
    # first saw_terminal therefore cannot kill a healthy long run mid-session.
    # `thread.completed` does not appear in the ThreadEvent enum at d807d44a
    # or verified main — kept here as defensive forward-compat, harmless.
    terminal_event_types=frozenset({"thread.completed", "turn.completed"}),
    extract_message_text=_codex_message_text,
    is_streaming_delta=_codex_is_streaming_delta,
)


def is_transient_codex_error(event: dict) -> bool:
    """§4.4 step 6 — codex `Reconnecting... N/M` events are not real failures."""
    if event.get("type") not in ("error", "fail"):
        return False
    msg = event.get("message")
    if not isinstance(msg, str):
        return False
    return RECONNECTING_RE.match(msg) is not None
