from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from agent_core.contracts import ArtifactInput, ArtifactOutput
from agent_core.registry import (
    CORE_API_VERSION,
    DOMAIN_PLUGIN_ENTRYPOINT_GROUP,
    DuplicatePluginError,
    PluginApiVersionError,
    PluginContractError,
    PluginManifest,
    PluginRegistry,
    UnknownPluginError,
)


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    enabled: bool = True


class Input(ArtifactInput[Options]):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Output(ArtifactOutput):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    result: str = "ok"


def manifest(*, plugin_id: str = "demo", api_version: str = CORE_API_VERSION) -> PluginManifest:
    return PluginManifest(
        plugin_id=plugin_id,
        api_version=api_version,
        version="1.2.3",
        display_name="Demo plugin",
        input_model=Input,
        options_model=Options,
        output_model=Output,
        required_capabilities=("skills",),
    )


class DemoPlugin:
    def __init__(self, plugin_manifest: PluginManifest) -> None:
        self.manifest = plugin_manifest
        self.plugin_id = plugin_manifest.plugin_id
        self.api_version = plugin_manifest.api_version

    def create_workflow(self, runtime: Any) -> tuple[str, Any]:
        return self.plugin_id, runtime


def test_registry_exposes_schemas_and_constructs_a_fresh_plugin_per_run() -> None:
    calls = 0
    plugin_manifest = manifest()

    def factory() -> DemoPlugin:
        nonlocal calls
        calls += 1
        return DemoPlugin(plugin_manifest)

    registry = PluginRegistry()
    descriptor = registry.register("demo", factory)
    first = registry.create_for_run("demo")
    second = registry.create_for_run("demo")

    assert calls == 3  # one startup probe plus one factory call per run
    assert first is not second
    assert descriptor.display_name == "Demo plugin"
    assert descriptor.version == "1.2.3"
    assert descriptor.options_schema["properties"]["enabled"]["type"] == "boolean"
    assert registry.list() == (descriptor,)
    assert registry.get("demo") is descriptor


def test_registry_fails_closed_for_duplicate_api_and_singleton_factories() -> None:
    registry = PluginRegistry()
    plugin_manifest = manifest()
    registry.register("demo", lambda: DemoPlugin(plugin_manifest))
    with pytest.raises(DuplicatePluginError):
        registry.register("demo", lambda: DemoPlugin(plugin_manifest))

    with pytest.raises(PluginApiVersionError):
        PluginRegistry().register(
            "demo",
            lambda: DemoPlugin(manifest(api_version="2.0")),
        )

    singleton = DemoPlugin(plugin_manifest)
    singleton_registry = PluginRegistry()
    singleton_registry.register("demo", lambda: singleton)
    with pytest.raises(PluginContractError, match="reused an instance"):
        singleton_registry.create("demo")

    with pytest.raises(UnknownPluginError):
        registry.create("missing")


@dataclass
class FakeEntryPoint:
    name: str
    value: str
    factory: Any
    group: str = DOMAIN_PLUGIN_ENTRYPOINT_GROUP

    def load(self) -> Any:
        return self.factory


def test_standard_entry_point_discovery_validates_manifest_at_startup() -> None:
    plugin_manifest = manifest(plugin_id="zeta")
    entry_point = FakeEntryPoint(
        name="zeta",
        value="example:create_plugin",
        factory=lambda: DemoPlugin(plugin_manifest),
    )

    registry = PluginRegistry.from_entry_points(entry_points=(entry_point,))

    assert registry.list()[0].plugin_id == "zeta"
    assert registry.list()[0].source == "entrypoint:example:create_plugin"


def test_invalid_entry_point_name_is_rejected_before_loading() -> None:
    loaded = False

    class InvalidEntryPoint:
        name = "../invalid"
        value = "malicious:factory"
        group = DOMAIN_PLUGIN_ENTRYPOINT_GROUP

        def load(self):
            nonlocal loaded
            loaded = True
            return object()

    with pytest.raises(PluginContractError, match="entry-point name"):
        PluginRegistry.from_entry_points(entry_points=(InvalidEntryPoint(),))
    assert loaded is False


def test_plugin_contract_requires_manifest_identity_and_workflow_factory() -> None:
    class MissingWorkflow:
        manifest = manifest()
        plugin_id = "demo"
        api_version = CORE_API_VERSION

    with pytest.raises(PluginContractError, match="create_workflow"):
        PluginRegistry().register("demo", MissingWorkflow)  # type: ignore[arg-type]

    wrong_manifest = manifest(plugin_id="other")
    with pytest.raises(PluginContractError, match="manifest id"):
        PluginRegistry().register("demo", lambda: DemoPlugin(wrong_manifest))


def test_manifest_rejects_non_artifact_input_and_output_models() -> None:
    class PlainInput(BaseModel):
        value: str

    class PlainOutput(BaseModel):
        value: str

    values = {
        "plugin_id": "demo",
        "api_version": CORE_API_VERSION,
        "version": "1.0.0",
        "display_name": "Demo",
        "options_model": Options,
        "required_capabilities": (),
    }
    with pytest.raises(ValueError, match="inherit ArtifactInput"):
        PluginManifest(
            **values,
            input_model=PlainInput,
            output_model=Output,
        )
    with pytest.raises(ValueError, match="inherit ArtifactOutput"):
        PluginManifest(
            **values,
            input_model=Input,
            output_model=PlainOutput,
        )
