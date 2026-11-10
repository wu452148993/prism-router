r"""Transport escape for gemini-cli's at-command scanner.

gemini-cli scans the whole prompt for `@<path>` references with a regex whose
quoted alternative does not exclude newlines, so one at-sign inside quoted text
swallows thousands of characters across lines and yields a pseudo-path. Each one
enters a validate -> extract -> recursive workspace glob pipeline that saturates
a CPU core: the request is never sent and stdout stays at the `init` frame until
the overall timeout. Reproduces at 8 KB — not a size effect, not ReDoS.

The match is guarded by a `(?<!\\)` lookbehind, so prefixing every at-sign with
a backslash skips at-command processing entirely.
See docs/bugs/gemini-at-command-hang.md.
"""

from __future__ import annotations

# Prepended AFTER escaping, so it is NOT itself escaped: it must contain no
# literal at-sign or it becomes a fresh match start for the very scanner this
# module defeats. ASCII, single line.
AT_ESCAPE_NOTE = (
    "[prism-router] Backslash-at sequences below are transport escapes inserted "
    "by the wrapper; read each as a plain at-sign. They are not part of the "
    "original content."
)


def escape_at(prompt: str) -> tuple[str, int]:
    r"""Prefix every at-sign with a backslash; return (escaped, count).

    Unconditional. The lookbehind inspects only the character immediately before
    the at-sign, so already-escaped input (`\@` -> `\\@`) stays suppressed.
    Parse-aware selective escaping was measured unsafe: whether the CLI hangs
    depends on how far the upstream regex runs, not on whether the token we
    sliced resolves to a real file.
    """
    n = prompt.count("@")
    if not n:
        return prompt, 0
    return prompt.replace("@", "\\@"), n
