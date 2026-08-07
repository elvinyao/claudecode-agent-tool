"""Validate and stage one explicitly selected Agent Skill.

The caller chooses a local ``SKILL.md``.  We validate its small metadata
contract and copy the complete skill directory into a fresh, provider-specific
workspace.  Symlinks and special files are rejected so a skill cannot escape
that workspace through copied resources.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MAX_SKILL_FILES = 128
MAX_SKILL_FILE_BYTES = 512 * 1024
MAX_SKILL_TOTAL_BYTES = 2 * 1024 * 1024
_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillError(ValueError):
    """The selected skill is missing, malformed, or unsafe to stage."""


@dataclass(frozen=True, slots=True)
class SkillSpec:
    """A validated local Agent Skill."""

    name: str
    source_dir: Path
    skill_file: Path


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
        raise SkillError(f"SKILL.md frontmatter 必须且只能包含一个 {key}")

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


def _read_frontmatter(skill_file: Path) -> tuple[str, str]:
    try:
        size = skill_file.stat().st_size
    except OSError as exc:
        raise SkillError(f"无法读取 Skill 文件：{skill_file}") from exc
    if size > MAX_SKILL_FILE_BYTES:
        raise SkillError(f"SKILL.md 超过 {MAX_SKILL_FILE_BYTES} 字节限制")

    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SkillError(f"SKILL.md 必须是可读的 UTF-8 文本：{skill_file}") from exc

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError("SKILL.md 必须以 YAML frontmatter（---）开头")
    try:
        closing = next(
            index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"
        )
    except StopIteration as exc:
        raise SkillError("SKILL.md 缺少 frontmatter 结束标记（---）") from exc

    metadata = lines[1:closing]
    name = _metadata_value(metadata, "name")
    description = _metadata_value(metadata, "description")
    if not name or len(name) > 64 or _SKILL_NAME.fullmatch(name) is None:
        raise SkillError("Skill name 必须是最多 64 字符的小写字母、数字和单连字符组合")
    if not description:
        raise SkillError("Skill description 不能为空")
    if len(description) > 1_024:
        raise SkillError("Skill description 不能超过 1024 字符")
    return name, description


def _validated_files(source_dir: Path) -> list[tuple[Path, Path]]:
    """Return ``(source, relative)`` files after a non-following tree walk."""

    files: list[tuple[Path, Path]] = []
    total_bytes = 0
    for root, directories, filenames in os.walk(source_dir, followlinks=False):
        root_path = Path(root)
        for directory in directories:
            path = root_path / directory
            if path.is_symlink():
                raise SkillError(f"Skill 目录不能包含符号链接：{path}")
            try:
                mode = path.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise SkillError(f"无法检查 Skill 目录：{path}") from exc
            if not stat.S_ISDIR(mode):
                raise SkillError(f"Skill 只能包含普通目录和文件：{path}")

        for filename in filenames:
            path = root_path / filename
            if path.is_symlink():
                raise SkillError(f"Skill 目录不能包含符号链接：{path}")
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise SkillError(f"无法检查 Skill 文件：{path}") from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise SkillError(f"Skill 只能包含普通目录和文件：{path}")
            if metadata.st_size > MAX_SKILL_FILE_BYTES:
                raise SkillError(f"Skill 单个文件超过 {MAX_SKILL_FILE_BYTES} 字节：{path}")
            files.append((path, path.relative_to(source_dir)))
            total_bytes += metadata.st_size
            if len(files) > MAX_SKILL_FILES:
                raise SkillError(f"Skill 文件数超过 {MAX_SKILL_FILES} 个")
            if total_bytes > MAX_SKILL_TOTAL_BYTES:
                raise SkillError(f"Skill 总大小超过 {MAX_SKILL_TOTAL_BYTES} 字节")
    return files


def load_skill(path: Path | str) -> SkillSpec:
    """Load and validate a skill directory or its ``SKILL.md`` path."""

    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise SkillError(f"Skill 路径不能是符号链接：{requested}")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise SkillError(f"Skill 路径不存在或不可访问：{requested}") from exc

    if resolved.is_dir():
        source_dir = resolved
        skill_file = source_dir / "SKILL.md"
    elif resolved.is_file() and resolved.name == "SKILL.md":
        source_dir = resolved.parent
        skill_file = resolved
    else:
        raise SkillError("--skill 必须指向 Skill 目录或名为 SKILL.md 的文件")

    if not skill_file.is_file() or skill_file.is_symlink():
        raise SkillError(f"Skill 目录缺少普通文件 SKILL.md：{source_dir}")
    name, _description = _read_frontmatter(skill_file)
    _validated_files(source_dir)
    return SkillSpec(name=name, source_dir=source_dir, skill_file=skill_file)


def materialize_skill(
    skill: SkillSpec,
    root: Path | str,
    provider: Literal["codex", "claude", "gemini"],
) -> Path:
    """Copy a validated skill into the provider's project discovery path.

    Returns the staged ``SKILL.md`` path, which Codex can also receive as an
    explicit ``SkillInput`` reference.
    """

    layouts = {
        "codex": Path(".agents") / "skills",
        "claude": Path(".claude") / "skills",
        "gemini": Path("skills"),
    }
    try:
        layout = layouts[provider]
    except KeyError as exc:  # pragma: no cover - protected by provider types
        raise SkillError(f"未知 Skill provider：{provider}") from exc

    if skill.source_dir.is_symlink() or skill.skill_file.is_symlink():
        raise SkillError("SkillSpec 不能引用符号链接")
    try:
        source_dir = skill.source_dir.resolve(strict=True)
        skill_file = skill.skill_file.resolve(strict=True)
    except OSError as exc:
        raise SkillError("SkillSpec 引用的路径不存在或不可访问") from exc
    if not source_dir.is_dir() or skill_file != source_dir / "SKILL.md":
        raise SkillError("SkillSpec 必须引用 source_dir 根目录中的 SKILL.md")

    # Validate again immediately before copying to narrow the race between CLI
    # input validation and provider execution. This also protects callers that
    # construct SkillSpec directly instead of going through load_skill().
    files = _validated_files(source_dir)
    name, _description = _read_frontmatter(skill_file)
    if name != skill.name:
        raise SkillError("Skill name 在装载后发生变化，已拒绝调用")

    root_path = Path(root)
    destination = root_path / layout / skill.name
    if destination.exists():
        raise SkillError(f"临时工作区中已存在同名 Skill：{skill.name}")
    destination.mkdir(parents=True)

    # Preserve empty resource directories as well as files. Recheck each
    # directory because the source can change between CLI validation and use.
    for root, directories, _filenames in os.walk(source_dir, followlinks=False):
        root_path = Path(root)
        for directory in directories:
            source_directory = root_path / directory
            if source_directory.is_symlink():
                raise SkillError(f"Skill 目录在复制前变为符号链接：{source_directory}")
            try:
                resolved_directory = source_directory.resolve(strict=True)
            except OSError as exc:
                raise SkillError(f"无法复制 Skill 目录：{source_directory}") from exc
            if not resolved_directory.is_relative_to(source_dir):
                raise SkillError(f"Skill 目录解析到外部：{source_directory}")
            relative_directory = source_directory.relative_to(source_dir)
            (destination / relative_directory).mkdir(parents=True, exist_ok=True)

    for source, relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Recheck each leaf directly before copying. copy2 never follows a
        # symlink here because a changed leaf is rejected first.
        if source.is_symlink():
            raise SkillError(f"Skill 文件在复制前变为符号链接：{source}")
        try:
            resolved_source = source.resolve(strict=True)
            if not resolved_source.is_relative_to(source_dir):
                raise SkillError(f"Skill 文件解析到目录外部：{source}")
            metadata = source.stat(follow_symlinks=False)
        except OSError as exc:
            raise SkillError(f"无法复制 Skill 文件：{source}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise SkillError(f"Skill 文件在复制前不再是普通文件：{source}")
        try:
            shutil.copy2(source, target, follow_symlinks=False)
        except OSError as exc:
            raise SkillError(f"无法复制 Skill 文件：{source}") from exc

    staged_skill = destination / "SKILL.md"
    if not staged_skill.is_file():  # pragma: no cover - validated source invariant
        raise SkillError("复制后的 Skill 缺少 SKILL.md")
    return staged_skill


__all__ = ["SkillError", "SkillSpec", "load_skill", "materialize_skill"]
