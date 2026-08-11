"""Validate local Agent Skills without following filesystem links."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from agent_core.skills.models import SkillSpec, SkillValidationError

MAX_SKILL_FILES = 128
MAX_SKILL_FILE_BYTES = 512 * 1024
MAX_SKILL_TOTAL_BYTES = 2 * 1024 * 1024

_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class ValidatedSkillFile:
    """One regular file proven to be inside a validated skill directory."""

    source: Path
    relative_path: Path
    size: int


def _unquote_yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1].strip()
    return value


def _metadata_value(lines: list[str], key: str) -> str:
    matches: list[tuple[int, str]] = []
    prefix = f"{key}:"
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            matches.append((index, line[len(prefix) :].strip()))
    if len(matches) != 1:
        raise SkillValidationError(
            f"SKILL.md frontmatter must contain exactly one {key!r} field"
        )

    index, value = matches[0]
    if value in {"|", ">", "|-", ">-", "|+", ">+"}:
        continuation: list[str] = []
        for line in lines[index + 1 :]:
            if line and not line[0].isspace():
                break
            if line.strip():
                continuation.append(line.strip())
        value = " ".join(continuation)
    return _unquote_yaml_scalar(value)


def read_skill_metadata(skill_file: Path) -> tuple[str, str]:
    """Read the bounded YAML frontmatter fields supported by the framework."""

    try:
        metadata = skill_file.stat(follow_symlinks=False)
    except OSError as exc:
        raise SkillValidationError(f"cannot inspect skill file: {skill_file}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise SkillValidationError(f"SKILL.md must be a regular file: {skill_file}")
    if metadata.st_size > MAX_SKILL_FILE_BYTES:
        raise SkillValidationError(
            f"SKILL.md exceeds the {MAX_SKILL_FILE_BYTES}-byte limit"
        )

    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SkillValidationError("SKILL.md must be readable UTF-8 text") from exc

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillValidationError("SKILL.md must start with YAML frontmatter")
    try:
        closing = next(
            index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"
        )
    except StopIteration as exc:
        raise SkillValidationError("SKILL.md frontmatter is not closed") from exc

    frontmatter = lines[1:closing]
    name = _metadata_value(frontmatter, "name")
    description = _metadata_value(frontmatter, "description")
    if not name or len(name) > 64 or _SKILL_NAME.fullmatch(name) is None:
        raise SkillValidationError(
            "skill name must contain at most 64 lowercase letters, digits, or single hyphens"
        )
    if not description:
        raise SkillValidationError("skill description must not be empty")
    if len(description) > 1_024:
        raise SkillValidationError("skill description exceeds 1024 characters")
    return name, description


def validate_skill_tree(source_dir: Path) -> tuple[ValidatedSkillFile, ...]:
    """Validate a skill tree and return regular files without following links."""

    files: list[ValidatedSkillFile] = []
    total_bytes = 0
    for root, directories, filenames in os.walk(source_dir, followlinks=False):
        root_path = Path(root)
        for directory in directories:
            path = root_path / directory
            if path.is_symlink():
                raise SkillValidationError(f"skill directories must not be symlinks: {path}")
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise SkillValidationError(f"cannot inspect skill directory: {path}") from exc
            if not stat.S_ISDIR(metadata.st_mode):
                raise SkillValidationError(
                    f"skills may contain only regular directories and files: {path}"
                )

        for filename in filenames:
            path = root_path / filename
            if path.is_symlink():
                raise SkillValidationError(f"skill files must not be symlinks: {path}")
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise SkillValidationError(f"cannot inspect skill file: {path}") from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise SkillValidationError(
                    f"skills may contain only regular directories and files: {path}"
                )
            if metadata.st_size > MAX_SKILL_FILE_BYTES:
                raise SkillValidationError(
                    f"skill file exceeds the {MAX_SKILL_FILE_BYTES}-byte limit: {path}"
                )
            files.append(
                ValidatedSkillFile(
                    source=path,
                    relative_path=path.relative_to(source_dir),
                    size=metadata.st_size,
                )
            )
            total_bytes += metadata.st_size
            if len(files) > MAX_SKILL_FILES:
                raise SkillValidationError(
                    f"skill contains more than {MAX_SKILL_FILES} files"
                )
            if total_bytes > MAX_SKILL_TOTAL_BYTES:
                raise SkillValidationError(
                    f"skill exceeds the {MAX_SKILL_TOTAL_BYTES}-byte total limit"
                )
    return tuple(files)


def load_skill(path: str | Path) -> SkillSpec:
    """Load a skill directory or SKILL.md after strict filesystem validation."""

    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise SkillValidationError(f"selected skill path must not be a symlink: {requested}")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise SkillValidationError(
            f"skill path does not exist or is inaccessible: {requested}"
        ) from exc

    if resolved.is_dir():
        source_dir = resolved
        skill_file = source_dir / "SKILL.md"
    elif resolved.is_file() and resolved.name == "SKILL.md":
        source_dir = resolved.parent
        skill_file = resolved
    else:
        raise SkillValidationError("skill path must be a directory or a file named SKILL.md")

    if skill_file.is_symlink() or not skill_file.is_file():
        raise SkillValidationError(f"skill directory has no regular SKILL.md: {source_dir}")
    name, description = read_skill_metadata(skill_file)
    validate_skill_tree(source_dir)
    return SkillSpec(
        name=name,
        description=description,
        source_dir=source_dir,
        skill_file=skill_file,
    )


__all__ = [
    "MAX_SKILL_FILES",
    "MAX_SKILL_FILE_BYTES",
    "MAX_SKILL_TOTAL_BYTES",
    "ValidatedSkillFile",
    "load_skill",
    "read_skill_metadata",
    "validate_skill_tree",
]
