"""§3.3 path normalization tests. Platform-aware: Git Bash conversion is Windows-only."""

from __future__ import annotations

import os
import sys

import pytest

from prism_router.paths import normalize_cwd

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")


@WINDOWS_ONLY
def test_git_bash_drive_upper(tmp_path):
    target = tmp_path  # tmp_path is e.g. C:\Users\...\pytest-...
    drive = str(target)[0].lower()
    rest = str(target)[2:].replace("\\", "/")
    git_bash = f"/{drive}{rest}"
    assert str(normalize_cwd(git_bash)).lower() == os.path.normcase(str(target))


@WINDOWS_ONLY
def test_backslash_and_forward_slash_collapse(tmp_path):
    bs = str(tmp_path)
    fs = bs.replace("\\", "/")
    assert normalize_cwd(bs) == normalize_cwd(fs)
    assert str(normalize_cwd(bs)) == str(normalize_cwd(fs))


@WINDOWS_ONLY
def test_case_fold(tmp_path):
    upper = str(tmp_path)
    lower = upper.lower()
    assert str(normalize_cwd(upper)) == str(normalize_cwd(lower))


@WINDOWS_ONLY
def test_idempotent(tmp_path):
    once = normalize_cwd(tmp_path)
    twice = normalize_cwd(once)
    assert str(once) == str(twice)


@WINDOWS_ONLY
def test_git_bash_with_trailing_segments(tmp_path):
    # /c/Users/foo/bar — drive prefix stripped, rest preserved verbatim
    drive = str(tmp_path)[0]
    rest = str(tmp_path)[2:].replace("\\", "/")
    git_bash = f"/{drive.lower()}{rest}/sub/dir"
    plain = f"{drive}:{rest}/sub/dir"
    assert str(normalize_cwd(git_bash)) == str(normalize_cwd(plain))


def test_accepts_path_object(tmp_path):
    assert normalize_cwd(tmp_path) == normalize_cwd(str(tmp_path))


def test_returns_absolute(tmp_path):
    result = normalize_cwd(tmp_path)
    assert result.is_absolute()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX semantics")
def test_posix_passthrough(tmp_path):
    # On POSIX `/c/Users/foo` is a real path, not Git Bash; must not be rewritten.
    raw = "/c/Users/foo"
    assert str(normalize_cwd(raw)) == raw
