"""§4.4 stream parser. Pure: takes lines, returns ParseResult. No IO.

Design contract:
- type whitelist + per-type required-field validation (§4.4 steps 2–4)
- non-matching lines → `noise_log` (cap 100), NOT a wrapper failure
- prefix-`{` substring fallback (CCG 6988f64) — kept open until any event is
  accepted, since augment-context-engine spam can arrive on multiple lines
  before the real init line and itself contains `{` substrings. Fallback
  candidates must satisfy init-event validation, otherwise the noise's stray
  `{}` would be admitted as a "session" (codex round-2 BLOCKER fix).
- codex `Reconnecting... N/M` events siphoned to `transient_lines`,
  preserved verbatim for stderr_tail; not classified as `codex_error`
- multi-message join uses `\n` (mcp-audit/README.md H2)
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .events import EventSpec, is_transient_codex_error, validate_required_fields

NOISE_LOG_CAP = 100


@dataclass
class ParseResult:
    session_id: str | None
    agent_messages: str
    all_messages: list[dict]
    noise_log: list[str]
    transient_lines: list[str]
    saw_terminal: bool
    error_events: list[dict]


class StreamParser:
    """Stateful per-line consumer; finalize via `.result()`."""

    def __init__(self, spec: EventSpec) -> None:
        self.spec = spec
        self._session_id: str | None = None
        self._events: list[dict] = []
        self._messages: list[str] = []
        self._delta_buffer: list[str] = []
        self._noise: list[str] = []
        self._transient: list[str] = []
        self._error_events: list[dict] = []
        self._saw_terminal = False
        self._prefix_fallback_open = True

    def feed_line(self, raw: str) -> None:
        line = raw.rstrip("\r\n")
        if not line.strip():
            return

        event = self._try_parse_json(
            line, allow_prefix_strip=self._prefix_fallback_open
        )
        if event is None:
            self._add_noise(line)
            return

        etype = event.get("type")
        if not isinstance(etype, str) or etype not in self.spec.event_types:
            self._add_noise(line)
            return

        if not validate_required_fields(self.spec, etype, event):
            self._add_noise(line)
            return

        if self.spec.name == "codex" and is_transient_codex_error(event):
            self._transient.append(line)
            return

        self._events.append(event)
        # Once any event is accepted, the upstream stream is well-formed; no
        # further prefix-stripping is allowed (avoids drift on later lines).
        self._prefix_fallback_open = False

        if etype == self.spec.init_event_type and self._session_id is None:
            sid = event.get(self.spec.session_id_field)
            if isinstance(sid, str) and sid:
                self._session_id = sid

        if etype in self.spec.error_event_types:
            self._error_events.append(event)

        if etype in self.spec.terminal_event_types:
            self._saw_terminal = True

        text = self.spec.extract_message_text(event)
        if self.spec.is_streaming_delta(event):
            if text:
                self._delta_buffer.append(text)
            return

        self._flush_delta_buffer()
        if text:
            self._messages.append(text)

    def feed_lines(self, lines) -> None:
        for line in lines:
            self.feed_line(line)

    @property
    def saw_terminal(self) -> bool:
        """True iff a `turn.completed`/`thread.completed` (codex) or `result`
        (gemini) has been admitted. Runner watches this to fire the §3.4 #3
        post-terminal SIGTERM chain (codex 0.80 `--json` non-exit fix)."""
        return self._saw_terminal

    @property
    def transient_count(self) -> int:
        """Lines siphoned into `transient_lines` so far (codex
        `Reconnecting... N/M`). Runner compares it across `feed_line` so
        reconnect chatter does not refresh the dispatcher's pre_terminal_idle
        watchdog — see docs/bugs/codex-transient-retry-storm.md.
        Package-internal."""
        return len(self._transient)

    @property
    def session_id(self) -> str | None:
        """Captured on first admitted init event (`thread.started` for codex,
        `init` for gemini). Exposed so runner can emit a thread_id milestone
        the moment the parser sees it — dispatcher needs it before terminal
        to drive rollout recovery on pre_terminal_idle. Package-internal."""
        return self._session_id

    def _flush_delta_buffer(self) -> None:
        if self._delta_buffer:
            self._messages.append("".join(self._delta_buffer))
            self._delta_buffer.clear()

    def _materialized_messages(self) -> list[str]:
        if not self._delta_buffer:
            return list(self._messages)
        return [*self._messages, "".join(self._delta_buffer)]

    def result(self) -> ParseResult:
        return ParseResult(
            session_id=self._session_id,
            agent_messages="\n".join(self._materialized_messages()),
            all_messages=list(self._events),
            noise_log=list(self._noise),
            transient_lines=list(self._transient),
            saw_terminal=self._saw_terminal,
            error_events=list(self._error_events),
        )

    def _try_parse_json(self, line: str, *, allow_prefix_strip: bool) -> dict | None:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            return obj
        if not allow_prefix_strip:
            return None
        # Walk every `{` after position 0 (idx=0 already attempted above);
        # accept the first candidate that looks like a valid init event. This
        # prevents stray `{}` substrings inside log lines from being admitted.
        idx = line.find("{", 1)
        while idx > 0:
            try:
                obj2 = json.loads(line[idx:])
            except json.JSONDecodeError:
                obj2 = None
            if isinstance(obj2, dict) and self._is_valid_init_fallback(obj2):
                return obj2
            idx = line.find("{", idx + 1)
        return None

    def _is_valid_init_fallback(self, event: dict) -> bool:
        etype = event.get("type")
        if not isinstance(etype, str) or etype != self.spec.init_event_type:
            return False
        return validate_required_fields(self.spec, etype, event)

    def _add_noise(self, line: str) -> None:
        if len(self._noise) < NOISE_LOG_CAP:
            self._noise.append(line)
