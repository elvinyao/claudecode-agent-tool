from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility
    import tomli as tomllib

from agent_core.contracts import AgentRequest, ProviderResult
from agent_core.providers import ProviderCapabilities
from agent_core.registry import DOMAIN_PLUGIN_ENTRYPOINT_GROUP, PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
TOY_PACKAGE = ROOT / "tests" / "fixtures" / "toy-plugin"
TOY_SOURCE = TOY_PACKAGE / "src"
CORE_SOURCE = ROOT / "packages" / "agent-core" / "src"


def _toy_project() -> dict[str, Any]:
    with (TOY_PACKAGE / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)


def _load_toy_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(TOY_SOURCE))
    return importlib.import_module("toy_agent_plugin.plugin")


@dataclass(slots=True)
class FakeProvider:
    """Provider double that validates and materializes the requested response model."""

    name: str = "fake"
    model: str = "fake-toy-model"
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    requests: list[AgentRequest[Any]] = field(default_factory=list)
    timeouts: list[float | None] = field(default_factory=list)

    async def execute(
        self,
        request: AgentRequest[Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ProviderResult[Any]:
        self.requests.append(request)
        self.timeouts.append(timeout_seconds)
        text = request.metadata["toy_text"]
        if not isinstance(text, str):
            raise TypeError("toy_text metadata must be a string")
        output = request.response_model(
            original_text=text,
            reversed_text=text[::-1],
            character_count=len(text),
            schema_sentinel="TOY_RESPONSE_SCHEMA_V1",
        )
        return ProviderResult(
            request_id=request.request_id,
            provider=self.name,
            model=self.model,
            output=output,
        )


@dataclass(frozen=True, slots=True)
class FakeRuntime:
    provider: FakeProvider


def test_standalone_package_declares_a_loadable_domain_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _toy_project()
    entry_points = project["project"]["entry-points"]
    target = entry_points[DOMAIN_PLUGIN_ENTRYPOINT_GROUP]["toy"]

    assert target == "toy_agent_plugin.plugin:create_plugin"
    assert "agent-core>=0.1,<0.2" in project["project"]["dependencies"]
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/toy_agent_plugin"
    ]

    _load_toy_module(monkeypatch)
    entry_point = EntryPoint(
        name="toy",
        value=target,
        group=DOMAIN_PLUGIN_ENTRYPOINT_GROUP,
    )
    registry = PluginRegistry.from_entry_points(entry_points=(entry_point,))

    descriptor = registry.get("toy")
    assert descriptor.source == f"entrypoint:{target}"
    assert descriptor.display_name == "Toy Text Reverser"
    assert {"content", "filename", "options"} <= set(
        descriptor.input_schema["properties"]
    )
    assert "preserve_case" in descriptor.options_schema["properties"]
    assert "format_sentinel" in descriptor.output_schema["properties"]


@pytest.mark.asyncio
async def test_toy_workflow_passes_prompt_and_schema_and_produces_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toy = _load_toy_module(monkeypatch)
    target = _toy_project()["project"]["entry-points"][
        DOMAIN_PLUGIN_ENTRYPOINT_GROUP
    ]["toy"]
    registry = PluginRegistry.from_entry_points(
        entry_points=(
            EntryPoint(
                name="toy",
                value=target,
                group=DOMAIN_PLUGIN_ENTRYPOINT_GROUP,
            ),
        )
    )

    first_plugin = registry.create_for_run("toy")
    second_plugin = registry.create_for_run("toy")
    assert first_plugin is not second_plugin

    provider = FakeProvider()
    workflow = first_plugin.create_workflow(FakeRuntime(provider=provider))
    assert workflow.ordered_node_ids == (
        "toy_parse",
        "toy_plan",
        "toy_agent",
        "toy_render",
    )

    phrases = ("Codex", "本地 AI")
    result = await workflow.execute(
        toy.ToyInput(
            content=json.dumps({"phrases": phrases}).encode("utf-8"),
            options=toy.ToyOptions(),
            filename="phrases.json",
        ),
        attempt_timeout_seconds=1.5,
    )

    assert [request.metadata["toy_text"] for request in provider.requests] == list(phrases)
    assert provider.timeouts == [1.5, 1.5]
    for request in provider.requests:
        assert request.system_prompt.startswith("TOY_SENTINEL_SYSTEM_V1")
        assert request.prompt.startswith("TOY_SENTINEL_PROMPT_V1")
        assert request.response_model is toy.ToyAgentAnswer
        response_schema = request.response_model.model_json_schema()
        assert "schema_sentinel" in response_schema["properties"]
        assert request.tool_policy.web_access is False

    artifact = result.final_outputs[0]
    assert isinstance(artifact, toy.ToyArtifact)
    assert artifact.media_type == "application/vnd.agent-core.toy+json"
    assert artifact.format_sentinel == "TOY_ARTIFACT_V1"
    records = json.loads(artifact.content)
    assert [record["original_text"] for record in records] == list(phrases)
    assert [record["reversed_text"] for record in records] == [
        phrase[::-1] for phrase in phrases
    ]


def test_core_and_toy_run_when_trivy_imports_are_blocked() -> None:
    for source in TOY_SOURCE.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        imported_modules = [
            imported.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for imported in node.names
        ] + [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        assert not any(
            name == "trivy_ai_report" or name.startswith("trivy_ai_report.")
            for name in imported_modules
        )

    script = textwrap.dedent(
        """
        import asyncio
        import importlib.abc
        import sys
        from importlib.metadata import EntryPoint

        class BlockTrivy(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "trivy_ai_report" or fullname.startswith("trivy_ai_report."):
                    raise ImportError(f"blocked unexpected import: {fullname}")
                return None

        sys.meta_path.insert(0, BlockTrivy())

        from agent_core.contracts import ProviderResult
        from agent_core.registry import PluginRegistry

        class Provider:
            name = "isolated-fake"
            model = "isolated-model"

            async def execute(self, request, *, timeout_seconds=None):
                text = request.metadata["toy_text"]
                output = request.response_model(
                    original_text=text,
                    reversed_text=text[::-1],
                    character_count=len(text),
                    schema_sentinel="TOY_RESPONSE_SCHEMA_V1",
                )
                return ProviderResult(
                    request_id=request.request_id,
                    provider=self.name,
                    model=self.model,
                    output=output,
                )

        class Runtime:
            provider = Provider()

        async def main():
            entry_point = EntryPoint(
                name="toy",
                value="toy_agent_plugin.plugin:create_plugin",
                group="agent_core.domain_plugins",
            )
            registry = PluginRegistry.from_entry_points(entry_points=(entry_point,))
            plugin = registry.create_for_run("toy")
            toy_module = __import__("toy_agent_plugin", fromlist=["ToyInput"])
            result = await plugin.create_workflow(Runtime()).execute(
                toy_module.ToyInput(
                    content=b'{"phrases":["generic"]}',
                    options=toy_module.ToyOptions(),
                    filename="input.json",
                )
            )
            artifact = result.final_outputs[0]
            assert artifact.format_sentinel == "TOY_ARTIFACT_V1"
            assert not any(name.startswith("trivy_ai_report") for name in sys.modules)
            print("toy-ok")

        asyncio.run(main())
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((str(CORE_SOURCE), str(TOY_SOURCE)))
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "toy-ok"
