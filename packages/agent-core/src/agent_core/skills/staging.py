"""Stage validated skills into isolated provider discovery layouts."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from agent_core.skills.loader import (
    ValidatedSkillFile,
    read_skill_metadata,
    validate_skill_tree,
)
from agent_core.skills.models import (
    ANTIGRAVITY_SKILL_LAYOUT,
    CLAUDE_SKILL_LAYOUT,
    CODEX_SKILL_LAYOUT,
    SkillLayout,
    SkillSpec,
    SkillStagingError,
    UnknownSkillLayoutError,
)

DEFAULT_SKILL_LAYOUTS: dict[str, SkillLayout] = {
    ANTIGRAVITY_SKILL_LAYOUT.provider: ANTIGRAVITY_SKILL_LAYOUT,
    CODEX_SKILL_LAYOUT.provider: CODEX_SKILL_LAYOUT,
    CLAUDE_SKILL_LAYOUT.provider: CLAUDE_SKILL_LAYOUT,
}


def _validated_source(skill: SkillSpec) -> tuple[Path, tuple[ValidatedSkillFile, ...]]:
    if skill.source_dir.is_symlink() or skill.skill_file.is_symlink():
        raise SkillStagingError("SkillSpec must not reference symlinks")
    try:
        source_dir = skill.source_dir.resolve(strict=True)
        skill_file = skill.skill_file.resolve(strict=True)
    except OSError as exc:
        raise SkillStagingError("SkillSpec source no longer exists") from exc
    if not source_dir.is_dir() or skill_file != source_dir / "SKILL.md":
        raise SkillStagingError("SkillSpec must reference SKILL.md at the source root")

    try:
        files = validate_skill_tree(source_dir)
        name, description = read_skill_metadata(skill_file)
    except Exception as exc:
        if isinstance(exc, SkillStagingError):
            raise
        raise SkillStagingError(f"skill changed before staging: {exc}") from exc
    if name != skill.name or description != skill.description:
        raise SkillStagingError("skill metadata changed after validation")
    return source_dir, files


def stage_skill(skill: SkillSpec, workspace: str | Path, layout: SkillLayout) -> Path:
    """Copy one validated skill and return its staged SKILL.md path."""

    source_dir, files = _validated_source(skill)
    workspace_path = Path(workspace)
    destination = workspace_path.joinpath(*layout.relative_root.parts, skill.name)
    if destination.exists():
        raise SkillStagingError(f"workspace already contains skill {skill.name!r}")
    try:
        destination.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise SkillStagingError(f"cannot create staged skill directory: {destination}") from exc

    # Preserve empty resource directories. Recheck every entry immediately before use
    # to narrow the race between validation and copying.
    for root, directories, _filenames in os.walk(source_dir, followlinks=False):
        root_path = Path(root)
        for directory in directories:
            source_directory = root_path / directory
            if source_directory.is_symlink():
                raise SkillStagingError(
                    f"skill directory became a symlink before staging: {source_directory}"
                )
            try:
                resolved_directory = source_directory.resolve(strict=True)
            except OSError as exc:
                raise SkillStagingError(
                    f"cannot resolve skill directory: {source_directory}"
                ) from exc
            if not resolved_directory.is_relative_to(source_dir):
                raise SkillStagingError(
                    f"skill directory resolves outside its source: {source_directory}"
                )
            relative_directory = source_directory.relative_to(source_dir)
            (destination / relative_directory).mkdir(parents=True, exist_ok=True)

    for entry in files:
        source = entry.source
        target = destination / entry.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            raise SkillStagingError(f"skill file became a symlink before staging: {source}")
        try:
            resolved_source = source.resolve(strict=True)
            metadata = source.stat(follow_symlinks=False)
        except OSError as exc:
            raise SkillStagingError(f"cannot inspect skill file while staging: {source}") from exc
        if not resolved_source.is_relative_to(source_dir):
            raise SkillStagingError(f"skill file resolves outside its source: {source}")
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != entry.size:
            raise SkillStagingError(f"skill file changed while staging: {source}")
        try:
            shutil.copy2(source, target, follow_symlinks=False)
        except OSError as exc:
            raise SkillStagingError(f"cannot stage skill file: {source}") from exc

    staged_skill = destination / "SKILL.md"
    if not staged_skill.is_file():
        raise SkillStagingError("staged skill has no SKILL.md")
    return staged_skill


def materialize_skill(skill: SkillSpec, workspace: str | Path, provider: str) -> Path:
    """Compatibility helper that selects a registered provider layout by name."""

    try:
        layout = DEFAULT_SKILL_LAYOUTS[provider.strip().lower()]
    except KeyError as exc:
        raise UnknownSkillLayoutError(
            f"no skill layout registered for provider {provider!r}"
        ) from exc
    return stage_skill(skill, workspace, layout)


__all__ = ["DEFAULT_SKILL_LAYOUTS", "materialize_skill", "stage_skill"]
