"""Backend.build_args matrix. Pure: no IO, no spawn."""

from __future__ import annotations

from pathlib import Path

import pytest

from prism_router.backend import SpawnConfig
from prism_router.backend_codex import CodexBackend
from prism_router.backend_gemini import GeminiBackend


CWD = Path("/tmp/proj")


def cfg(**overrides) -> SpawnConfig:
    base = dict(
        cwd=CWD,
        sandbox="",
        session_id=None,
        model="",
        profile="",
        skip_git_repo_check=False,
        yolo=False,
        images=(),
    )
    base.update(overrides)
    return SpawnConfig(**base)


# ───────────────────────── codex ─────────────────────────


def test_codex_minimal():
    args = CodexBackend().build_args(cfg())
    assert args == ["codex", "exec", "--json", "-C", str(CWD), "-"]


def test_codex_skip_git_position_before_cd():
    args = CodexBackend().build_args(cfg(skip_git_repo_check=True))
    # Order: exec --json --skip-git-repo-check -C <cwd> -
    assert args.index("--skip-git-repo-check") < args.index("-C")


def test_codex_sandbox_passes_through():
    args = CodexBackend().build_args(cfg(sandbox="workspace-write"))
    assert "--sandbox" in args
    assert args[args.index("--sandbox") + 1] == "workspace-write"


def test_codex_model_and_profile():
    args = CodexBackend().build_args(cfg(model="gpt-5", profile="dev"))
    assert args[args.index("--model") + 1] == "gpt-5"
    assert args[args.index("--profile") + 1] == "dev"


def test_codex_images_repeat_flag():
    args = CodexBackend().build_args(cfg(images=(Path("a.png"), Path("b.png"))))
    # Per §3.1: -i FILE per image (codex CLI accepts repeated flag).
    assert args.count("--image") == 2


def test_codex_yolo_flag():
    args = CodexBackend().build_args(cfg(yolo=True))
    assert "--dangerously-bypass-approvals-and-sandbox" in args


def test_codex_resume_when_session_id():
    args = CodexBackend().build_args(cfg(session_id="abc-123"))
    # Order: ... resume abc-123 -
    assert args[-3:] == ["resume", "abc-123", "-"]


def test_codex_resume_with_flags_keeps_options_before_resume():
    args = CodexBackend().build_args(
        cfg(session_id="abc", sandbox="read-only", model="m", yolo=True)
    )
    # All flag tokens must appear before "resume" (clap subcommand semantics).
    resume_idx = args.index("resume")
    flags = ("--sandbox", "--model", "--dangerously-bypass-approvals-and-sandbox")
    for f in flags:
        assert args.index(f) < resume_idx


def test_codex_prompt_dash_always_last():
    # Without resume.
    assert CodexBackend().build_args(cfg())[-1] == "-"
    # With resume.
    assert CodexBackend().build_args(cfg(session_id="x"))[-1] == "-"


def test_codex_never_emits_prompt_flag():
    args = CodexBackend().build_args(
        cfg(session_id="x", model="m", profile="p", yolo=True, images=(Path("a"),))
    )
    # `-p` is `--profile` short, allowed; only `--prompt` long form is forbidden.
    assert "--prompt" not in args


# ───────────────────────── gemini ─────────────────────────


def test_gemini_minimal():
    args = GeminiBackend().build_args(cfg())
    assert args == ["gemini", "-o", "stream-json"]


def test_gemini_no_cwd_flag_emitted():
    # Verified 2026-05-03: gemini-cli has no --cwd flag; runner sets subprocess cwd=.
    args = GeminiBackend().build_args(cfg(cwd=Path("D:/projects/x")))
    assert "--cwd" not in args
    assert "/projects/x" not in " ".join(args)


def test_gemini_resume_short_flag():
    args = GeminiBackend().build_args(cfg(session_id="sid-9"))
    assert args[args.index("-r") + 1] == "sid-9"


def test_gemini_model():
    args = GeminiBackend().build_args(cfg(model="gemini-2.5-pro"))
    assert args[args.index("-m") + 1] == "gemini-2.5-pro"


def test_gemini_sandbox_bool_flag():
    args = GeminiBackend().build_args(cfg(sandbox="any-nonempty"))
    assert "-s" in args
    # Confirm no value follows -s (gemini -s is bool, not value-taking)
    s_idx = args.index("-s")
    if s_idx + 1 < len(args):
        # Allowed if next is another flag or end.
        assert args[s_idx + 1].startswith("-") or s_idx + 1 == len(args) - 1 or args[s_idx + 1] == "-y"


def test_gemini_yolo():
    args = GeminiBackend().build_args(cfg(yolo=True))
    assert "-y" in args


def test_gemini_never_emits_prompt_flag():
    args = GeminiBackend().build_args(
        cfg(session_id="x", model="m", sandbox="x", yolo=True)
    )
    assert "-p" not in args
    assert "--prompt" not in args


def test_gemini_ignores_codex_only_fields():
    # profile / images / skip_git_repo_check have no gemini equivalent.
    args = GeminiBackend().build_args(
        cfg(profile="dev", images=(Path("a.png"),), skip_git_repo_check=True)
    )
    assert "--profile" not in args
    assert "--image" not in args
    assert "--skip-git-repo-check" not in args
