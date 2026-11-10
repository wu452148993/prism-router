"""§6.1 cap_envelope — UTF-8 char-count cap + multibyte safety."""

from __future__ import annotations

from prism_router.cap import CAP_CHARS, cap_envelope


def test_under_limit_passthrough():
    text, truncated = cap_envelope("hello")
    assert text == "hello"
    assert truncated is False


def test_at_limit_passthrough():
    s = "x" * CAP_CHARS
    text, truncated = cap_envelope(s)
    assert len(text) == CAP_CHARS
    assert truncated is False


def test_over_limit_truncates():
    s = "x" * (CAP_CHARS + 10)
    text, truncated = cap_envelope(s)
    assert len(text) == CAP_CHARS
    assert truncated is True


def test_emoji_not_split_midbyte():
    """Each emoji is a single code point (or surrogate pair / ZWJ sequence —
    here we use single-codepoint emoji). Python str slicing operates on code
    points; we must never cut a code point in half."""
    emoji = "🌟"  # U+1F31F, single code point
    s = emoji * (CAP_CHARS + 5)
    text, truncated = cap_envelope(s)
    assert len(text) == CAP_CHARS
    assert truncated is True
    # All chars must round-trip through utf-8 cleanly — proves no half-codepoint.
    text.encode("utf-8").decode("utf-8")


def test_cjk_counted_per_char():
    """CJK chars are 1 code point each; cap counts in chars, not bytes."""
    s = "中" * (CAP_CHARS + 3)
    text, truncated = cap_envelope(s)
    assert len(text) == CAP_CHARS
    assert truncated is True
    text.encode("utf-8").decode("utf-8")


def test_custom_limit():
    text, truncated = cap_envelope("abcdef", limit_chars=3)
    assert text == "abc"
    assert truncated is True


def test_empty_passthrough():
    text, truncated = cap_envelope("")
    assert text == ""
    assert truncated is False
