"""Runtime configuration, kept separate from behaviour so tests can shrink limits."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Limits:
    """Hard stops for one workflow run."""

    max_turns: int = 12
    max_tool_calls: int = 16
    max_seconds: float = 120.0


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    limits: Limits = field(default_factory=Limits)
    model_name: str = "scripted"
