"""Typed contracts for validated, local Agent Skills."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agent_core.contracts import AgentCoreError


class SkillError(AgentCoreError, ValueError):
    """Base class for invalid or unsafe skill inputs."""


class SkillValidationError(SkillError):
    """A source skill failed metadata or filesystem validation."""


class SkillStagingError(SkillError):
    """A validated skill could not be staged safely."""


class UnknownSkillLayoutError(SkillStagingError):
    """No staging layout is registered for the requested provider."""


@dataclass(frozen=True, slots=True)
class SkillSpec:
    """A validated local Agent Skill and its immutable source identity."""

    name: str
    source_dir: Path
    skill_file: Path
    # Kept last with a default so callers using the original three-field
    # SkillSpec remain source-compatible; staging still rejects an unverified value.
    description: str = ""


@dataclass(frozen=True, slots=True)
class SkillLayout:
    """Provider-specific discovery path relative to an isolated workspace."""

    provider: str
    relative_root: PurePosixPath

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("provider must not be empty")
        if self.relative_root.is_absolute() or ".." in self.relative_root.parts:
            raise ValueError("relative_root must remain inside the workspace")


CODEX_SKILL_LAYOUT = SkillLayout(
    provider="codex",
    relative_root=PurePosixPath(".agents/skills"),
)
CLAUDE_SKILL_LAYOUT = SkillLayout(
    provider="claude",
    relative_root=PurePosixPath(".claude/skills"),
)


__all__ = [
    "CLAUDE_SKILL_LAYOUT",
    "CODEX_SKILL_LAYOUT",
    "SkillError",
    "SkillLayout",
    "SkillSpec",
    "SkillStagingError",
    "SkillValidationError",
    "UnknownSkillLayoutError",
]
