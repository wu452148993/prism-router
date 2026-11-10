"""§3.1 codex backend: argv assembly only.

Verified 2026-05-03 against openai/codex@main:
- `codex-rs/exec/src/cli.rs` — `exec [OPTIONS] [PROMPT|-]` and `exec resume <SID>` subcmd
- `codex-rs/utils/cli/src/shared_options.rs` — flag spellings (--cd/-C, --sandbox/-s,
  --profile/-p, --model/-m, --image/-i)
- `codex-rs/utils/cli/src/sandbox_mode_cli_arg.rs` — sandbox enum kebab-case

Ordering rule: all flags placed BETWEEN `exec` and `resume` (parent scope). Resume
subcommand only takes <SESSION_ID> + trailing PROMPT positional. PROMPT is `-`
always — fed via stdin per §9 V1.
"""

from __future__ import annotations

from .backend import SpawnConfig
from .events import CODEX_SPEC, EventSpec


class CodexBackend:
    name: str = "codex"
    command: str = "codex"
    spec: EventSpec = CODEX_SPEC

    def build_args(self, cfg: SpawnConfig) -> list[str]:
        args: list[str] = [self.command, "exec", "--json"]
        if cfg.skip_git_repo_check:
            args.append("--skip-git-repo-check")
        args += ["-C", str(cfg.cwd)]
        if cfg.sandbox:
            args += ["--sandbox", cfg.sandbox]
        if cfg.model:
            args += ["--model", cfg.model]
        if cfg.profile:
            args += ["--profile", cfg.profile]
        for img in cfg.images:
            args += ["--image", str(img)]
        if cfg.yolo:
            args.append("--dangerously-bypass-approvals-and-sandbox")
        if cfg.session_id:
            # Unknown SIDs may silently start a fresh codex thread; callers that care
            # compare request/response SESSION_ID (see design-rationale §4.2).
            args += ["resume", cfg.session_id]
        args.append("-")
        return args
