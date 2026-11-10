"""§5.1 M4 fixture regression — 65 legacy GuDaStudio failures must not be reproduced.

Strategy chosen: B-mocked-spawn (hermetic).

  Strategy A (raw stream → parser → classify) is impossible: neither
  ``fixtures.json`` nor ``docs/mcp-audit/samples/*.txt`` carries raw stream
  lines. The samples are summary digests; the fixtures expose only ``args``
  and the legacy ``original_error`` string.

  Strategy B-replay (real subprocess against fixture args) does not buy a
  useful regression signal: fixture cwds are paths from the source machine
  (most don't exist here), 503/401 categories were transient backend states
  unlikely to reproduce on demand, and 65 long prompts × real API quotas
  cost money without exceeding what ``tests/integration/test_smoke.py`` (M2)
  already covers for the live spawn path.

  Therefore we monkeypatch ``runner.run`` with a per-category synthetic
  ``RunOutcome`` factory and drive ``server._invoke_codex`` /
  ``_invoke_gemini`` end-to-end with each fixture's actual ``args``. The
  synthetic outcome encodes the root-cause shape (503 message, refusal
  status, oversize body, etc.); the assertion stack proves the resulting
  envelope is leadin-free and shape-conformant.

Layered assertions (per v1-plan §5.1):
  1. ``agent_messages`` contains none of the GuDaStudio LEGACY_LEADINS
  2. envelope shape: success+str OR error+typed envelope (5-key contract)
  3. EXPECTED_CODE map enforced for buckets where the failure mode is
     deterministic (503/401/disconnect/cli_too_long/stop_self/no_session)
  4. MISSING bucket: success path must yield non-empty ``agent_messages``
     (empty success is the legacy "no result block" regression mode)
  5. legacy GuDaStudio dict-shape signatures absent from the serialised
     envelope (proves the new wrapper structurally cannot emit them)

Marked ``@pytest.mark.fixtures`` — default ``pytest -q`` excludes; user runs
``pytest -m fixtures`` before ``.mcp.json`` cutover.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prism_router import server as server_mod
from prism_router.errors import ALL_ERROR_CODES, ALL_STAGES
from prism_router.parser import ParseResult
from prism_router.runner import RunOutcome

pytestmark = pytest.mark.fixtures

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FIXTURES_PATH = _REPO_ROOT / "docs" / "mcp-audit" / "fixtures.json"

# GuDaStudio failure leadins. The new wrapper never writes these strings into
# any envelope field — they are templates from the legacy wrapper. Backend
# root-cause text from upstream CLIs may still legitimately appear, but those
# would be substrings of error.raw/stderr_tail with their own backend
# verbatim, not these wrapper-fabricated prefixes.
LEGACY_LEADINS: tuple[str, ...] = (
    "[json decode error]",
    "Failed to retrieve `agent_messages`",
    "Failed to get `agent_messages`",
    "Failed to get `SESSION_ID`",
)

# §4.2 envelope key contracts.
_RESULT_KEYS: frozenset[str] = frozenset(
    {
        "success", "SESSION_ID", "agent_messages", "truncated",
        "error", "all_messages", "diagnostics",
    }
)
_ERROR_KEYS: frozenset[str] = frozenset(
    {"code", "stage", "retryable", "raw", "stderr_tail"}
)

# Categories whose root cause maps deterministically to one classify code.
# Buckets not listed here are validated by the structural assertions only.
EXPECTED_CODE: dict[str, frozenset[str]] = {
    "codex-503":                 frozenset({"proxy_503"}),
    "codex-401":                 frozenset({"auth_401"}),
    "codex-stream-disconnected": frozenset({"stream_disconnected"}),
    "codex-cli-too-long":        frozenset({"cli_too_long"}),
    "gemini-stop-self":          frozenset({"model_refused"}),
    "gemini-no-session-id":      frozenset({"no_session"}),
    "gemini-winerror-206":       frozenset({"cli_too_long"}),
}

# Legacy GuDaStudio dict-shape signatures. None of these can appear anywhere
# in the new envelope's serialisation — they encode the very wrapper bug we
# are replacing.
_LEGACY_DICT_SIGNATURES: tuple[str, ...] = (
    "Failed to get `agent_messages` from",
    "Failed to retrieve `agent_messages` data from",
    "Failed to get `SESSION_ID` from",
)


def _load_fixtures() -> list[dict]:
    return list(json.loads(_FIXTURES_PATH.read_text(encoding="utf-8"))["fixtures"])


_FIXTURES = _load_fixtures()


def _parse(
    *,
    session_id: str | None = None,
    saw_terminal: bool = False,
    agent_messages: str = "",
    all_messages: list[dict] | None = None,
    noise_log: list[str] | None = None,
    error_events: list[dict] | None = None,
) -> ParseResult:
    return ParseResult(
        session_id=session_id,
        agent_messages=agent_messages,
        all_messages=list(all_messages or []),
        noise_log=list(noise_log or []),
        transient_lines=[],
        saw_terminal=saw_terminal,
        error_events=list(error_events or []),
    )


def _outcome(
    parse: ParseResult,
    *,
    returncode: int = 0,
    stderr_tail: str = "",
    timed_out: bool = False,
) -> RunOutcome:
    return RunOutcome(
        parse_result=parse,
        stderr_tail=stderr_tail,
        kill_chain=[],
        returncode=returncode,
        timed_out=timed_out,
    )


def _terminal_event(*, is_codex: bool, status: str = "ok") -> dict:
    return {"type": "turn.completed" if is_codex else "result", "status": status}


def _init_event(*, is_codex: bool, sid: str) -> dict:
    if is_codex:
        return {"type": "thread.started", "thread_id": sid}
    return {"type": "init", "session_id": sid, "model": "gemini"}


def _error_event(*, is_codex: bool, message: str) -> dict:
    """Spec-faithful synthetic error event. GEMINI_SPEC requires `severity`
    AND `message`; CODEX_SPEC requires only `message`."""
    if is_codex:
        return {"type": "error", "message": message}
    return {"type": "error", "severity": "error", "message": message}


def _success_outcome(
    *, is_codex: bool, sid: str, message: str, noise_log: list[str] | None = None
) -> RunOutcome:
    return _outcome(
        _parse(
            session_id=sid,
            saw_terminal=True,
            agent_messages=message,
            all_messages=[
                _init_event(is_codex=is_codex, sid=sid),
                _terminal_event(is_codex=is_codex),
            ],
            noise_log=noise_log,
        ),
        returncode=0,
    )


def _synthesize_outcome(category: str, tool_name: str, fixture_args: dict) -> RunOutcome:
    """Map (category, tool) → RunOutcome that drives the matching classify
    branch. Categories whose root cause is "wrapper missed the result"
    (MISSING bucket) get a success-path outcome; their regression target
    is "no longer empty success", proven by assertion #4."""
    is_codex = tool_name == "mcp__codex__codex"
    sid_from_args = fixture_args.get("SESSION_ID") or ""

    if category == "codex-503":
        return _outcome(
            _parse(error_events=[_error_event(is_codex=is_codex, message="503 Service Unavailable")]),
            returncode=1,
        )
    if category == "codex-401":
        return _outcome(
            _parse(error_events=[_error_event(is_codex=is_codex, message="401 Unauthorized")]),
            returncode=1,
        )
    if category == "codex-stream-disconnected":
        return _outcome(
            _parse(error_events=[
                _error_event(is_codex=is_codex, message="stream disconnected before completion")
            ]),
            returncode=1,
        )
    if category == "codex-cli-too-long":
        return _outcome(_parse(), returncode=1, stderr_tail="argument list too long")
    if category == "codex-bare-failure":
        # rc!=0, no events, no terminal → spawn_failed envelope
        return _outcome(_parse(), returncode=1)
    if category == "codex-no-result-block":
        return _success_outcome(
            is_codex=is_codex, sid="t-noresult",
            message="Recovered output via type-whitelist parser.",
        )
    if category == "codex-oversize":
        # cap_envelope truncates UTF-8 char count to 100K; truncated=True
        sid = "t-oversize" if is_codex else "g-oversize"
        return _outcome(
            _parse(
                session_id=sid, saw_terminal=True,
                agent_messages="x" * 200_000,
                all_messages=[
                    _init_event(is_codex=is_codex, sid=sid),
                    _terminal_event(is_codex=is_codex),
                ],
            ),
            returncode=0,
        )
    if category == "multi-turn-codex-fail":
        sid = sid_from_args or "t-multi-codex"
        return _success_outcome(
            is_codex=True, sid=sid,
            message="Multi-turn codex resumed cleanly via SESSION_ID.",
        )

    if category == "gemini-augment-pollution":
        return _success_outcome(
            is_codex=False, sid="g-aug",
            message="Real reply after augment pollution stripped to noise_log.",
            noise_log=[
                "Registering notification handlers for server 'augment-context-engine'.",
                "[augment-context-engine] capabilities: prompts list",
            ],
        )
    if category == "gemini-no-session-id":
        # saw_terminal but session_id never extracted → no_session (stream)
        return _outcome(
            _parse(
                session_id=None, saw_terminal=True,
                all_messages=[_terminal_event(is_codex=False)],
            ),
            returncode=0,
        )
    if category == "gemini-no-result-block":
        return _success_outcome(
            is_codex=False, sid="g-noresult",
            message="Recovered gemini reply via terminal whitelist.",
        )
    if category == "gemini-stop-self":
        sid = "t-stop" if is_codex else "g-stop"
        return _outcome(
            _parse(
                session_id=sid, saw_terminal=True,
                all_messages=[
                    _init_event(is_codex=is_codex, sid=sid),
                    _terminal_event(is_codex=is_codex, status="stopped"),
                ],
            ),
            returncode=0,
        )
    if category == "gemini-winerror-206":
        return _outcome(
            _parse(),
            returncode=1,
            stderr_tail="OSError: [WinError 206] The filename or extension is too long",
        )
    if category == "multi-turn-gemini-fail":
        sid = sid_from_args or "g-multi"
        return _success_outcome(
            is_codex=False, sid=sid,
            message="Multi-turn gemini resumed cleanly via SESSION_ID.",
        )

    raise ValueError(f"unknown category: {category!r}")


@pytest.fixture(params=_FIXTURES, ids=lambda f: f["id"])
def fixture(request) -> dict:
    return request.param


async def test_fixture_replay(fixture: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    args = dict(fixture["args"])
    category = fixture["category"]
    tool_name = fixture["tool_name"]

    # Bypass C3 pre-flight; this test pinpoints classify+envelope, not auth.
    monkeypatch.setattr(server_mod, "preflight_codex_auth", lambda: None)

    outcome = _synthesize_outcome(category, tool_name, args)

    from prism_router.dispatcher import DispatchResult

    class _FakeDispatcher:
        def __init__(self, **_): pass
        async def invoke(self, *, backend_name, prompt, cfg):
            return DispatchResult(outcome=outcome)

    monkeypatch.setattr(server_mod, "Dispatcher", _FakeDispatcher)
    # Many fixture cwds reference the source machine — pin to tmp_path so
    # path normalization can't fail per-fixture.
    monkeypatch.setattr(server_mod, "normalize_cwd", lambda _p: tmp_path)

    if tool_name == "mcp__codex__codex":
        sandbox = args.get("sandbox") or "read-only"
        result = await server_mod._invoke_codex(
            prompt=args["PROMPT"],
            cd=tmp_path,
            sandbox=sandbox,
            session_id=args.get("SESSION_ID") or "",
            skip_git_repo_check=bool(args.get("skip_git_repo_check", True)),
            return_all_messages=bool(args.get("return_all_messages", False)),
            model=args.get("model") or "",
            profile=args.get("profile") or "",
            image=args.get("image"),
            yolo=bool(args.get("yolo", False)),
            require_approval=False,
        )
    else:
        result = await server_mod._invoke_gemini(
            prompt=args["PROMPT"],
            cd=tmp_path,
            session_id=args.get("SESSION_ID") or "",
            return_all_messages=bool(args.get("return_all_messages", False)),
            model=args.get("model") or "",
            sandbox=bool(args.get("sandbox", False)),
            gemini_default_model="",
        )

    fid = fixture["id"]

    # 1) GuDaStudio leadins must not appear in agent_messages.
    msg = result.get("agent_messages") or ""
    for leadin in LEGACY_LEADINS:
        assert leadin not in msg, f"{fid}: legacy leadin {leadin!r} in agent_messages"

    # 2) Envelope shape contract (§4.2 outer 7-key + error 5-key).
    assert set(result) == _RESULT_KEYS, f"{fid}: outer keys {set(result)!r}"
    assert isinstance(result["success"], bool)
    assert isinstance(result["agent_messages"], str)
    assert isinstance(result["truncated"], bool)
    if result["success"]:
        assert result["error"] is None
    else:
        env = result["error"]
        assert env is not None
        assert set(env) == _ERROR_KEYS, f"{fid}: error keys {set(env)!r}"
        assert env["code"] in ALL_ERROR_CODES, f"{fid}: bad code {env['code']!r}"
        assert env["stage"] in ALL_STAGES, f"{fid}: bad stage {env['stage']!r}"
        assert env["retryable"] in (True, False)
        assert isinstance(env["raw"], str)
        # stderr_tail can be None or str; either is contract-legal.
        assert env["stderr_tail"] is None or isinstance(env["stderr_tail"], str)

    # 3) Category-specific code (deterministic root-cause buckets only).
    if category in EXPECTED_CODE:
        assert not result["success"], (
            f"{fid}: category {category} expected error envelope, got success"
        )
        assert result["error"]["code"] in EXPECTED_CODE[category], (
            f"{fid}: category {category} got {result['error']['code']!r}, "
            f"expected one of {set(EXPECTED_CODE[category])}"
        )

    if category == "codex-oversize":
        assert result["truncated"] is True, f"{fid}: oversize must truncate"
        assert len(result["agent_messages"]) == 100_000

    # 4) MISSING bucket: empty success is the legacy regression.
    if fixture["original_raw_kind"] == "MISSING":
        if result["success"]:
            assert result["agent_messages"], (
                f"{fid}: empty success on MISSING fixture is a regression"
            )

    # 5) Legacy GuDaStudio leadins + dict signatures absent from full envelope.
    # No `default=str` — any non-JSON-serializable value sneaking into the
    # envelope would raise here (regression itself).
    serialised = json.dumps(result, ensure_ascii=False)
    for leadin in LEGACY_LEADINS:
        assert leadin not in serialised, f"{fid}: legacy leadin {leadin!r} leaked into envelope"
    for sig in _LEGACY_DICT_SIGNATURES:
        assert sig not in serialised, f"{fid}: legacy dict signature {sig!r} leaked"
