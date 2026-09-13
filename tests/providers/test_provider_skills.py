from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.skills import (
    ANTIGRAVITY_SKILL_LAYOUT,
    CLAUDE_SKILL_LAYOUT,
    CODEX_SKILL_LAYOUT,
    SkillStagingError,
    SkillValidationError,
    UnknownSkillLayoutError,
    load_skill,
    materialize_skill,
    stage_skill,
)


def create_skill(root: Path) -> Path:
    skill_dir = root / "demo-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Provide read-only demo guidance.\n---\n\n# Demo\n",
        encoding="utf-8",
    )
    references = skill_dir / "references"
    references.mkdir()
    (references / "policy.md").write_text("Use supported versions.\n", encoding="utf-8")
    return skill_dir


def test_load_and_stage_provider_specific_layouts(tmp_path: Path) -> None:
    spec = load_skill(create_skill(tmp_path))

    codex_path = stage_skill(spec, tmp_path / "codex", CODEX_SKILL_LAYOUT)
    claude_path = stage_skill(spec, tmp_path / "claude", CLAUDE_SKILL_LAYOUT)
    antigravity_path = stage_skill(
        spec,
        tmp_path / "antigravity",
        ANTIGRAVITY_SKILL_LAYOUT,
    )

    assert spec.name == "demo-skill"
    assert spec.description == "Provide read-only demo guidance."
    assert codex_path.relative_to(tmp_path / "codex").as_posix() == (
        ".agents/skills/demo-skill/SKILL.md"
    )
    assert claude_path.relative_to(tmp_path / "claude").as_posix() == (
        ".claude/skills/demo-skill/SKILL.md"
    )
    assert antigravity_path.relative_to(tmp_path / "antigravity").as_posix() == (
        ".agents/skills/demo-skill/SKILL.md"
    )
    assert (codex_path.parent / "references/policy.md").read_text(encoding="utf-8") == (
        "Use supported versions.\n"
    )


def test_load_rejects_symlink_in_skill_tree(tmp_path: Path) -> None:
    skill_dir = create_skill(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (skill_dir / "escape").symlink_to(outside)

    with pytest.raises(SkillValidationError, match="symlink"):
        load_skill(skill_dir)


def test_staging_revalidates_metadata(tmp_path: Path) -> None:
    skill_dir = create_skill(tmp_path)
    spec = load_skill(skill_dir)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: renamed-skill\ndescription: changed\n---\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillStagingError, match="changed"):
        stage_skill(spec, tmp_path / "workspace", CODEX_SKILL_LAYOUT)


def test_unknown_provider_layout_is_explicit(tmp_path: Path) -> None:
    spec = load_skill(create_skill(tmp_path))

    with pytest.raises(UnknownSkillLayoutError, match="gemini"):
        materialize_skill(spec, tmp_path / "workspace", "gemini")
