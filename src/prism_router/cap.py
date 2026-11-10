"""§6.1 100K UTF-8 hard cap on `agent_messages`.

By Unicode code-point count (not bytes / not grapheme clusters). Python str
indexing operates on code points, so any slice on a `str` is multibyte-safe by
construction — emoji and CJK are never split mid-byte.

Returns `(text, truncated)` so the caller can set `truncated:true` on the
envelope without re-checking length.
"""

from __future__ import annotations

CAP_CHARS = 100_000


def cap_envelope(text: str, *, limit_chars: int = CAP_CHARS) -> tuple[str, bool]:
    if len(text) <= limit_chars:
        return text, False
    return text[:limit_chars], True
