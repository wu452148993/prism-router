"""§4.4 server-level tests: smoke probe gating + envelope construction.

These run as default unit tests (not integration-marked) because they use
`fastmcp.Client` with in-memory transport — no real codex/gemini binary needed
once `smoke_probe` is monkeypatched.

Real-binary integration coverage lives in `tests/integration/test_smoke.py`
(M2) and the upcoming M4 fixtures replay.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import time
from pathlib import Path

import anyio
import pytest
from fastmcp import Client

from prism_router import server as server_mod
from prism_router._liveness import LivenessEgress
from prism_router.at_escape import AT_ESCAPE_NOTE
from prism_router.backend_gemini import GeminiBackend
from prism_router.config import ServerConfig
from prism_router.dispatcher import STDIN_PREFLIGHT_CAP, DispatchResult
from prism_router.errors import make_error
from prism_router.events import GEMINI_SPEC
from prism_router.parser import ParseResult, StreamParser
from prism_router.protocol import (
    AGENT_SIDE,
    ORCHESTRATOR_SIDE,
    OVERSIZED_THRESHOLD_BYTES,
    inject_agent_side,
)
from prism_router.runner import KillStep, RunOutcome
from prism_router.server import build_server


class _FakeDispatcher:
    """Replaces Dispatcher in tests; the constructor accepts the same kwargs
    (or none) and `invoke` returns whatever the test wired up.
    """

    def __init__(self, _result_or_callable=None, _seen: dict | None = None, **_):
        self._r = _result_or_callable
        self._seen = _seen

    @classmethod
    def factory(cls, result_or_callable, seen=None):
        def _make(**_):
            return cls(_result_or_callable=result_or_callable, _seen=seen)
        return _make

    async def invoke(self, *, backend_name, prompt, cfg):
        if self._seen is not None:
            self._seen["backend_name"] = backend_name
            self._seen["prompt"] = prompt
            self._seen["cfg"] = cfg
        r = self._r
        if callable(r):
            r = r(backend_name=backend_name, prompt=prompt, cfg=cfg)
        return r


# ---------- C7 conditional registration ----------

@pytest.fixture
def force_probes(monkeypatch):
    """Stub smoke_probe with caller-controlled per-cmd boolean map."""
    def _apply(codex_ok: bool, gemini_ok: bool) -> None:
        def fake(cmd: str, *, timeout_sec: float = 5.0) -> bool:
            return {"codex": codex_ok, "gemini": gemini_ok}.get(cmd, False)
        monkeypatch.setattr(server_mod, "smoke_probe", fake)
    return _apply


async def _list_tool_names(mcp) -> set[str]:
    async with Client(mcp) as client:
        tools = await client.list_tools()
    return {t.name for t in tools}


async def test_both_backends_register_when_probes_pass(force_probes):
    force_probes(codex_ok=True, gemini_ok=True)
    names = await _list_tool_names(build_server())
    assert "codex" in names
    assert "gemini" in names


async def test_only_gemini_when_codex_probe_fails(force_probes):
    force_probes(codex_ok=False, gemini_ok=True)
    names = await _list_tool_names(build_server())
    assert "codex" not in names
    assert "gemini" in names


async def test_only_codex_when_gemini_probe_fails(force_probes):
    force_probes(codex_ok=True, gemini_ok=False)
    names = await _list_tool_names(build_server())
    assert "codex" in names
    assert "gemini" not in names


async def test_no_tools_when_both_probes_fail(force_probes):
    force_probes(codex_ok=False, gemini_ok=False)
    names = await _list_tool_names(build_server())
    assert names == set()


# ---------- BLOCKER 1 regression: CODEX_REQUIRE_APPROVAL → SpawnConfig.yolo ----------

@pytest.mark.parametrize(
    ("require_approval", "tool_yolo", "expected_yolo"),
    [
        (False, False, True),   # default: env says bypass, caller didn't override → bypass
        (True, False, False),   # operator wants approvals, caller didn't override → require
        (True, True, True),     # caller explicitly bypass overrides operator default
        (False, True, True),
    ],
)
async def test_codex_approval_config_controls_default_bypass(
    monkeypatch, tmp_path: Path,
    require_approval: bool, tool_yolo: bool, expected_yolo: bool,
):
    seen: dict = {}
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
    monkeypatch.setattr(
        server_mod, "Dispatcher",
        _FakeDispatcher.factory(DispatchResult(outcome=_success_outcome()), seen=seen),
    )

    result = await server_mod._invoke_codex(
        prompt="hi", cd=tmp_path, sandbox="read-only", session_id="",
        skip_git_repo_check=True, return_all_messages=False,
        model="", profile="", image=None, yolo=tool_yolo,
        require_approval=require_approval,
    )
    assert result["success"] is True
    assert seen["cfg"].yolo is expected_yolo


@pytest.mark.parametrize(("profile", "dispatched"), [("", False), ("p", True)])
async def test_codex_auth_preflight_skipped_for_profile_calls(
    monkeypatch, tmp_path: Path, profile: str, dispatched: bool,
):
    """A `--profile` overlay can switch the provider, so a base-config verdict
    must not block it (docs/bugs/codex-preflight-custom-provider.md)."""
    seen: dict = {}
    calls: list[None] = []

    def preflight():
        calls.append(None)
        return make_error("auth_401", "pre_flight", retryable=False, raw="base config")

    monkeypatch.setattr(server_mod, "preflight_codex_auth", preflight)
    monkeypatch.setattr(
        server_mod, "Dispatcher",
        _FakeDispatcher.factory(DispatchResult(outcome=_success_outcome()), seen=seen),
    )
    result = await server_mod._invoke_codex(
        prompt="hi", cd=tmp_path, sandbox="read-only", session_id="",
        skip_git_repo_check=True, return_all_messages=False,
        model="", profile=profile, image=None, yolo=False, require_approval=True,
    )
    assert result["success"] is dispatched
    assert ("cfg" in seen) is dispatched
    assert len(calls) == (0 if profile else 1)


@pytest.mark.parametrize(
    ("require_approval", "tool_yolo", "expected_yolo"),
    [
        (False, False, True),   # operator opted back into bypass → -y
        (True, False, False),   # shipped default: no caller override → no -y
        (True, True, True),     # caller explicitly asks for write/shell tools
        (False, True, True),
    ],
)
async def test_gemini_approval_config_controls_default_bypass(
    monkeypatch, tmp_path: Path,
    require_approval: bool, tool_yolo: bool, expected_yolo: bool,
):
    seen: dict = {}
    monkeypatch.setattr(
        server_mod, "Dispatcher",
        _FakeDispatcher.factory(DispatchResult(outcome=_success_outcome()), seen=seen),
    )
    result = await server_mod._invoke_gemini(
        prompt="hi", cd=tmp_path, session_id="", return_all_messages=False,
        model="", sandbox=False, gemini_default_model="gemini-test",
        yolo=tool_yolo, require_approval=require_approval,
    )
    assert result["success"] is True
    assert seen["cfg"].yolo is expected_yolo


async def test_gemini_default_env_emits_no_yolo_flag(
    monkeypatch, force_probes, tmp_path: Path,
):
    """Chain guard through the PUBLIC tool: default env → no `-y` in argv.

    Goes via `build_server` + `Client` so the closure capture in the tool
    body is covered too — the matrix above only exercises `_invoke_gemini`
    directly, and `test_argv` only SpawnConfig → argv, leaving the
    registered-tool hop untested.
    docs/bugs/gemini-default-write-capability.md
    """
    seen: dict = {}
    force_probes(codex_ok=False, gemini_ok=True)
    monkeypatch.setattr(
        server_mod, "Dispatcher",
        _FakeDispatcher.factory(DispatchResult(outcome=_success_outcome()), seen=seen),
    )
    mcp = build_server(ServerConfig.from_env({}))
    async with Client(mcp) as client:
        await client.call_tool("gemini", {"PROMPT": "hi", "cd": str(tmp_path)})
    assert seen["cfg"].yolo is False
    assert "-y" not in GeminiBackend().build_args(seen["cfg"])


# ---------- envelope construction (success + error paths) ----------

def _success_outcome(msg: str = "hello") -> RunOutcome:
    pr = ParseResult(
        session_id="sid-success",
        agent_messages=msg,
        all_messages=[
            {"type": "thread.started", "thread_id": "sid-success"},
            {"type": "turn.completed"},
        ],
        noise_log=[],
        transient_lines=[],
        saw_terminal=True,
        error_events=[],
    )
    return RunOutcome(
        parse_result=pr, stderr_tail="", kill_chain=[], returncode=0, timed_out=False,
    )


def _post_terminal_success_outcome(msg: str = "hello") -> RunOutcome:
    """Clean stream → wrapper post-terminal kill → rc=1 with content intact.
    Mirrors the bug repro: classify must surface this as success, not codex_error."""
    return dataclasses.replace(
        _success_outcome(msg),
        kill_chain=[KillStep(action="TerminateProcess", reason="post_terminal", elapsed=1.0)],
        returncode=1,
    )


@pytest.mark.parametrize("backend_name", ["codex", "gemini"])
async def test_invoke_post_terminal_kill_with_content_returns_success(
    monkeypatch, tmp_path: Path, backend_name: str,
):
    monkeypatch.setattr(
        server_mod, "Dispatcher",
        _FakeDispatcher.factory(
            DispatchResult(outcome=_post_terminal_success_outcome("PRISM_OK")),
        ),
    )
    if backend_name == "codex":
        monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
        result = await server_mod._invoke_codex(
            prompt="hi", cd=tmp_path, sandbox="read-only", session_id="",
            skip_git_repo_check=True, return_all_messages=False,
            model="", profile="", image=None, yolo=False, require_approval=True,
        )
    else:
        result = await server_mod._invoke_gemini(
            prompt="hi", cd=tmp_path, session_id="", return_all_messages=False,
            model="", sandbox=False, gemini_default_model="gemini-test",
        )

    assert result["success"] is True
    assert result["agent_messages"] == "PRISM_OK"
    assert result["error"] is None


def test_envelope_success_minimal():
    """return_all_messages=False + no error → all_messages=None,
    diagnostics=None, success=True."""
    env = server_mod._envelope(
        backend_name="codex",
        error=None,
        outcome=_success_outcome(),
        return_all_messages=False,
    )
    assert env["success"] is True
    assert env["SESSION_ID"] == "sid-success"
    assert env["agent_messages"] == "hello"
    assert env["truncated"] is False
    assert env["error"] is None
    assert env["all_messages"] is None
    assert env["diagnostics"] is None


def test_envelope_success_with_return_all_messages():
    """return_all_messages=True populates all_messages AND diagnostics."""
    env = server_mod._envelope(
        backend_name="gemini",
        error=None,
        outcome=_success_outcome(),
        return_all_messages=True,
    )
    assert env["all_messages"] is not None
    assert len(env["all_messages"]) == 2
    assert env["diagnostics"] is not None
    assert env["diagnostics"]["backend_chosen"] == "gemini"


def test_envelope_error_forces_diagnostics_even_when_flag_off():
    """error not None → diagnostics populated regardless of return_all_messages."""
    err = make_error("proxy_503", "stream", retryable=True, raw="503", stderr_tail=None)
    env = server_mod._envelope(
        backend_name="codex", error=err, outcome=_success_outcome(),
        return_all_messages=False,
    )
    assert env["success"] is False
    assert env["error"] == err
    assert env["diagnostics"] is not None
    assert env["all_messages"] is None  # flag still gates this field


def test_envelope_pre_flight_no_outcome():
    """auth pre-flight returns envelope with no spawn (outcome=None)."""
    err = make_error("auth_401", "pre_flight", retryable=False, raw="missing key")
    env = server_mod._envelope(
        backend_name="codex", error=err, outcome=None, return_all_messages=False,
    )
    assert env["success"] is False
    assert env["SESSION_ID"] is None
    assert env["agent_messages"] == ""
    assert env["truncated"] is False
    assert env["error"]["code"] == "auth_401"
    assert env["all_messages"] is None
    assert env["diagnostics"] is None


def _recovered_outcome(msg: str = "rollout text") -> RunOutcome:
    """Mirrors dispatcher._synthesize_recovered_outcome — `returncode=0`,
    `saw_terminal=False` (honest), session_id=thread_id."""
    pr = ParseResult(
        session_id="recovered-thread-uuid",
        agent_messages=msg,
        all_messages=[],
        noise_log=[],
        transient_lines=[],
        saw_terminal=False,
        error_events=[],
    )
    return RunOutcome(
        parse_result=pr, stderr_tail="", kill_chain=[],
        returncode=0, timed_out=False,
    )


def test_envelope_recovered_success_surfaces_diagnostics_without_flag():
    """Default `return_all_messages=False` recovered-success path: caller
    must see `recovered_from_rollout=True` even though no flag is set.
    Without forcing diagnostics, callers cannot tell a rollout-recovered
    success from a normal one — defeats the whole observability story.
    docs/bugs/rollout-recovery.md."""
    extra = {
        "recovered_from_rollout": True,
        "rollout_path": "C:/x/.codex/sessions/2026/05/18/rollout-...-uuid.jsonl",
        "max_stdout_silence_s": 1200.4,
        # Rollout-liveness observability (docs/bugs/rollout-liveness-tick.md)
        # — the B-class fingerprint companions of recovered_from_rollout:
        "rollout_ticks": 118,
        "rollout_watched_s": 1400.0,
        "last_rollout_growth_age_s": 1207.3,
        "rollout_observation_stale": True,
        # Internal-only keys that must NOT leak to envelope:
        "stderr_tail": "leaked",
        "transport_error": "leaked",
        "kill_reason": "pre_terminal_idle",
    }
    env = server_mod._envelope(
        backend_name="codex", error=None,
        outcome=_recovered_outcome("recovered message body"),
        return_all_messages=False,
        extra_diagnostics=extra,
    )
    assert env["success"] is True
    assert env["agent_messages"] == "recovered message body"
    assert env["diagnostics"] is not None
    diag = env["diagnostics"]
    assert diag["recovered_from_rollout"] is True
    assert diag["rollout_path"].endswith(".jsonl")
    assert diag["max_stdout_silence_s"] == 1200.4
    assert diag["rollout_ticks"] == 118
    assert diag["rollout_watched_s"] == 1400.0
    assert diag["last_rollout_growth_age_s"] == 1207.3
    assert diag["rollout_observation_stale"] is True
    # Internal fields must NOT have leaked.
    assert "stderr_tail" not in diag
    assert "transport_error" not in diag
    assert "kill_reason" not in diag


def test_envelope_normal_success_does_not_surface_recovery_fields():
    """Sanity: when extra_diagnostics is empty/None, the normal-success
    diagnostics-is-None path holds (we don't suddenly emit on every call)."""
    env = server_mod._envelope(
        backend_name="codex", error=None,
        outcome=_success_outcome("normal"),
        return_all_messages=False,
        extra_diagnostics=None,
    )
    assert env["diagnostics"] is None
    # Same when extra_diagnostics has only internal keys.
    env2 = server_mod._envelope(
        backend_name="codex", error=None,
        outcome=_success_outcome("normal"),
        return_all_messages=False,
        extra_diagnostics={"stderr_tail": "...", "kill_reason": "..."},
    )
    assert env2["diagnostics"] is None


def test_envelope_normal_success_with_telemetry_keys_does_not_force_diagnostics():
    """Round 5 BLOCKER regression: Dispatcher's normal-success path ALWAYS
    sets `max_stdout_silence_s` in DispatchResult.diagnostics (telemetry).
    The force-diagnostics gate in _envelope must NOT trigger on that alone —
    only `recovered_from_rollout=True` should force public emission.
    Otherwise every default-success envelope leaks diagnostics."""
    env = server_mod._envelope(
        backend_name="codex", error=None,
        outcome=_success_outcome("normal"),
        return_all_messages=False,
        # Mirror what Dispatcher actually emits on a normal happy-path call
        # (rollout-liveness keys included when the watchdog tracked the file).
        extra_diagnostics={
            "max_stdout_silence_s": 0.0, "saw_terminal_event": True,
            "rollout_ticks": 42, "rollout_watched_s": 900.0,
            "last_rollout_growth_age_s": 3.2,
            "rollout_observation_stale": True,
        },
    )
    assert env["diagnostics"] is None, env["diagnostics"]


async def test_invoke_codex_normal_success_does_not_force_diagnostics(
    monkeypatch, tmp_path: Path,
):
    """End-to-end via _invoke_codex: a normal happy-path Dispatcher result
    carrying its routine telemetry must not surface diagnostics on the
    default `return_all_messages=False` envelope."""
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
    monkeypatch.setattr(
        server_mod, "Dispatcher",
        _FakeDispatcher.factory(
            DispatchResult(
                outcome=_success_outcome("normal answer"),
                diagnostics={
                    "max_stdout_silence_s": 0.0,
                    "saw_terminal_event": True,
                },
            ),
        ),
    )
    result = await server_mod._invoke_codex(
        prompt="hi", cd=tmp_path, sandbox="read-only", session_id="",
        skip_git_repo_check=True, return_all_messages=False,
        model="", profile="", image=None, yolo=False, require_approval=True,
    )
    assert result["success"] is True
    assert result["diagnostics"] is None


async def test_invoke_codex_recovered_success_through_classify(
    monkeypatch, tmp_path: Path,
):
    """End-to-end through classify: a synthesized recovered outcome
    (rc=0, saw_terminal=False, empty kill_chain, populated session_id)
    must NOT trip classify's spawn_failed / no_session / etc branches.
    This is the R1 BLOCKER regression."""
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
    extra = {
        "recovered_from_rollout": True,
        "rollout_path": str(tmp_path / "rollout.jsonl"),
        "max_stdout_silence_s": 1200.0,
    }
    monkeypatch.setattr(
        server_mod, "Dispatcher",
        _FakeDispatcher.factory(
            DispatchResult(
                outcome=_recovered_outcome("recovered final answer"),
                diagnostics=extra,
            ),
        ),
    )
    result = await server_mod._invoke_codex(
        prompt="hi", cd=tmp_path, sandbox="read-only", session_id="",
        skip_git_repo_check=True, return_all_messages=False,
        model="", profile="", image=None, yolo=False, require_approval=True,
    )
    assert result["success"] is True
    assert result["error"] is None
    assert result["agent_messages"] == "recovered final answer"
    assert result["SESSION_ID"] == "recovered-thread-uuid"
    assert result["diagnostics"] is not None
    assert result["diagnostics"]["recovered_from_rollout"] is True


def test_envelope_truncates_oversize_agent_messages():
    pr = ParseResult(
        session_id="s",
        agent_messages="x" * 250_000,
        all_messages=[],
        noise_log=[],
        transient_lines=[],
        saw_terminal=True,
        error_events=[],
    )
    outcome = RunOutcome(
        parse_result=pr, stderr_tail="", kill_chain=[],
        returncode=0, timed_out=False,
    )
    env = server_mod._envelope(
        backend_name="codex", error=None, outcome=outcome, return_all_messages=False,
    )
    assert env["truncated"] is True
    assert len(env["agent_messages"]) == 100_000


@pytest.mark.parametrize("backend_name", ["codex", "gemini"])
@pytest.mark.parametrize(
    ("kill_reason", "expected_code"),
    [("drain_timeout", "drain_timeout"), ("idle_after_terminal", "idle_after_terminal")],
)
async def test_invoke_kill_reasons_propagate_to_envelope(
    monkeypatch, tmp_path: Path, kill_reason: str, expected_code: str, backend_name: str,
):
    """Plumbing guard: server.py must pass `kill_reasons` from RunOutcome
    into classify() at BOTH backend call sites, otherwise drain_timeout /
    idle_after_terminal silently route to success despite reaching the
    shutdown ladder. Regression for the round-3 codex-flagged silent revert."""
    chain = [KillStep(action="kill_alias", reason=kill_reason, elapsed=1.5)]
    outcome = dataclasses.replace(_success_outcome("partial"), kill_chain=chain)
    monkeypatch.setattr(
        server_mod, "Dispatcher",
        _FakeDispatcher.factory(DispatchResult(outcome=outcome)),
    )
    if backend_name == "codex":
        monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
        result = await server_mod._invoke_codex(
            prompt="hi", cd=tmp_path, sandbox="read-only", session_id="",
            skip_git_repo_check=True, return_all_messages=False,
            model="", profile="", image=None, yolo=False, require_approval=True,
        )
    else:
        result = await server_mod._invoke_gemini(
            prompt="hi", cd=tmp_path, session_id="", return_all_messages=False,
            model="", sandbox=False, gemini_default_model="gemini-test",
        )
    assert result["success"] is False
    assert result["error"] is not None
    assert result["error"]["code"] == expected_code


def test_diagnostics_includes_kill_chain_serialised():
    pr = ParseResult(
        session_id="s", agent_messages="", all_messages=[],
        noise_log=["n1"], transient_lines=[], saw_terminal=True, error_events=[],
    )
    chain = [KillStep(action="terminate", reason="overall_timeout", elapsed=1.5)]
    outcome = RunOutcome(
        parse_result=pr, stderr_tail="late line", kill_chain=chain,
        returncode=-9, timed_out=True,
    )
    err = make_error("timeout", "shutdown", retryable=True, raw="t", stderr_tail="late line")
    env = server_mod._envelope(
        backend_name="codex", error=err, outcome=outcome, return_all_messages=False,
    )
    diag = env["diagnostics"]
    assert diag["noise_log"] == ["n1"]
    assert diag["kill_chain"] == [dataclasses.asdict(chain[0])]
    assert diag["backend_chosen"] == "codex"


# ---------- gemini transient empty-stream internal retry ----------
# docs/bugs/gemini-transient-empty-stream-retry.md

_EMPTY_STREAM_MSG = (
    "Invalid stream: The model returned an empty response or malformed tool call."
)


def _classify_error_outcome(msg: str = _EMPTY_STREAM_MSG) -> RunOutcome:
    pr = ParseResult(
        session_id="sid-err",
        agent_messages="",
        all_messages=[],
        noise_log=[],
        transient_lines=[],
        saw_terminal=True,
        error_events=[{"type": "error", "message": msg}],
    )
    return RunOutcome(
        parse_result=pr, stderr_tail="", kill_chain=[], returncode=0, timed_out=False,
    )


def _sequenced_dispatcher(monkeypatch, results: list[DispatchResult]) -> list[dict]:
    """Dispatcher stub returning `results` in order; records each invoke's kwargs."""
    calls: list[dict] = []

    def next_result(**kwargs):
        calls.append(kwargs)
        return results[len(calls) - 1]

    monkeypatch.setattr(server_mod, "Dispatcher", _FakeDispatcher.factory(next_result))
    monkeypatch.setattr(server_mod, "_GEMINI_TRANSIENT_RETRY_DELAY_S", 0.0)
    return calls


async def _invoke_gemini_default(tmp_path: Path, session_id: str = "") -> dict:
    return await server_mod._invoke_gemini(
        prompt="hi", cd=tmp_path, session_id=session_id, return_all_messages=False,
        model="", sandbox=False, gemini_default_model="gemini-test",
    )


async def test_gemini_transient_empty_stream_retries_once_then_succeeds(
    monkeypatch, tmp_path: Path,
):
    calls = _sequenced_dispatcher(monkeypatch, [
        DispatchResult(outcome=_classify_error_outcome()),
        DispatchResult(outcome=_success_outcome("second try")),
    ])
    result = await _invoke_gemini_default(tmp_path, session_id="resume-1")
    assert len(calls) == 2
    # Identical job re-issued: same backend, prompt, and full SpawnConfig.
    assert calls[1]["backend_name"] == calls[0]["backend_name"] == "gemini"
    assert calls[1]["prompt"] == calls[0]["prompt"]
    assert calls[1]["cfg"] == calls[0]["cfg"]
    assert calls[0]["cfg"].session_id == "resume-1"
    assert result["success"] is True
    assert result["error"] is None
    assert result["agent_messages"] == "second try"
    # Retry must stay visible even on the default flag-off success envelope.
    assert result["diagnostics"] is not None
    assert result["diagnostics"]["internal_retries"] == 1


async def test_gemini_transient_empty_stream_exhaustion_returns_last_error(
    monkeypatch, tmp_path: Path,
):
    second_msg = "Invalid stream: model returned an empty response (attempt 2)"
    calls = _sequenced_dispatcher(monkeypatch, [
        DispatchResult(outcome=_classify_error_outcome()),
        DispatchResult(outcome=_classify_error_outcome(second_msg)),
    ])
    result = await _invoke_gemini_default(tmp_path)
    assert len(calls) == 2  # exactly one internal retry
    assert result["success"] is False
    err = result["error"]
    assert err["code"] == "gemini_error"
    assert err["stage"] == "classify"
    assert err["raw"] == second_msg  # LAST error, not the first
    assert err["retryable"] is True
    assert result["diagnostics"]["internal_retries"] == 1


async def test_gemini_retry_attempt_dying_without_outcome_keeps_indicator(
    monkeypatch, tmp_path: Path,
):
    """Codex-review blocker regression: when the retry attempt fails at the
    dispatcher level (no outcome), the returned envelope must still carry
    `internal_retries` — the extra dispatch must not become invisible."""
    transport_err = make_error(
        "transport_failure", "shutdown", retryable=True, raw="worker died",
    )
    calls = _sequenced_dispatcher(monkeypatch, [
        DispatchResult(outcome=_classify_error_outcome()),
        DispatchResult(outcome=None, error=transport_err),
    ])
    result = await _invoke_gemini_default(tmp_path)
    assert len(calls) == 2
    assert result["success"] is False
    assert result["error"] == transport_err
    assert result["diagnostics"]["internal_retries"] == 1


async def test_gemini_first_attempt_preflight_error_not_retried(
    monkeypatch, tmp_path: Path,
):
    """First dispatch dying without an outcome is never retried and keeps
    the pre-existing envelope shape (diagnostics=None)."""
    preflight = make_error(
        "request_too_large", "pre_flight", retryable=False, raw="too big",
    )
    calls = _sequenced_dispatcher(monkeypatch, [
        DispatchResult(outcome=None, error=preflight),
    ])
    result = await _invoke_gemini_default(tmp_path)
    assert len(calls) == 1
    assert result["success"] is False
    assert result["error"] == preflight
    assert result["diagnostics"] is None


async def test_gemini_non_matching_classify_error_not_retried(
    monkeypatch, tmp_path: Path,
):
    calls = _sequenced_dispatcher(monkeypatch, [
        DispatchResult(outcome=_classify_error_outcome("tool runtime crashed")),
    ])
    result = await _invoke_gemini_default(tmp_path)
    assert len(calls) == 1
    assert result["success"] is False
    assert result["error"]["code"] == "gemini_error"
    assert result["error"]["retryable"] is False
    assert "internal_retries" not in result["diagnostics"]


async def test_codex_empty_stream_never_retried(monkeypatch, tmp_path: Path):
    calls = _sequenced_dispatcher(monkeypatch, [
        DispatchResult(outcome=_classify_error_outcome()),
    ])
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
    result = await server_mod._invoke_codex(
        prompt="hi", cd=tmp_path, sandbox="read-only", session_id="",
        skip_git_repo_check=True, return_all_messages=False,
        model="", profile="", image=None, yolo=False, require_approval=True,
    )
    assert len(calls) == 1
    assert result["success"] is False
    assert result["error"]["code"] == "codex_error"
    assert result["error"]["retryable"] is False


# ---------- gemini empty-success fail-closed (empty_final) ----------
# docs/bugs/gemini-empty-final-fail-closed.md


def _empty_final_stream(sid: str, *, tool_round: bool = True) -> list[str]:
    """gemini-cli's swallowed-InvalidStreamError signature: zero assistant
    messages, clean result:success, exit 0. `tool_round=False` models the
    round-1 NO_RESPONSE_TEXT case (no tool call precedes the empty final),
    which the detection gate must also catch."""
    lines = [
        json.dumps({"type": "init", "session_id": sid, "model": "gemini-test"}),
        json.dumps({"type": "message", "role": "user", "content": "probe task"}),
    ]
    if tool_round:
        lines += [
            json.dumps({"type": "tool_use", "tool_name": "read_file",
                        "tool_id": "t1", "parameters": {"path": "README.md"}}),
            json.dumps({"type": "tool_result", "tool_id": "t1", "status": "success"}),
        ]
    lines.append(json.dumps({"type": "result", "status": "success"}))
    return lines


def _gemini_stream_outcome(lines: list[str]) -> RunOutcome:
    """Golden path: raw JSONL through the real gemini parser, proving the
    stream assembles to empty agent_messages before hitting _invoke_gemini."""
    parser = StreamParser(GEMINI_SPEC)
    parser.feed_lines(lines)
    return RunOutcome(
        parse_result=parser.result(), stderr_tail="", kill_chain=[],
        returncode=0, timed_out=False,
    )


async def test_gemini_empty_final_retry_returns_text_succeeds(
    monkeypatch, tmp_path: Path,
):
    calls = _sequenced_dispatcher(monkeypatch, [
        DispatchResult(outcome=_gemini_stream_outcome(_empty_final_stream("sid-empty-1"))),
        DispatchResult(outcome=_success_outcome("real answer")),
    ])
    result = await _invoke_gemini_default(tmp_path, session_id="resume-2")
    assert len(calls) == 2
    # Identical job re-issued, same continuation session.
    assert calls[1]["prompt"] == calls[0]["prompt"]
    assert calls[1]["cfg"] == calls[0]["cfg"]
    assert calls[0]["cfg"].session_id == "resume-2"
    assert result["success"] is True
    assert result["error"] is None
    assert result["agent_messages"] == "real answer"
    # Envelope carries the RETRY attempt's session, not the first attempt's.
    assert result["SESSION_ID"] == "sid-success"
    assert result["diagnostics"] is not None
    assert result["diagnostics"]["internal_retries"] == 1


@pytest.mark.parametrize("tool_round", [True, False], ids=["tool-round", "round-1"])
async def test_gemini_empty_final_exhaustion_fails_closed(
    monkeypatch, tmp_path: Path, tool_round: bool,
):
    calls = _sequenced_dispatcher(monkeypatch, [
        DispatchResult(outcome=_gemini_stream_outcome(
            _empty_final_stream("sid-empty-1", tool_round=tool_round))),
        DispatchResult(outcome=_gemini_stream_outcome(
            _empty_final_stream("sid-empty-2", tool_round=tool_round))),
    ])
    result = await _invoke_gemini_default(tmp_path)
    assert len(calls) == 2  # exactly one internal re-dispatch
    assert result["success"] is False
    err = result["error"]
    assert err["code"] == "empty_final"
    assert err["stage"] == "classify"
    assert err["retryable"] is True
    assert "InvalidStreamError" in err["raw"]
    assert "docs/bugs/gemini-empty-final-fail-closed.md" in err["raw"]
    # SESSION_ID from the FINAL outcome stays on the envelope.
    assert result["SESSION_ID"] == "sid-empty-2"
    assert result["agent_messages"] == ""
    assert result["diagnostics"]["internal_retries"] == 1


async def test_gemini_whitespace_only_final_treated_as_empty(
    monkeypatch, tmp_path: Path,
):
    calls = _sequenced_dispatcher(monkeypatch, [
        DispatchResult(outcome=_success_outcome("  \n\t ")),
        DispatchResult(outcome=_success_outcome("  \n\t ")),
    ])
    result = await _invoke_gemini_default(tmp_path)
    assert len(calls) == 2
    assert result["success"] is False
    assert result["error"]["code"] == "empty_final"


async def test_gemini_empty_final_retry_dying_without_outcome_keeps_indicator(
    monkeypatch, tmp_path: Path,
):
    """empty-success → retry dies at the dispatcher level (no outcome): the
    transport error is returned as-is (no empty_final override — outcome is
    None) and the extra dispatch stays visible via internal_retries."""
    transport_err = make_error(
        "transport_failure", "shutdown", retryable=True, raw="worker died",
    )
    calls = _sequenced_dispatcher(monkeypatch, [
        DispatchResult(outcome=_gemini_stream_outcome(_empty_final_stream("sid-empty-1"))),
        DispatchResult(outcome=None, error=transport_err),
    ])
    result = await _invoke_gemini_default(tmp_path)
    assert len(calls) == 2
    assert result["success"] is False
    assert result["error"] == transport_err
    assert result["diagnostics"]["internal_retries"] == 1


async def test_gemini_empty_final_then_transient_error_shares_budget(
    monkeypatch, tmp_path: Path,
):
    """Both triggers firing sequentially must spend the SAME one-retry
    budget: empty-success → retry → transient empty-stream error → stop
    (no third dispatch); the retry's error is returned as-is."""
    calls = _sequenced_dispatcher(monkeypatch, [
        DispatchResult(outcome=_gemini_stream_outcome(_empty_final_stream("sid-empty-1"))),
        DispatchResult(outcome=_classify_error_outcome()),
    ])
    result = await _invoke_gemini_default(tmp_path)
    assert len(calls) == 2
    assert result["success"] is False
    assert result["error"]["code"] == "gemini_error"
    assert result["diagnostics"]["internal_retries"] == 1


async def test_gemini_transient_error_then_empty_success_fails_closed(
    monkeypatch, tmp_path: Path,
):
    """Reverse order: transient error → retry → empty success. The shared
    budget forbids a second retry; the empty retry converts to empty_final."""
    calls = _sequenced_dispatcher(monkeypatch, [
        DispatchResult(outcome=_classify_error_outcome()),
        DispatchResult(outcome=_gemini_stream_outcome(_empty_final_stream("sid-empty-2"))),
    ])
    result = await _invoke_gemini_default(tmp_path)
    assert len(calls) == 2
    assert result["success"] is False
    assert result["error"]["code"] == "empty_final"
    assert result["diagnostics"]["internal_retries"] == 1


async def test_gemini_nonempty_success_default_envelope_unchanged(
    monkeypatch, tmp_path: Path,
):
    """Regression: a normal non-empty success dispatches once and emits NO
    diagnostics — the empty_final gate must not touch the default envelope."""
    calls = _sequenced_dispatcher(monkeypatch, [
        DispatchResult(outcome=_success_outcome("hello")),
    ])
    result = await _invoke_gemini_default(tmp_path)
    assert len(calls) == 1
    assert result["success"] is True
    assert result["diagnostics"] is None


async def _invoke_codex_default(tmp_path: Path) -> dict:
    return await server_mod._invoke_codex(
        prompt="hi", cd=tmp_path, sandbox="read-only", session_id="",
        skip_git_repo_check=True, return_all_messages=False,
        model="", profile="", image=None, yolo=False, require_approval=True,
    )


async def test_codex_empty_success_fails_closed_without_retry(
    monkeypatch, tmp_path: Path,
):
    """Codex empty final fails closed on the FIRST dispatch — no re-issue:
    a codex turn costs minutes and an empty final has no observed transient
    cause. docs/bugs/codex-transient-retry-storm.md."""
    calls = _sequenced_dispatcher(monkeypatch, [
        DispatchResult(outcome=_success_outcome("")),
    ])
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
    result = await _invoke_codex_default(tmp_path)

    assert len(calls) == 1                      # no internal retry
    assert result["success"] is False
    err = result["error"]
    assert err["code"] == "empty_final"
    assert err["stage"] == "classify"
    # retryable is a caller hint: repeating a multi-minute codex turn whose
    # tool side effects already ran is exactly what must not be advised.
    # Asymmetric with gemini's empty_final (retryable=True) on purpose —
    # that one has a known transient cause and a demonstrated recovery.
    assert err["retryable"] is False
    # SESSION_ID stays on the envelope so the caller can resume/inspect.
    assert result["SESSION_ID"] == "sid-success"


async def test_codex_whitespace_only_final_fails_closed(
    monkeypatch, tmp_path: Path,
):
    """Whitespace is not content — same gate as gemini's."""
    _sequenced_dispatcher(monkeypatch, [
        DispatchResult(outcome=_success_outcome("  \n\t ")),
    ])
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
    result = await _invoke_codex_default(tmp_path)
    assert result["error"]["code"] == "empty_final"


async def test_codex_nonempty_success_default_envelope_unchanged(
    monkeypatch, tmp_path: Path,
):
    """Regression: the empty_final gate must not touch a normal success —
    still one dispatch, no error, no diagnostics."""
    calls = _sequenced_dispatcher(monkeypatch, [
        DispatchResult(outcome=_success_outcome("hello")),
    ])
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
    result = await _invoke_codex_default(tmp_path)
    assert len(calls) == 1
    assert result["success"] is True
    assert result["error"] is None
    assert result["diagnostics"] is None


async def test_codex_existing_error_not_masked_by_empty_final(
    monkeypatch, tmp_path: Path,
):
    """An outcome that classify already condemned keeps ITS code — the gate
    only fires when classify found nothing (the 2026-08-10 incident's three
    turns were `codex_error` + empty text and must stay that way)."""
    _sequenced_dispatcher(monkeypatch, [
        DispatchResult(outcome=_classify_error_outcome("unexpected status 504")),
    ])
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
    result = await _invoke_codex_default(tmp_path)
    assert result["success"] is False
    assert result["error"]["code"] == "codex_error"


# ---------- smoke_probe edge cases (real subprocess but trivial) ----------

def test_smoke_probe_returns_false_for_missing_binary():
    """Binary not on PATH at all (deliberately bogus name)."""
    assert server_mod.smoke_probe("definitely-not-a-real-cli-aoeuhtns") is False


def test_build_server_passes_per_backend_probe_timeouts(monkeypatch):
    """Wiring guard: build_server hands codex and gemini their OWN probe
    budgets, not a shared constant — gemini wider for Node cold start.
    docs/bugs/gemini-smoke-probe-timeout.md."""
    seen: dict[str, float] = {}

    def fake(cmd: str, *, timeout_sec: float = 5.0) -> bool:
        seen[cmd] = timeout_sec
        return True

    monkeypatch.setattr(server_mod, "smoke_probe", fake)
    build_server(ServerConfig.from_env(
        {"PRISM_PROBE_TIMEOUT_CODEX": "4", "PRISM_PROBE_TIMEOUT_GEMINI": "13"}
    ))
    assert seen["codex"] == 4.0
    assert seen["gemini"] == 13.0


def test_build_server_default_probe_timeouts(monkeypatch):
    """Defaults flow through: codex 5s, gemini 12s."""
    seen: dict[str, float] = {}

    def fake(cmd: str, *, timeout_sec: float = 5.0) -> bool:
        seen[cmd] = timeout_sec
        return True

    monkeypatch.setattr(server_mod, "smoke_probe", fake)
    build_server(ServerConfig.from_env({}))
    assert seen["codex"] == 5.0
    assert seen["gemini"] == 12.0


# ---------- MCP Bundle Protocol wiring (docs/bugs/codex-prompt-bundle-protocol.md) ----------


async def test_server_instructions_include_preamble_and_orchestrator_side(force_probes):
    """Regression: instructions must carry BOTH the original two-sentence
    preamble AND the ORCHESTRATOR_SIDE protocol — guards against future
    refactor accidentally wiping either half."""
    force_probes(codex_ok=True, gemini_ok=True)
    instr = build_server().instructions
    assert "Wrappers for codex / gemini CLIs" in instr
    assert "Use SESSION_ID for multi-turn conversations" in instr
    assert ORCHESTRATOR_SIDE in instr


@pytest.mark.parametrize("backend_name", ["codex", "gemini"])
async def test_invoke_prepends_agent_side_to_prompt(
    monkeypatch, tmp_path: Path, backend_name: str,
):
    """Wiring guard: the prompt seen by Dispatcher MUST start with AGENT_SIDE
    and end with the caller's original PROMPT body."""
    seen: dict = {}
    monkeypatch.setattr(
        server_mod, "Dispatcher",
        _FakeDispatcher.factory(DispatchResult(outcome=_success_outcome()), seen=seen),
    )
    if backend_name == "codex":
        monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
        await server_mod._invoke_codex(
            prompt="user task body", cd=tmp_path, sandbox="read-only", session_id="",
            skip_git_repo_check=True, return_all_messages=False,
            model="", profile="", image=None, yolo=False, require_approval=True,
        )
    else:
        await server_mod._invoke_gemini(
            prompt="user task body", cd=tmp_path, session_id="",
            return_all_messages=False, model="", sandbox=False,
            gemini_default_model="gemini-test",
        )
    assert seen["prompt"].startswith(AGENT_SIDE)
    assert seen["prompt"].endswith("user task body")


@pytest.mark.parametrize("backend_name", ["codex", "gemini"])
async def test_invoke_warns_when_prompt_exceeds_200kb(
    monkeypatch, tmp_path: Path, caplog, backend_name: str,
):
    """Diagnostic guard: oversize warning fires in the main MCP process log
    (not the worker stderr), so an operator investigating codex compaction
    can correlate."""
    monkeypatch.setattr(
        server_mod, "Dispatcher",
        _FakeDispatcher.factory(DispatchResult(outcome=_success_outcome())),
    )
    big = "x" * (OVERSIZED_THRESHOLD_BYTES + 1)
    with caplog.at_level("WARNING", logger="prism_router"):
        if backend_name == "codex":
            monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
            await server_mod._invoke_codex(
                prompt=big, cd=tmp_path, sandbox="read-only", session_id="",
                skip_git_repo_check=True, return_all_messages=False,
                model="", profile="", image=None, yolo=False, require_approval=True,
            )
        else:
            await server_mod._invoke_gemini(
                prompt=big, cd=tmp_path, session_id="",
                return_all_messages=False, model="", sandbox=False,
                gemini_default_model="gemini-test",
            )
    msgs = [r.getMessage() for r in caplog.records]
    assert any("oversized prompt" in m for m in msgs)
    assert any(f"backend={backend_name}" in m for m in msgs)


async def test_invoke_codex_no_warning_for_normal_prompt(monkeypatch, tmp_path: Path, caplog):
    """Negative: a short PROMPT must not trigger the oversize warning even
    after AGENT_SIDE prepend (~1.5 KB)."""
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
    monkeypatch.setattr(
        server_mod, "Dispatcher",
        _FakeDispatcher.factory(DispatchResult(outcome=_success_outcome())),
    )
    with caplog.at_level("WARNING", logger="prism_router"):
        await server_mod._invoke_codex(
            prompt="short task", cd=tmp_path, sandbox="read-only", session_id="",
            skip_git_repo_check=True, return_all_messages=False,
            model="", profile="", image=None, yolo=False, require_approval=True,
        )
    assert not any("oversized prompt" in r.getMessage() for r in caplog.records)


# ---------- liveness egress lifecycle (docs/bugs/host-idle-progress-egress.md) ----------


class _SpyEgress(LivenessEgress):
    """Records lifecycle calls so a test can prove WHICH branch ran."""

    made: list["_SpyEgress"] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed = 0
        self.aborted = 0
        _SpyEgress.made.append(self)

    async def aclose(self, **kwargs) -> None:
        self.closed += 1
        await super().aclose(**kwargs)

    def abort(self) -> None:
        self.aborted += 1
        super().abort()


class _LivenessSpy:
    """Stands in for an active fastmcp Context with a progressToken. `sink` is
    late-bound so a test can swap in a blocking one before the call."""

    def __init__(self) -> None:
        self.sent: list[tuple[float, str]] = []
        self.sink = self._record

    async def _record(self, progress: float, message: str) -> None:
        self.sent.append((progress, message))

    @property
    def egresses(self) -> list[_SpyEgress]:
        return _SpyEgress.made


@pytest.fixture
def liveness_spy(monkeypatch):
    _SpyEgress.made = []
    spy = _LivenessSpy()
    monkeypatch.setattr(server_mod, "_progress_sink", lambda: spy.sink)
    monkeypatch.setattr(server_mod, "LivenessEgress", _SpyEgress)
    return spy


def _preflight_error(code: str) -> DispatchResult:
    return DispatchResult(
        outcome=None,
        error=make_error(code, "pre_flight", retryable=False, raw=f"{code} raw"),
    )


async def test_the_real_no_spawn_path_creates_then_acloses_a_silent_egress(
    monkeypatch, tmp_path: Path, liveness_spy,
):
    """The REAL Dispatcher's pre-flight reject, not a fake standing in for it:
    an oversize request is refused before any spawn, so no evidence site can
    run and the egress must close having sent nothing."""
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
    result = await server_mod._run_with_liveness(True, lambda note: server_mod._invoke_codex(
        prompt="x" * (STDIN_PREFLIGHT_CAP + 100), cd=tmp_path,
        sandbox="read-only", session_id="", skip_git_repo_check=True,
        return_all_messages=False, model="", profile="", image=None,
        yolo=False, require_approval=True, on_liveness=note,
    ))

    assert result["error"]["code"] == "request_too_large"
    assert liveness_spy.sent == []
    assert liveness_spy.egresses[0].closed == 1
    assert liveness_spy.egresses[0].attempts == 0


@pytest.mark.parametrize(
    "code", ["request_too_large", "disk_unavailable", "spawn_failed"],
)
async def test_no_spawn_dispatcher_returns_still_create_then_aclose_the_egress(
    monkeypatch, tmp_path: Path, liveness_spy, code: str,
):
    """Every no-spawn error SHAPE (outcome=None + error) must take the barrier.
    Faked so all three are reachable; the real-Dispatcher case above is what
    proves the production pre-flight path itself stays silent."""
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
    monkeypatch.setattr(
        server_mod, "Dispatcher", _FakeDispatcher.factory(_preflight_error(code)),
    )
    result = await server_mod._run_with_liveness(True, lambda note: server_mod._invoke_codex(
        prompt="hi", cd=tmp_path, sandbox="read-only", session_id="",
        skip_git_repo_check=True, return_all_messages=False,
        model="", profile="", image=None, yolo=False, require_approval=True,
        on_liveness=note,
    ))

    assert result["success"] is False
    assert result["error"]["code"] == code
    assert liveness_spy.sent == []
    assert len(liveness_spy.egresses) == 1
    egress = liveness_spy.egresses[0]
    assert egress.closed == 1, "the barrier must run on the error-envelope path"
    assert egress.aborted == 1, "the finally backstop must still run"
    assert egress.attempts == 0


async def test_prompt_file_error_returns_before_dispatch_still_closes_the_egress(
    monkeypatch, tmp_path: Path, liveness_spy,
):
    """`prompt_file_error` short-circuits before any Dispatcher exists — proven
    by an exploding constructor, not merely by the returned code."""
    def _never(**kwargs):
        raise AssertionError("no Dispatcher may be constructed on this path")

    monkeypatch.setattr(server_mod, "Dispatcher", _never)
    result = await server_mod._run_with_liveness(True, lambda note: server_mod._invoke_codex(
        prompt="hi", cd=tmp_path, sandbox="read-only", session_id="",
        skip_git_repo_check=True, return_all_messages=False,
        model="", profile="", image=None, yolo=False, require_approval=True,
        prompt_file="does-not-exist.md", on_liveness=note,
    ))

    assert result["error"]["code"] == "prompt_file_error"
    assert liveness_spy.sent == []
    assert liveness_spy.egresses[0].closed == 1


def _observer_recording_dispatcher(monkeypatch, results: list[DispatchResult]):
    """Dispatcher stub that records the `on_liveness` each CONSTRUCTION got and
    fires it once from `invoke`, standing in for a `stdout_tick`.
    `_FakeDispatcher` swallows constructor kwargs, so without this a dropped
    hand-off would be invisible to every other test."""
    observers: list = []

    class _Recording:
        def __init__(self, **kwargs):
            self._observer = kwargs.get("on_liveness")
            self._result = results[len(observers)]
            observers.append(self._observer)

        async def invoke(self, *, backend_name, prompt, cfg):
            if self._observer is not None:
                self._observer("stdout")
            return self._result

    monkeypatch.setattr(server_mod, "Dispatcher", _Recording)
    return observers


async def test_codex_hands_its_observer_to_the_dispatcher(
    monkeypatch, tmp_path: Path, liveness_spy,
):
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
    observers = _observer_recording_dispatcher(
        monkeypatch, [DispatchResult(outcome=_success_outcome())],
    )
    await server_mod._run_with_liveness(True, lambda note: server_mod._invoke_codex(
        prompt="hi", cd=tmp_path, sandbox="read-only", session_id="",
        skip_git_repo_check=True, return_all_messages=False,
        model="", profile="", image=None, yolo=False, require_approval=True,
        on_liveness=note,
    ))
    assert observers == [liveness_spy.egresses[0].note]
    assert liveness_spy.sent == [(1.0, liveness_spy.sent[0][1])]
    assert liveness_spy.sent[0][1].startswith("stdout activity")


async def test_gemini_retry_shares_one_observer_across_both_dispatchers(
    monkeypatch, tmp_path: Path, liveness_spy,
):
    """F2: one egress per REQUEST. gemini's transient retry constructs a SECOND
    Dispatcher, which must get the SAME observer or `progress` restarts."""
    monkeypatch.setattr(server_mod, "_GEMINI_TRANSIENT_RETRY_DELAY_S", 0.0)
    observers = _observer_recording_dispatcher(monkeypatch, [
        DispatchResult(outcome=_classify_error_outcome()),
        DispatchResult(outcome=_success_outcome("second try")),
    ])
    result = await server_mod._run_with_liveness(
        True, lambda note: server_mod._invoke_gemini(
            prompt="hi", cd=tmp_path, session_id="", return_all_messages=False,
            model="", sandbox=False, gemini_default_model="gemini-test",
            on_liveness=note,
        ),
    )
    assert result["diagnostics"]["internal_retries"] == 1
    assert len(observers) == 2
    assert observers[0] == liveness_spy.egresses[0].note
    assert observers[1] == observers[0], "the re-dispatch must reuse the egress"
    # One egress, one throttle window: the second dispatch's tick is dropped,
    # so `progress` never restarts at 1.
    assert [progress for progress, _ in liveness_spy.sent] == [1.0]


async def test_a_raising_invoke_passes_the_barrier_before_the_error_propagates(
    liveness_spy,
):
    """G2: an unexpected exception must still drain the in-flight send BEFORE
    FastMCP can emit an error response — and reach FastMCP unwrapped, not
    inside the task group's ExceptionGroup."""
    order: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def sink(progress: float, message: str) -> None:
        started.set()
        await release.wait()
        order.append("sent")

    async def invoke(note):
        note("stdout")
        await started.wait()
        asyncio.get_running_loop().call_later(0.05, release.set)
        raise RuntimeError("invoke blew up")

    liveness_spy.sink = sink
    with pytest.raises(RuntimeError) as excinfo:
        await server_mod._run_with_liveness(True, invoke)
    order.append("raised")

    assert excinfo.type is RuntimeError
    assert str(excinfo.value) == "invoke blew up"
    assert order == ["sent", "raised"]
    assert liveness_spy.egresses[0].closed == 1


async def test_cancel_aborts_without_draining_and_re_raises_cancellederror(
    liveness_spy,
):
    """Cancel unwind: `abort()` only (no awaited drain), CancelledError
    re-raised undelayed and unwrapped (dispatcher invariant 5)."""
    outcome: list[str] = []
    started = asyncio.Event()

    async def sink(progress: float, message: str) -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise

    async def invoke(note):
        note("stdout")
        await started.wait()
        raise asyncio.CancelledError()

    liveness_spy.sink = sink
    with pytest.raises(asyncio.CancelledError):
        await server_mod._run_with_liveness(True, invoke)

    assert outcome == ["cancelled"]
    egress = liveness_spy.egresses[0]
    assert egress.closed == 0, "the cancel path must NOT await a drain"
    assert egress.aborted >= 1


async def test_normal_return_awaits_the_barrier_then_returns(liveness_spy):
    """Success path: the send settles before the envelope leaves the tool."""
    order: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def sink(progress: float, message: str) -> None:
        started.set()
        await release.wait()
        order.append("sent")

    async def invoke(note):
        note("stdout")
        await started.wait()
        asyncio.get_running_loop().call_later(0.05, release.set)
        return {"success": True}

    liveness_spy.sink = sink
    result = await server_mod._run_with_liveness(True, invoke)
    order.append("returned")

    assert result == {"success": True}
    assert order == ["sent", "returned"]
    assert liveness_spy.egresses[0].closed == 1


async def test_a_sink_that_resists_cancellation_only_delays_the_response(
    liveness_spy,
):
    """Bound the degradation: a sink that shields itself past the drain window
    delays the RESPONSE by exactly how long it resists — the envelope is
    neither lost nor stalled beyond that."""
    stubborn_for = 0.4
    finished: list[str] = []

    async def sink(progress: float, message: str) -> None:
        with anyio.CancelScope(shield=True):
            await anyio.sleep(stubborn_for)  # ignores abort()'s cancel
        finished.append("done")

    async def invoke(note):
        note("stdout")
        return "envelope"

    liveness_spy.sink = sink
    t0 = time.monotonic()
    result = await server_mod._run_with_liveness(True, invoke)
    elapsed = time.monotonic() - t0

    assert result == "envelope"
    # Deterministic rather than a wall-clock lower bound: an event loop can
    # return from `sleep` inside its own clock resolution, which made the
    # naive `elapsed >= stubborn_for` form flake.
    assert finished == ["done"], "the barrier must wait for real settlement"
    assert elapsed < stubborn_for + 2.0, "and must not hang past it"


async def test_a_spuriously_cancelling_sink_does_not_break_a_successful_call(
    liveness_spy,
):
    """Regression: a sink raising CancelledError with none pending used to
    cancel the tool's task group; `__aexit__` swallowed that cancellation and
    the handler fell through to an unassigned result."""
    async def sink(progress: float, message: str) -> None:
        raise asyncio.CancelledError()

    async def invoke(note):
        note("stdout")
        await asyncio.sleep(0.02)
        return "envelope"

    liveness_spy.sink = sink
    assert await server_mod._run_with_liveness(True, invoke) == "envelope"


async def test_a_swallowed_group_cancellation_becomes_a_request_local_failure(
    liveness_spy,
):
    """Backstop, independent of the sink: anyio cancels a task group's own
    scope whenever ANY child ends in CancelledError, and `__aexit__` then
    swallows that. Falling through would return an unassigned result — but the
    replacement must NOT be CancelledError, which the SDK reads as transport
    shutdown (see the protocol test for the session-level consequence)."""
    async def invoke(note):
        async def rogue() -> None:  # stands in for any child dying by cancel
            raise asyncio.CancelledError()

        liveness_spy.egresses[0]._spawn(rogue)
        await asyncio.sleep(0.05)  # the group scope cancels us here
        return "envelope"  # pragma: no cover

    with pytest.raises(RuntimeError, match="cancelled unexpectedly"):
        await server_mod._run_with_liveness(True, invoke)


async def test_liveness_disabled_hands_the_invoke_no_observer(liveness_spy):
    """Knob off → no egress constructed, `on_liveness=None`, zero per-milestone
    calls."""
    seen: list = []

    async def invoke(note):
        seen.append(note)
        return "done"

    assert await server_mod._run_with_liveness(False, invoke) == "done"
    assert seen == [None]
    assert liveness_spy.egresses == []


async def test_missing_context_degrades_to_the_silent_path():
    """No active fastmcp Context → no sink → today's behavior, not a failure."""
    seen: list = []

    async def invoke(note):
        seen.append(note)
        return "done"

    # `_progress_sink` unpatched: there is no Context in a plain unit test.
    assert server_mod._progress_sink() is None
    assert await server_mod._run_with_liveness(True, invoke) == "done"
    assert seen == [None]


@pytest.mark.parametrize("tool", ["codex", "gemini"])
async def test_a_dispatcher_tick_reaches_the_client_through_the_registered_tool(
    monkeypatch, force_probes, tmp_path: Path, tool: str,
):
    """End-to-end over a real client: dispatcher evidence -> progress on the
    wire. This is the ONLY test that spans the tool-body -> `_invoke_*` hop, so
    it is what catches `on_liveness=note` being dropped from the `build_server`
    lambdas — a mutation that silently disables the whole feature while every
    other test stays green."""
    force_probes(codex_ok=True, gemini_ok=True)
    monkeypatch.setenv("PRISM_LIVENESS_PROGRESS", "1")
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)

    class _Ticking:
        def __init__(self, **kwargs):
            self._observer = kwargs.get("on_liveness")

        async def invoke(self, *, backend_name, prompt, cfg):
            assert self._observer is not None, "the tool body dropped on_liveness"
            self._observer("stdout")
            return DispatchResult(outcome=_success_outcome())

    monkeypatch.setattr(server_mod, "Dispatcher", _Ticking)

    seen: list[tuple[float, str | None]] = []

    async def on_progress(progress: float, total: float | None, message: str | None):
        seen.append((progress, message))

    async with Client(build_server(), progress_handler=on_progress) as client:
        result = await client.call_tool(
            tool, {"PROMPT": "hi", "cd": str(tmp_path)},
        )

    assert result.structured_content["success"] is True
    assert seen == [(1.0, seen[0][1])]
    assert seen[0][1].startswith("stdout activity")


@pytest.mark.parametrize(
    ("knob", "expected"), [("1", True), ("0", False)], ids=["on", "off"],
)
async def test_build_server_threads_the_liveness_knob_into_the_tool_bodies(
    monkeypatch, force_probes, tmp_path: Path, knob: str, expected: bool,
):
    force_probes(codex_ok=True, gemini_ok=True)
    monkeypatch.setenv("PRISM_LIVENESS_PROGRESS", knob)
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
    monkeypatch.setattr(
        server_mod, "Dispatcher",
        _FakeDispatcher.factory(DispatchResult(outcome=_success_outcome())),
    )
    seen: list[bool] = []
    real = server_mod._run_with_liveness

    async def spy(enabled, invoke):
        seen.append(enabled)
        return await real(enabled, invoke)

    monkeypatch.setattr(server_mod, "_run_with_liveness", spy)

    mcp = build_server()
    async with Client(mcp) as client:
        await client.call_tool("codex", {"PROMPT": "hi", "cd": str(tmp_path)})
        await client.call_tool("gemini", {"PROMPT": "hi", "cd": str(tmp_path)})

    assert seen == [expected, expected]


# ---------- gemini at-escape (docs/bugs/gemini-at-command-hang.md) ----------

# One at-sign inside quoted text is enough: upstream's scanner swallows across
# newlines from there. Shaped like the package.json diff that first hung.
_AT_BODY = '## Diff\n-  "@scope/pkg": "workspace:*",\n+  "@scope/pkg": "^2.0.0",\n'
_AT_BODY_ESCAPED = _AT_BODY.replace("@", "\\@")


def _dispatcher_seeing(monkeypatch) -> dict:
    seen: dict = {}
    monkeypatch.setattr(
        server_mod, "Dispatcher",
        _FakeDispatcher.factory(DispatchResult(outcome=_success_outcome()), seen=seen),
    )
    return seen


async def test_gemini_payload_is_escaped_and_prefixed_with_the_note(
    monkeypatch, tmp_path: Path,
):
    """The exact bytes handed to the dispatcher: AGENT_SIDE wrapper, then the
    note, then the body with every at-sign backslash-prefixed."""
    seen = _dispatcher_seeing(monkeypatch)
    await server_mod._invoke_gemini(
        prompt=_AT_BODY, cd=tmp_path, session_id="", return_all_messages=False,
        model="", sandbox=False, gemini_default_model="gemini-test",
    )
    assert seen["prompt"] == inject_agent_side(f"{AT_ESCAPE_NOTE}\n\n{_AT_BODY_ESCAPED}")
    assert "@" not in seen["prompt"].replace("\\@", "")


async def test_codex_payload_is_never_escaped(monkeypatch, tmp_path: Path):
    """Regression: the escape is gemini-only. Same input, byte-identical to the
    pre-fix codex payload — no escaping, no note."""
    seen = _dispatcher_seeing(monkeypatch)
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)
    await server_mod._invoke_codex(
        prompt=_AT_BODY, cd=tmp_path, sandbox="read-only", session_id="",
        skip_git_repo_check=True, return_all_messages=False,
        model="", profile="", image=None, yolo=False, require_approval=True,
    )
    assert seen["prompt"] == inject_agent_side(_AT_BODY)
    assert AT_ESCAPE_NOTE not in seen["prompt"]


async def test_payload_without_at_signs_is_byte_identical_to_the_prefix_build(
    monkeypatch, tmp_path: Path,
):
    """No-op fidelity, knob ON: nothing is rewritten and the note stays away.
    Deliberately not size-gated — the hang reproduces at 8 KB."""
    body = "## Diff\n-  old line\n+  new line\n"
    seen = _dispatcher_seeing(monkeypatch)
    await server_mod._invoke_gemini(
        prompt=body, cd=tmp_path, session_id="", return_all_messages=False,
        model="", sandbox=False, gemini_default_model="gemini-test",
    )
    assert seen["prompt"] == inject_agent_side(body)
    assert AT_ESCAPE_NOTE not in seen["prompt"]


async def test_knob_off_restores_the_unescaped_payload(monkeypatch, tmp_path: Path):
    """Rollback hatch: `at_escape=False` reproduces today's payload exactly,
    at-signs included."""
    seen = _dispatcher_seeing(monkeypatch)
    await server_mod._invoke_gemini(
        prompt=_AT_BODY, cd=tmp_path, session_id="", return_all_messages=False,
        model="", sandbox=False, gemini_default_model="gemini-test",
        at_escape=False,
    )
    assert seen["prompt"] == inject_agent_side(_AT_BODY)


async def test_the_internal_retry_re_dispatches_a_singly_escaped_payload(
    monkeypatch, tmp_path: Path,
):
    """The transient retry loop re-sends the SAME `prompt` variable. A second
    escape pass would ship `\\\\@` and one note per attempt."""
    calls = _sequenced_dispatcher(monkeypatch, [
        DispatchResult(outcome=_classify_error_outcome()),
        DispatchResult(outcome=_success_outcome("second try")),
    ])
    result = await server_mod._invoke_gemini(
        prompt=_AT_BODY, cd=tmp_path, session_id="", return_all_messages=False,
        model="", sandbox=False, gemini_default_model="gemini-test",
    )
    assert result["success"] is True
    assert len(calls) == 2
    expected = inject_agent_side(f"{AT_ESCAPE_NOTE}\n\n{_AT_BODY_ESCAPED}")
    assert calls[0]["prompt"] == calls[1]["prompt"] == expected
    assert "\\\\@" not in calls[1]["prompt"]
    assert calls[1]["prompt"].count(AT_ESCAPE_NOTE) == 1


@pytest.mark.parametrize(
    ("at_escape", "inline", "content", "expected_body"),
    [
        pytest.param(
            True, "review this", _AT_BODY,
            f"{AT_ESCAPE_NOTE}\n\nreview this\n\n{_AT_BODY_ESCAPED}",
            id="escapes-the-merged-payload",
        ),
        pytest.param(
            True, "review @header", _AT_BODY,
            f"{AT_ESCAPE_NOTE}\n\nreview \\@header\n\n{_AT_BODY_ESCAPED}",
            id="escapes-the-inline-half-too",
        ),
        pytest.param(
            True, "review this", "plain evidence\n",
            "review this\n\nplain evidence\n",
            id="at-free-file-gets-no-note",
        ),
        pytest.param(
            False, "review this", _AT_BODY,
            f"review this\n\n{_AT_BODY}",
            id="knob-off-leaves-the-file-content-alone",
        ),
    ],
)
async def test_receipt_attests_the_raw_file_across_the_escape_combinations(
    monkeypatch, tmp_path: Path,
    at_escape: bool, inline: str, content: str, expected_body: str,
):
    """The escape runs in memory AFTER the file is read and hashed, so the
    receipt's coverage domain must not move — callers verify it against the file
    on disk. The cases pin the escape to the MERGED payload: gating it on
    `prompt_file`, or applying it to the file half only, fails one of them.
    """
    raw = content.encode("utf-8")
    (tmp_path / "evidence.md").write_bytes(raw)
    seen = _dispatcher_seeing(monkeypatch)

    result = await server_mod._invoke_gemini(
        prompt=inline, cd=tmp_path, session_id="", return_all_messages=False,
        model="", sandbox=False, gemini_default_model="gemini-test",
        prompt_file="evidence.md", at_escape=at_escape,
    )
    receipt = result["prompt_file_receipt"]
    assert receipt["sha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["bytes"] == len(raw)
    assert seen["prompt"] == inject_agent_side(expected_body)


async def test_escape_count_is_logged_and_kept_off_the_envelope(
    monkeypatch, tmp_path: Path, caplog,
):
    """Observability lands in the log, never in `diagnostics`: widening
    `_envelope`'s `has_extra` gate would give every at-bearing gemini call a
    diagnostics block it does not have today."""
    _dispatcher_seeing(monkeypatch)
    with caplog.at_level("INFO", logger="prism_router"):
        result = await server_mod._invoke_gemini(
            prompt=_AT_BODY, cd=tmp_path, session_id="", return_all_messages=False,
            model="", sandbox=False, gemini_default_model="gemini-test",
        )
    assert result["diagnostics"] is None
    assert any(
        "gemini at-escape: 2 at-sign(s) escaped" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize(
    ("knob", "escaped"), [("1", True), ("0", False)], ids=["on", "off"],
)
async def test_build_server_threads_the_at_escape_knob_into_the_tool_body(
    monkeypatch, force_probes, tmp_path: Path, knob: str, escaped: bool,
):
    """Spans the registered tool -> `_invoke_gemini` hop, which no other test in
    this section covers. `_invoke_gemini` defaults `at_escape=True`, so dropping
    `at_escape=gemini_at_escape` from the tool-body lambda keeps the ON case
    green — only the OFF case catches it. Both are asserted on delivered bytes.

    The env is flipped AFTER the config is built: the tool body must read the
    ServerConfig it was constructed with, not re-read the environment.
    """
    force_probes(codex_ok=False, gemini_ok=True)
    monkeypatch.setenv("PRISM_GEMINI_AT_ESCAPE", knob)
    cfg = ServerConfig.from_env()
    monkeypatch.setenv("PRISM_GEMINI_AT_ESCAPE", "0" if knob == "1" else "1")
    seen = _dispatcher_seeing(monkeypatch)

    mcp = build_server(cfg)
    async with Client(mcp) as client:
        await client.call_tool("gemini", {"PROMPT": _AT_BODY, "cd": str(tmp_path)})

    expected = (
        inject_agent_side(f"{AT_ESCAPE_NOTE}\n\n{_AT_BODY_ESCAPED}") if escaped
        else inject_agent_side(_AT_BODY)
    )
    assert seen["prompt"] == expected
