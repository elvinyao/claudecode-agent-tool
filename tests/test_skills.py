from __future__ import annotations

from pathlib import Path

import pytest

from trivy_ai_report.skills import SkillError, SkillSpec, load_skill, materialize_skill


def _write_skill(
    root: Path,
    *,
    name: str = "trivy-remediation",
    frontmatter: str | None = None,
) -> Path:
    root.mkdir(parents=True)
    skill_file = root / "SKILL.md"
    skill_file.write_text(
        frontmatter
        if frontmatter is not None
        else (
            "---\n"
            f"name: {name}\n"
            "description: Generate evidence-based Trivy remediation advice.\n"
            "---\n"
            "\n"
            "# Trivy remediation\n"
        ),
        encoding="utf-8",
    )
    return skill_file


@pytest.mark.parametrize("select_file", [False, True], ids=["directory", "skill-md"])
def test_load_skill_accepts_directory_and_direct_skill_file(
    tmp_path: Path,
    select_file: bool,
) -> None:
    source = tmp_path / "skill"
    skill_file = _write_skill(source)

    spec = load_skill(skill_file if select_file else source)

    assert spec.name == "trivy-remediation"
    assert spec.source_dir == source.resolve()
    assert spec.skill_file == skill_file.resolve()


@pytest.mark.parametrize(
    ("frontmatter", "message"),
    [
        ("# no frontmatter\n", "frontmatter"),
        (
            "---\nname: trivy-remediation\n"
            "description: Missing closing marker.\n",
            "结束标记",
        ),
        (
            "---\nname: first\nname: second\n"
            "description: Duplicate name.\n---\n",
            "一个 name",
        ),
        (
            "---\nname: trivy-remediation\n"
            "description: First.\ndescription: Second.\n---\n",
            "一个 description",
        ),
    ],
    ids=["missing", "unclosed", "duplicate-name", "duplicate-description"],
)
def test_load_skill_rejects_invalid_or_duplicate_frontmatter(
    tmp_path: Path,
    frontmatter: str,
    message: str,
) -> None:
    skill_file = _write_skill(tmp_path / "skill", frontmatter=frontmatter)

    with pytest.raises(SkillError, match=message):
        load_skill(skill_file)


@pytest.mark.parametrize(
    "name",
    [
        "Uppercase",
        "contains spaces",
        "../escape",
        "leading-",
        "-trailing",
        "double--hyphen",
        "a" * 65,
    ],
)
def test_load_skill_rejects_illegal_name(tmp_path: Path, name: str) -> None:
    skill_file = _write_skill(tmp_path / "skill", name=name)

    with pytest.raises(SkillError, match="Skill name"):
        load_skill(skill_file)


def test_load_skill_rejects_symlink_as_selected_path(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_skill(source)
    selected = tmp_path / "selected-skill"
    try:
        selected.symlink_to(source, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - platforms without symlink privileges
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(SkillError, match="符号链接"):
        load_skill(selected)


def test_load_skill_rejects_symlink_inside_resource_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_skill(source)
    external = tmp_path / "outside.txt"
    external.write_text("must not be copied", encoding="utf-8")
    resources = source / "references"
    resources.mkdir()
    linked_resource = resources / "outside.txt"
    try:
        linked_resource.symlink_to(external)
    except OSError as exc:  # pragma: no cover - platforms without symlink privileges
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(SkillError, match="符号链接"):
        load_skill(source)


@pytest.mark.parametrize(
    ("provider", "layout"),
    [
        ("codex", Path(".agents") / "skills"),
        ("claude", Path(".claude") / "skills"),
        ("gemini", Path("skills")),
    ],
)
def test_materialize_skill_copies_complete_resource_tree(
    tmp_path: Path,
    provider: str,
    layout: Path,
) -> None:
    source = tmp_path / "source"
    skill_file = _write_skill(source)
    references = source / "references"
    scripts = source / "scripts"
    references.mkdir()
    scripts.mkdir()
    (source / "empty-assets").mkdir()
    (references / "policy.md").write_text("# Corporate policy\n", encoding="utf-8")
    (scripts / "verify.py").write_text("print('verified')\n", encoding="utf-8")
    (source / "fixture.json").write_bytes(b'{"safe":true}\n')
    spec = load_skill(source)
    workspace = tmp_path / f"{provider}-workspace"

    staged_skill = materialize_skill(spec, workspace, provider)  # type: ignore[arg-type]

    destination = workspace / layout / spec.name
    assert staged_skill == destination / "SKILL.md"
    expected_files = {
        Path("SKILL.md"),
        Path("references/policy.md"),
        Path("scripts/verify.py"),
        Path("fixture.json"),
    }
    actual_files = {
        path.relative_to(destination) for path in destination.rglob("*") if path.is_file()
    }
    assert actual_files == expected_files
    assert (destination / "empty-assets").is_dir()
    for relative in expected_files:
        assert (destination / relative).read_bytes() == (source / relative).read_bytes()
    assert staged_skill.read_bytes() == skill_file.read_bytes()


def test_materialize_skill_rejects_unknown_provider(tmp_path: Path) -> None:
    spec = load_skill(_write_skill(tmp_path / "source"))

    with pytest.raises(SkillError, match="未知 Skill provider"):
        materialize_skill(spec, tmp_path / "workspace", "unknown")  # type: ignore[arg-type]


def test_materialize_skill_rejects_forged_skill_spec(tmp_path: Path) -> None:
    source = tmp_path / "source"
    skill_file = _write_skill(source)
    nested = source / "nested"
    nested.mkdir()
    # Pointing the otherwise well-shaped object at a non-root SKILL.md is invalid.
    nested_skill = nested / "SKILL.md"
    nested_skill.write_bytes(skill_file.read_bytes())
    forged = SkillSpec(
        name="trivy-remediation",
        source_dir=source,
        skill_file=nested_skill,
    )

    with pytest.raises(SkillError, match="根目录中的 SKILL.md"):
        materialize_skill(forged, tmp_path / "workspace", "codex")
