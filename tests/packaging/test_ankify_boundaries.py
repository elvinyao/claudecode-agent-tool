from __future__ import annotations

import ast
from pathlib import Path

from ankify.eval.fixture_loader import load_eval_fixtures
from ankify.plugin import bundled_skill_path

ROOT = Path(__file__).resolve().parents[2]
CORE_SOURCE = ROOT / "packages" / "agent-core" / "src" / "agent_core"
ANKIFY_PROJECT = ROOT / "plugins" / "ankify" / "pyproject.toml"


def test_core_never_imports_ankify_plugin() -> None:
    violations: list[str] = []
    for source in CORE_SOURCE.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "ankify" or name.startswith("ankify.") for name in names):
                violations.append(str(source.relative_to(ROOT)))
    assert violations == []


def test_ankify_declares_domain_entry_point() -> None:
    manifest = ANKIFY_PROJECT.read_text(encoding="utf-8")

    assert '[project.entry-points."agent_core.domain_plugins"]' in manifest
    assert 'ankify = "ankify.plugin:create_plugin"' in manifest
    assert '[project.scripts]' in manifest
    assert 'ankify-eval = "ankify.eval.cli:main"' in manifest


def test_ankify_bundled_resources_are_available_through_package_paths() -> None:
    assert bundled_skill_path().is_file()
    assert len(load_eval_fixtures()) == 7
