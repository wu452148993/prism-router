"""FastMCP entry. M3 wires C7 smoke probe + codex/gemini tool registration.

v1 P0 thinness: validate inputs, dispatch to backend, return §4.2 envelope.
No daemon, no shim. Single MCP server, two tools. The one mid-call egress is
evidence-gated liveness progress (docs/bugs/host-idle-progress-egress.md).
Per-call lineage rows go to a local file, never to the client (docs/lineage.md).

C7 (tool surface attribution): each backend's `--version` is probed at startup.
On failure the corresponding `@mcp.tool` registration is skipped — Claude Code
sees its native "no such tool" rather than a wrapper-fabricated error envelope.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Awaitable, Callable, Literal, TypeVar

import anyio
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context

from . import lineage
from ._liveness import SEND_TIMEOUT_S, LivenessEgress
from .at_escape import AT_ESCAPE_NOTE, escape_at
from .auth import preflight_codex_auth
from .backend import SpawnConfig
from .backend_codex import CodexBackend
from .backend_gemini import GeminiBackend
from .cap import cap_envelope
from .classify import classify, is_transient_empty_stream
from .config import ServerConfig
from .dispatcher import OVERALL_TIMEOUT_S, ChildCallback, Dispatcher, LivenessCallback
from .errors import (
    CodexResult,
    Diagnostics,
    ErrorEnvelope,
    GeminiResult,
    PromptFileReceipt,
    make_error,
)
from .paths import normalize_cwd
from .prompt_file import load_prompt_file
from .protocol import ORCHESTRATOR_SIDE, inject_agent_side, warn_if_oversized
from .runner import RunOutcome

_LOG = logging.getLogger("prism_router")


def smoke_probe(cmd: str, *, timeout_sec: float = 5.0) -> bool:
    """`cmd --version` exits 0 within timeout (C7).

    Uses `shutil.which` so PATH lookup matches subprocess semantics. Windows
    suppresses the console flash via CREATE_NO_WINDOW. stdin=DEVNULL keeps the
    child off the JSON-RPC pipe (docs/bugs/prompt-file-git-dir-stdin-hang.md).
    """
    binary = shutil.which(cmd)
    if binary is None:
        return False
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            [binary, "--version"],
            stdin=subprocess.DEVNULL,
            timeout=timeout_sec,
            capture_output=True,
            check=False,
            creationflags=flags,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


# Whitelist of dispatcher diagnostic keys that may surface on the public
# envelope. Internal fields (stderr_tail, transport_error, kill_reason, etc.)
# stay private. See docs/bugs/rollout-recovery.md.
_PUBLIC_DIAG_KEYS = (
    "recovered_from_rollout", "rollout_path", "max_stdout_silence_s",
    "internal_retries", "rollout_ticks", "rollout_watched_s",
    "last_rollout_growth_age_s", "rollout_observation_stale",
)

# Gemini transient empty/malformed-stream retry (single re-dispatch).
# docs/bugs/gemini-transient-empty-stream-retry.md
_GEMINI_TRANSIENT_RETRY_DELAY_S = 1.0

# gemini-cli swallows InvalidStreamError on the final round: turn.js converts
# it to an InvalidStream event that nonInteractiveCli has no case for, so the
# CLI exits 0 with clean stderr and an empty final message. The event never
# reaches stdout — detection is inferential: error-free outcome + empty
# assembled agent_messages. Shares the single-retry budget above; fails
# closed on exhaustion. docs/bugs/gemini-empty-final-fail-closed.md
_GEMINI_EMPTY_FINAL_RAW = (
    "gemini exited success with an empty final message: gemini-cli swallows "
    "InvalidStreamError (the InvalidStream event is dropped before stdout; "
    "stderr stays clean) so an empty final round masquerades as success. "
    "One identical re-dispatch also returned empty. "
    "See docs/bugs/gemini-empty-final-fail-closed.md"
)


# Codex counterpart of the gemini gate above: a turn that ends with no
# `item.completed{agent_message}` and no error event. Never observed in 815
# recorded codex calls (a dead codex turn pairs with an `error` event), so this
# is drift insurance rather than a fix — and unlike gemini it is NOT retried:
# a codex turn costs minutes and an empty final has no observed transient
# cause. `retryable=False` for the same reason: the flag is a caller hint, and
# telling a caller to repeat a multi-minute turn (whose tool side effects have
# already run) contradicts the rationale for skipping the internal retry.
# docs/bugs/codex-transient-retry-storm.md
_CODEX_EMPTY_FINAL_RAW = (
    "codex exited success with an empty final message: the turn produced no "
    "agent_message item and no error event. Not retried — an empty codex final "
    "has no observed transient cause. "
    "See docs/bugs/codex-transient-retry-storm.md"
)


def _diagnostics(
    outcome: RunOutcome, backend_name: str, extra: dict | None = None,
) -> Diagnostics:
    chain = [dataclasses.asdict(s) for s in outcome.kill_chain] or None
    diag: Diagnostics = Diagnostics(
        noise_log=list(outcome.parse_result.noise_log),
        kill_chain=chain,
        backend_pid=None,
        backend_chosen=backend_name,
    )
    if extra:
        for key in _PUBLIC_DIAG_KEYS:
            if key in extra:
                diag[key] = extra[key]  # type: ignore[literal-required]
    return diag


def _envelope(
    *,
    backend_name: str,
    error: ErrorEnvelope | None,
    outcome: RunOutcome | None,
    return_all_messages: bool,
    extra_diagnostics: dict | None = None,
    prompt_file_receipt: PromptFileReceipt | None = None,
) -> dict:
    """Build the §4.2 result dict. Same shape for CodexResult / GeminiResult.

    `extra_diagnostics` carries dispatcher-owned recovery state (e.g.,
    `recovered_from_rollout`) and forces diagnostics emission so callers
    can tell a recovered success from a normal one. Only whitelisted keys
    surface — internal dispatcher fields stay private.

    `prompt_file_receipt` is attached iff a `prompt_file` was read and
    injected; absent otherwise so PROMPT-only envelopes stay byte-identical.
    """
    if outcome is None:
        # Pre-flight short-circuit: no spawn occurred. Diagnostics only when
        # an internal retry preceded this error — the retry must stay visible
        # even when the second attempt dies before producing an outcome.
        diag = None
        if extra_diagnostics and extra_diagnostics.get("internal_retries"):
            diag = Diagnostics(
                noise_log=[], kill_chain=None, backend_pid=None,
                backend_chosen=backend_name,
            )
            diag["internal_retries"] = extra_diagnostics["internal_retries"]
        env = dict(
            success=False,
            SESSION_ID=None,
            agent_messages="",
            truncated=False,
            error=error,
            all_messages=None,
            diagnostics=diag,
        )
        if prompt_file_receipt is not None:
            env["prompt_file_receipt"] = prompt_file_receipt
        return env
    pr = outcome.parse_result
    capped, truncated = cap_envelope(pr.agent_messages)
    # Force diagnostics emission ONLY for rollout-recovered and internally
    # retried successes — the paths that otherwise look indistinguishable
    # from normal success to callers. Narrow gate: `max_stdout_silence_s`
    # alone is added on every normal dispatcher success too; widening to any
    # whitelisted key would leak diagnostics into every default envelope.
    has_extra = bool(extra_diagnostics) and (
        extra_diagnostics.get("recovered_from_rollout") is True
        or bool(extra_diagnostics.get("internal_retries"))
    )
    diag = (
        _diagnostics(outcome, backend_name, extra_diagnostics)
        if (return_all_messages or error is not None or has_extra)
        else None
    )
    all_msgs = list(pr.all_messages) if return_all_messages else None

    env = dict(
        success=error is None,
        SESSION_ID=pr.session_id,
        agent_messages=capped,
        truncated=truncated,
        error=error,
        all_messages=all_msgs,
        diagnostics=diag,
    )
    if prompt_file_receipt is not None:
        env["prompt_file_receipt"] = prompt_file_receipt
    return env


# ───── liveness progress egress (docs/bugs/host-idle-progress-egress.md) ─────

# The host's DEFAULT MCP idle cap. A host configured longer (or with the cap
# disabled) leaves prism's own overall watchdog as the authoritative bound.
_HOST_IDLE_CAP_S = 1800.0
# Attempt deadline. Subtracting the send timeout places the LATEST successful
# delivery no later than OVERALL_TIMEOUT_S - _HOST_IDLE_CAP_S, so the host's
# idle clock runs again in time to reap any residual occupation by
# ~overall_timeout. Derived from the dispatcher constant — single-sourced.
EGRESS_MAX_S = max(0.0, OVERALL_TIMEOUT_S - _HOST_IDLE_CAP_S - SEND_TIMEOUT_S)

_T = TypeVar("_T")


async def _run_with_liveness(
    enabled: bool,
    invoke: Callable[[LivenessCallback | None], Awaitable[_T]],
) -> _T:
    """Run `invoke` under one request-scoped `LivenessEgress`.

    The Context is captured HERE, inside the tool's dynamic extent, where its
    ContextVar is guaranteed active — tool signatures stay untouched and the
    `_invoke_*` helpers stay independently testable. Send tasks are spawned
    into a task group nested inside the request's cancel scope: the SDK sends
    its cancellation error response independently of handler cleanup, so
    ordering can only be established structurally, not by handler-local
    teardown.

    Lifecycle branches are explicit: normal return and non-cancellation
    exceptions BOTH await the `aclose` barrier before FastMCP can emit any
    response; `CancelledError` aborts synchronously and re-raises undelayed;
    `finally` aborts as an idempotent backstop. The exception is re-raised
    outside the task group so its type and traceback reach FastMCP unwrapped.
    """
    sink = _progress_sink() if enabled else None
    if sink is None:
        return await invoke(None)

    result: _T
    settled = False
    failure: BaseException | None = None
    async with anyio.create_task_group() as tg:
        egress = LivenessEgress(sink, spawn=tg.start_soon, max_s=EGRESS_MAX_S)
        try:
            result = await invoke(egress.note)
        except asyncio.CancelledError:
            egress.abort()
            raise
        except BaseException as exc:  # noqa: BLE001 — re-raised verbatim below
            failure = exc  # captured BEFORE the barrier: a cancel during the
            await egress.aclose()  # drain must not erase what actually failed
        else:
            await egress.aclose()
            settled = True
        finally:
            egress.abort()
    if failure is not None:
        raise failure
    if not settled:
        # Only this group's OWN cancellation reaches here — anyio cancels it
        # whenever a child ends in CancelledError, and `__aexit__` swallows
        # that. Request and transport cancellations stay visible in a parent
        # scope and never get this far. NOT CancelledError: the SDK reads a
        # cancellation the client never asked for as transport shutdown and
        # re-raises it into the server task group (lowlevel/server.py:773-782),
        # killing the session. This must fail one request, not the server.
        raise RuntimeError("liveness task group cancelled unexpectedly")
    return result


def _progress_sink() -> Callable[[float, str], Awaitable[None]] | None:
    """Bind `ctx.report_progress` for this request, or None when no Context is
    active (direct `_invoke_*` calls, or a fastmcp regression — degrading to
    today's silent behavior beats failing the call)."""
    try:
        ctx = get_context()
    except RuntimeError:
        return None

    async def sink(progress: float, message: str) -> None:
        await ctx.report_progress(progress=progress, total=None, message=message)

    return sink


async def _invoke_codex(
    *,
    prompt: str,
    cd: Path,
    sandbox: str,
    session_id: str,
    skip_git_repo_check: bool,
    return_all_messages: bool,
    model: str,
    profile: str,
    image: list[Path] | None,
    yolo: bool,
    require_approval: bool,
    prompt_file: str = "",
    prompt_file_sha256: str = "",
    on_liveness: LivenessCallback | None = None,
    on_child: ChildCallback | None = None,
) -> CodexResult:
    cwd = normalize_cwd(cd)

    receipt = None
    if prompt_file:
        # Off the event loop: blocking file reads plus a git spawn
        # (docs/bugs/prompt-file-git-dir-stdin-hang.md).
        loaded = await asyncio.to_thread(
            load_prompt_file, prompt_file, cwd=cwd, expected_sha256=prompt_file_sha256,
        )
        if loaded.error is not None:
            return _envelope(  # type: ignore[return-value]
                backend_name="codex", error=loaded.error, outcome=None,
                return_all_messages=return_all_messages,
            )
        prompt = f"{prompt}\n\n{loaded.content}"
        receipt = loaded.receipt

    # A `--profile` overlay can switch the provider; pre-flight reads only the
    # base config (docs/bugs/codex-preflight-custom-provider.md).
    auth_err = None if profile else preflight_codex_auth()
    if auth_err is not None:
        return _envelope(  # type: ignore[return-value]
            backend_name="codex", error=auth_err, outcome=None,
            return_all_messages=return_all_messages,
            prompt_file_receipt=receipt,
        )

    cfg = SpawnConfig(
        cwd=cwd,
        sandbox=sandbox,
        session_id=session_id or None,
        model=model,
        profile=profile,
        skip_git_repo_check=skip_git_repo_check,
        yolo=yolo or not require_approval,
        images=tuple(image or ()),
    )
    backend = CodexBackend()
    prompt = inject_agent_side(prompt)
    warn_if_oversized(prompt, backend_name="codex", cwd=cwd)
    result = await Dispatcher(on_liveness=on_liveness, on_child=on_child).invoke(
        backend_name="codex", prompt=prompt, cfg=cfg,
    )
    if result.error is not None and result.outcome is None:
        return _envelope(  # type: ignore[return-value]
            backend_name="codex", error=result.error, outcome=None,
            return_all_messages=return_all_messages,
            prompt_file_receipt=receipt,
        )
    outcome = result.outcome
    assert outcome is not None
    error = classify(
        outcome.parse_result,
        spec=backend.spec,
        returncode=outcome.returncode,
        stderr_tail=outcome.stderr_tail,
        timed_out=outcome.timed_out,
        post_terminal_killed=any(s.reason == "post_terminal" for s in outcome.kill_chain),
        kill_reasons={s.reason for s in outcome.kill_chain},
    )
    # Fail closed on an error-free empty final: silence must never read as a
    # verdict in the review pipelines this wrapper feeds.
    if error is None and not outcome.parse_result.agent_messages.strip():
        error = make_error(
            "empty_final", "classify", retryable=False,
            raw=_CODEX_EMPTY_FINAL_RAW,
            stderr_tail=outcome.stderr_tail or None,
        )
    return _envelope(  # type: ignore[return-value]
        backend_name="codex", error=error, outcome=outcome,
        return_all_messages=return_all_messages,
        extra_diagnostics=result.diagnostics,
        prompt_file_receipt=receipt,
    )


async def _invoke_gemini(
    *,
    prompt: str,
    cd: Path,
    session_id: str,
    return_all_messages: bool,
    model: str,
    sandbox: bool,
    gemini_default_model: str,
    yolo: bool = False,
    require_approval: bool = True,
    prompt_file: str = "",
    prompt_file_sha256: str = "",
    at_escape: bool = True,
    on_liveness: LivenessCallback | None = None,
    on_child: ChildCallback | None = None,
) -> GeminiResult:
    cwd = normalize_cwd(cd)

    receipt = None
    if prompt_file:
        # Off the event loop: blocking file reads plus a git spawn
        # (docs/bugs/prompt-file-git-dir-stdin-hang.md).
        loaded = await asyncio.to_thread(
            load_prompt_file, prompt_file, cwd=cwd, expected_sha256=prompt_file_sha256,
        )
        if loaded.error is not None:
            return _envelope(  # type: ignore[return-value]
                backend_name="gemini", error=loaded.error, outcome=None,
                return_all_messages=return_all_messages,
            )
        prompt = f"{prompt}\n\n{loaded.content}"
        receipt = loaded.receipt

    # Escape ONCE, here: after the prompt_file merge so the whole payload is
    # covered, before `inject_agent_side` so the note reads ahead of the escaped
    # body, and outside the retry loop below — that loop re-dispatches this same
    # string, and a second pass would turn `\@` into `\\@`. `receipt` is already
    # built from the raw file bytes, so its coverage domain does not move.
    # The count is log-only by design: `_envelope`'s `has_extra` gate admits
    # only recovery keys, so a diagnostics key would either stay invisible on a
    # normal success or force every at-bearing call to grow a diagnostics block.
    if at_escape:
        prompt, escaped = escape_at(prompt)
        if escaped:
            _LOG.info("gemini at-escape: %d at-sign(s) escaped (cwd=%s)", escaped, cwd)
            prompt = f"{AT_ESCAPE_NOTE}\n\n{prompt}"

    cfg = SpawnConfig(
        cwd=cwd,
        sandbox="1" if sandbox else "",
        session_id=session_id or None,
        model=model or gemini_default_model,
        profile="",
        skip_git_repo_check=True,
        yolo=yolo or not require_approval,
        images=(),
    )
    backend = GeminiBackend()
    prompt = inject_agent_side(prompt)
    warn_if_oversized(prompt, backend_name="gemini", cwd=cwd)
    retried = 0
    while True:
        # Same `on_liveness` across the re-dispatch: one egress per REQUEST, so
        # the progress counter stays monotonic through the internal retry.
        # Same `on_child` too: one lineage call, whose `end` carries the final id.
        result = await Dispatcher(on_liveness=on_liveness, on_child=on_child).invoke(
            backend_name="gemini", prompt=prompt, cfg=cfg,
        )
        outcome = result.outcome
        if result.error is not None and outcome is None:
            error = result.error
            break
        assert outcome is not None
        error = classify(
            outcome.parse_result,
            spec=backend.spec,
            returncode=outcome.returncode,
            stderr_tail=outcome.stderr_tail,
            timed_out=outcome.timed_out,
            post_terminal_killed=any(s.reason == "post_terminal" for s in outcome.kill_chain),
            kill_reasons={s.reason for s in outcome.kill_chain},
        )
        empty_final = error is None and not outcome.parse_result.agent_messages.strip()
        if retried or not (empty_final or is_transient_empty_stream(error)):
            break
        retried = 1
        await asyncio.sleep(_GEMINI_TRANSIENT_RETRY_DELAY_S)
    # Fail closed on retry exhaustion: an empty final message is never a
    # usable delegation answer; surfacing it as retryable lets downstream
    # unavailable→STOP disciplines catch it instead of misreading silence
    # as a no-findings verdict.
    if error is None and outcome is not None and not outcome.parse_result.agent_messages.strip():
        error = make_error(
            "empty_final", "classify", retryable=True,
            raw=_GEMINI_EMPTY_FINAL_RAW,
            stderr_tail=outcome.stderr_tail or None,
        )
    extra = result.diagnostics
    if retried:
        extra = {**(extra or {}), "internal_retries": retried}
    return _envelope(  # type: ignore[return-value]
        backend_name="gemini", error=error, outcome=outcome,
        return_all_messages=return_all_messages,
        extra_diagnostics=extra,
        prompt_file_receipt=receipt,
    )


def build_server(cfg: ServerConfig | None = None) -> FastMCP:
    """Construct FastMCP with conditional tool registration per C7 smoke probe.

    Tools are exposed only when `<cmd> --version` passes. A failed probe
    yields native "no such tool" from Claude Code — correct attribution
    without a wrapper-fabricated envelope.
    """
    cfg = cfg or ServerConfig.from_env()
    mcp = FastMCP(
        name="prism-router",
        instructions=(
            "Wrappers for codex / gemini CLIs returning typed result envelopes. "
            "Use SESSION_ID for multi-turn conversations within the same backend."
            "\n\n" + ORCHESTRATOR_SIDE
        ),
    )

    require_approval = cfg.require_approval
    gemini_default = cfg.gemini_model
    gemini_require_approval = cfg.gemini_require_approval
    gemini_at_escape = cfg.gemini_at_escape
    liveness = cfg.liveness_progress

    # C7 probes run concurrently so gemini's Node cold start (~3s idle, longer
    # under load) does not serialize behind codex and stack toward Claude Code's
    # MCP init ceiling. See docs/bugs/gemini-smoke-probe-timeout.md.
    with ThreadPoolExecutor(max_workers=2) as pool:
        codex_fut = pool.submit(smoke_probe, "codex", timeout_sec=cfg.codex_probe_timeout)
        gemini_fut = pool.submit(smoke_probe, "gemini", timeout_sec=cfg.gemini_probe_timeout)
        codex_ok, gemini_ok = codex_fut.result(), gemini_fut.result()

    if codex_ok:
        @mcp.tool
        async def codex(
            PROMPT: str,
            cd: Path,
            sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "read-only",
            SESSION_ID: str = "",
            skip_git_repo_check: bool = True,
            return_all_messages: bool = False,
            model: str = "",
            profile: str = "",
            image: list[Path] | None = None,
            yolo: bool = False,
            prompt_file: str = "",
            prompt_file_sha256: str = "",
        ) -> CodexResult:
            """Spawn `codex exec` with PROMPT via stdin; return §4.2 envelope.

            `agent_messages` is canonical; use `return_all_messages=True`
            when raw backend events are needed for debugging.

            `prompt_file`: path to a UTF-8 evidence file (absolute or
            relative to `cd`; must resolve under `cd` or its git dir, e.g.
            `.git/acf/**`). The WRAPPER reads it and sends
            `PROMPT + "\\n\\n" + content` on stdin — the model needs no
            filesystem access to the file. Any failure (missing, boundary,
            non-regular, oversize, non-UTF-8, sha256 mismatch) returns a
            pre-spawn `prompt_file_error` envelope: the backend is never
            spawned and no SESSION_ID is produced. On success the envelope
            carries `prompt_file_receipt` {path, sha256, bytes} computed by
            the wrapper. `prompt_file_sha256` optionally pins the expected
            lowercase-hex sha256 of the file bytes.
            """
            return await lineage.track("codex", cd, SESSION_ID, lambda child: _run_with_liveness(
                liveness, lambda note: _invoke_codex(
                    prompt=PROMPT, cd=cd, sandbox=sandbox, session_id=SESSION_ID,
                    skip_git_repo_check=skip_git_repo_check,
                    return_all_messages=return_all_messages,
                    model=model, profile=profile, image=image, yolo=yolo,
                    require_approval=require_approval,
                    prompt_file=prompt_file, prompt_file_sha256=prompt_file_sha256,
                    on_liveness=note, on_child=child,
                ),
            ))
    else:
        _LOG.warning("codex --version smoke probe failed; tool not registered")

    if gemini_ok:
        @mcp.tool
        async def gemini(
            PROMPT: str,
            cd: Path,
            SESSION_ID: str = "",
            return_all_messages: bool = False,
            model: str = "",
            sandbox: bool = False,
            yolo: bool = False,
            prompt_file: str = "",
            prompt_file_sha256: str = "",
        ) -> GeminiResult:
            """Spawn `gemini -o stream-json` with PROMPT via stdin; §4.2 envelope.

            `agent_messages` is canonical; use `return_all_messages=True`
            when raw backend events are needed for debugging.

            `prompt_file`: path to a UTF-8 evidence file (absolute or
            relative to `cd`; must resolve under `cd` or its git dir, e.g.
            `.git/acf/**`). The WRAPPER reads it and sends
            `PROMPT + "\\n\\n" + content` on stdin — the model needs no
            filesystem access to the file. Any failure (missing, boundary,
            non-regular, oversize, non-UTF-8, sha256 mismatch) returns a
            pre-spawn `prompt_file_error` envelope: the backend is never
            spawned and no SESSION_ID is produced. On success the envelope
            carries `prompt_file_receipt` {path, sha256, bytes} computed by
            the wrapper. `prompt_file_sha256` optionally pins the expected
            lowercase-hex sha256 of the file bytes.

            By default, at-signs in the payload are delivered backslash-escaped,
            disabling gemini's `@path` expansion shortcut — upstream at-command
            scanning hangs the CLI on ordinary quoted text
            (docs/bugs/gemini-at-command-hang.md). Inline the content or pass
            `prompt_file`; to have the model open a file itself, say so in
            PROMPT and let it use its own read tools.

            `yolo`: gemini runs WITHOUT `--yolo` by default, so on a stock
            operator config it declares read-only tools only (read_file /
            grep_search / glob / list_directory / google_web_search). Pass
            `yolo=true` only when the task genuinely needs a mutating agent:
            it additionally declares write_file / replace /
            run_shell_command / web_fetch / background-process / MCP /
            skill tools. Those are not confined to `cd` — it is only the
            starting directory — so at the default `sandbox=false` a shell
            command reaches whatever the host account can.
            """
            return await lineage.track("gemini", cd, SESSION_ID, lambda child: _run_with_liveness(
                liveness, lambda note: _invoke_gemini(
                    prompt=PROMPT, cd=cd, session_id=SESSION_ID,
                    return_all_messages=return_all_messages,
                    model=model, sandbox=sandbox,
                    gemini_default_model=gemini_default,
                    yolo=yolo, require_approval=gemini_require_approval,
                    prompt_file=prompt_file, prompt_file_sha256=prompt_file_sha256,
                    at_escape=gemini_at_escape,
                    on_liveness=note, on_child=child,
                ),
            ))
    else:
        _LOG.warning("gemini --version smoke probe failed; tool not registered")

    return mcp


def main() -> int:
    cfg = ServerConfig.from_env()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    lineage.configure()
    mcp = build_server(cfg)
    mcp.run()  # stdio default
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
