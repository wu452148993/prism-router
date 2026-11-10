"""§3.2 Backend protocol. Concrete codex/gemini in backend_codex / backend_gemini."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .events import EventSpec


@dataclass(frozen=True)
class SpawnConfig:
    cwd: Path
    sandbox: str          # codex enum string OR "" (gemini treats any non-empty as bool on)
    session_id: str | None
    model: str
    profile: str
    skip_git_repo_check: bool
    yolo: bool
    images: tuple[Path, ...] = ()


class Backend(Protocol):
    name: str
    command: str
    spec: EventSpec

    def build_args(self, cfg: SpawnConfig) -> list[str]: ...
