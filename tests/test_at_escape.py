r"""`escape_at` — unconditional at-sign transport escape for the gemini arm.

docs/bugs/gemini-at-command-hang.md
"""

from __future__ import annotations

import pytest

from prism_router.at_escape import AT_ESCAPE_NOTE, escape_at


@pytest.mark.parametrize(
    ("raw", "expected", "count"),
    [
        ("", "", 0),
        ("no at signs here", "no at signs here", 0),
        ("@", r"\@", 1),
        ("@a @b", r"\@a \@b", 2),
        ('"@scope/pkg": "workspace:*",', r'"\@scope/pkg": "workspace:*",', 1),
        ("a@@b", r"a\@\@b", 2),
        ("mail@example.com", r"mail\@example.com", 1),
    ],
)
def test_escapes_every_at_sign_and_reports_the_count(raw: str, expected: str, count: int):
    assert escape_at(raw) == (expected, count)


def test_already_escaped_input_is_escaped_again():
    r"""Pinned behavior, not an oversight: the upstream lookbehind inspects only
    the character immediately before the at-sign, so `\\@` suppresses the match
    exactly as `\@` does. Idempotence would require parsing the payload — the
    approach measured unsafe in the bug doc."""
    assert escape_at(r"\@path") == (r"\\@path", 1)
    assert escape_at(r"\\@path") == (r"\\\@path", 1)


def test_no_at_sign_returns_the_input_object_unchanged():
    """No-op fidelity: the knob-on / zero-at-sign payload must reach the
    dispatcher byte-identical to the pre-fix build."""
    raw = "## Diff\n- foo\n+ bar\n"
    escaped, n = escape_at(raw)
    assert n == 0
    assert escaped is raw


def test_only_at_signs_change():
    raw = "".join(chr(c) for c in range(1, 0x2500))
    escaped, n = escape_at(raw)
    assert n == raw.count("@") == 1
    assert escaped.replace(r"\@", "@") == raw
    assert len(escaped) == len(raw) + n


def test_note_carries_no_at_sign():
    """The note is prepended AFTER escaping, so it is delivered verbatim; a
    literal at-sign in it would become a fresh match start for the very scanner
    the escape defeats."""
    assert "@" not in AT_ESCAPE_NOTE
    assert "\n" not in AT_ESCAPE_NOTE
    assert AT_ESCAPE_NOTE.isascii()
