"""§3.1 gemini backend: argv assembly only.

Verified 2026-05-03 against google-gemini/gemini-cli@main
`packages/cli/src/config/config.ts`:
- `-o/--output-format stream-json` (L456)
- `-r/--resume <id>` (L395; bare `--resume` defaults to "latest")
- `-m/--model` (L282), `-y/--yolo` (L327), `-s/--sandbox` (L321; bool flag)
- NO `--cwd` flag — runner sets subprocess `cwd=` directly. Plan §3.1 deviation.

PROMPT via stdin natively per §9 E1 (`echo "..." | gemini`); never `-p` (E2 — emits
deprecation warning to stdout, polluting JSON).
"""

from __future__ import annotations

from .backend import SpawnConfig
from .events import GEMINI_SPEC, EventSpec


class GeminiBackend:
    name: str = "gemini"
    command: str = "gemini"
    spec: EventSpec = GEMINI_SPEC

    def build_args(self, cfg: SpawnConfig) -> list[str]:
        args: list[str] = [self.command, "-o", "stream-json"]
        if cfg.session_id:
            args += ["-r", cfg.session_id]
        if cfg.model:
            args += ["-m", cfg.model]
        if cfg.sandbox:  # codex-style "" / non-empty maps to gemini bool flag
            args.append("-s")
        if cfg.yolo:
            args.append("-y")
        return args
