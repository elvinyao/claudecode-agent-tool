from __future__ import annotations

import ast
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
CORE_SOURCE = ROOT / "packages" / "agent-core" / "src" / "agent_core"


def test_core_never_imports_trivy_plugin() -> None:
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
            if any(
                name == "trivy_ai_report" or name.startswith("trivy_ai_report.")
                for name in names
            ):
                violations.append(str(source.relative_to(ROOT)))
    assert violations == []


def test_trivy_is_declared_as_domain_entry_point() -> None:
    manifest = (ROOT / "plugins" / "trivy-ai-report" / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert '[project.entry-points."agent_core.domain_plugins"]' in manifest
    assert 'trivy = "trivy_ai_report.plugin:create_plugin"' in manifest


def test_base_core_metadata_keeps_optional_transports_and_sdks_out() -> None:
    with (ROOT / "packages" / "agent-core" / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]

    assert project["dependencies"] == ["pydantic>=2.10,<3"]
    assert set(project["optional-dependencies"]) == {
        "antigravity",
        "codex",
        "claude",
        "web",
        "all",
    }
    assert project["optional-dependencies"]["antigravity"] == [
        "google-antigravity>=0.1.13,<0.2"
    ]
    assert project["optional-dependencies"]["codex"] == [
        "openai-codex>=0.144.4,<1"
    ]
    assert project["scripts"] == {"agent-core": "agent_core.cli:main"}

    cli_tree = ast.parse(
        (CORE_SOURCE / "cli.py").read_text(encoding="utf-8"),
        filename="agent_core/cli.py",
    )
    top_level_imports = {
        node.module or ""
        for node in cli_tree.body
        if isinstance(node, ast.ImportFrom)
    }
    assert "agent_core.web" not in top_level_imports
    assert "agent_core.jobs" not in top_level_imports
