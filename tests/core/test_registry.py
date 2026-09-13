from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from agent_core.contracts import ArtifactInput, ArtifactOutput
from agent_core.ownership import (
    FieldOwnership,
    OwnershipContract,
    OwnershipField,
    OwnershipSurface,
    validate_ownership_contract,
)
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


class ContentItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    candidate: str


class ContentDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    items: tuple[ContentItem, ...]
    policy: str


def manifest(
    *,
    plugin_id: str = "demo",
    api_version: str = CORE_API_VERSION,
    artifact_content_model: type[BaseModel] | None = None,
    ownership: OwnershipContract | None = None,
) -> PluginManifest:
    return PluginManifest(
        plugin_id=plugin_id,
        api_version=api_version,
        version="1.2.3",
        display_name="Demo plugin",
        input_model=Input,
        options_model=Options,
        output_model=Output,
        artifact_content_model=artifact_content_model,
        ownership=ownership,
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
    assert descriptor.artifact_content_schema is None
    assert descriptor.ownership is None
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
        PluginRegistry().register("demo", MissingWorkflow)  # ty: ignore[invalid-argument-type]  # Deliberately incomplete plugin.

    wrong_manifest = manifest(plugin_id="other")
    with pytest.raises(PluginContractError, match="manifest id"):
        PluginRegistry().register("demo", lambda: DemoPlugin(wrong_manifest))


def test_manifest_rejects_non_artifact_input_and_output_models() -> None:
    class PlainInput(BaseModel):
        value: str

    class PlainOutput(BaseModel):
        value: str

    values: dict[str, Any] = {
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


def test_descriptor_exposes_versioned_ownership_and_artifact_content_schema() -> None:
    ownership = OwnershipContract(
        fields=(
            OwnershipField(
                surface=OwnershipSurface.INPUT,
                path="/filename",
                ownership=FieldOwnership.PROGRAM_FACT,
                label="Filename",
                description="A deterministic display name.",
            ),
            OwnershipField(
                surface=OwnershipSurface.INPUT,
                path="/content",
                ownership=FieldOwnership.ACTION_INPUT,
                label="Payload",
                description="The payload approved for the action.",
            ),
            OwnershipField(
                surface=OwnershipSurface.OPTIONS,
                path="/enabled",
                ownership=FieldOwnership.USER_CHOICE,
                label="Enabled",
                description="A user-selected option.",
            ),
            OwnershipField(
                surface=OwnershipSurface.ARTIFACT_CONTENT,
                path="/items/-/candidate",
                ownership=FieldOwnership.AI_CANDIDATE,
                label="Candidate",
                description="An AI-authored candidate value.",
            ),
            OwnershipField(
                surface=OwnershipSurface.ARTIFACT_CONTENT,
                path="/policy",
                ownership=FieldOwnership.POLICY_LOCKED,
                label="Policy",
                description="A policy-controlled result field.",
            ),
        )
    )

    descriptor = PluginRegistry().register(
        "demo",
        lambda: DemoPlugin(
            manifest(
                artifact_content_model=ContentDocument,
                ownership=ownership,
            )
        ),
    )

    assert descriptor.artifact_content_schema is not None
    assert descriptor.artifact_content_schema["title"] == "ContentDocument"
    dumped = descriptor.model_dump(mode="json")
    assert dumped["ownership"]["version"] == "1.0"
    assert {field["ownership"] for field in dumped["ownership"]["fields"]} == {
        ownership.value for ownership in FieldOwnership
    }
    assert dumped["ownership"]["fields"][3]["path"] == "/items/-/candidate"


def test_manifest_rejects_unresolvable_or_contentless_ownership_paths() -> None:
    missing_field = OwnershipContract(
        fields=(
            OwnershipField(
                surface=OwnershipSurface.ARTIFACT_CONTENT,
                path="/items/-/missing",
                ownership=FieldOwnership.AI_CANDIDATE,
                label="Missing",
                description="This field does not exist.",
            ),
        )
    )
    with pytest.raises(ValueError, match="does not resolve in artifact_content schema"):
        manifest(
            artifact_content_model=ContentDocument,
            ownership=missing_field,
        )

    contentless = OwnershipContract(
        fields=(
            OwnershipField(
                surface=OwnershipSurface.ARTIFACT_CONTENT,
                path="/policy",
                ownership=FieldOwnership.PROGRAM_FACT,
                label="Policy",
                description="No content schema was declared.",
            ),
        )
    )
    with pytest.raises(ValueError, match="targets unavailable artifact_content schema"):
        manifest(ownership=contentless)


def test_ownership_paths_are_canonical_unique_and_non_overlapping() -> None:
    field = OwnershipField(
        surface=OwnershipSurface.OPTIONS,
        path="/enabled",
        ownership=FieldOwnership.USER_CHOICE,
        label="Enabled",
        description="A user-selected option.",
    )
    with pytest.raises(ValueError, match="duplicate ownership path"):
        OwnershipContract(fields=(field, field))

    with pytest.raises(ValueError, match="must not overlap"):
        OwnershipContract(
            fields=(
                OwnershipField(
                    surface=OwnershipSurface.ARTIFACT_CONTENT,
                    path="/items",
                    ownership=FieldOwnership.PROGRAM_FACT,
                    label="Items",
                    description="All items.",
                ),
                OwnershipField(
                    surface=OwnershipSurface.ARTIFACT_CONTENT,
                    path="/items/-/candidate",
                    ownership=FieldOwnership.AI_CANDIDATE,
                    label="Candidate",
                    description="One candidate field.",
                ),
            )
        )

    with pytest.raises(ValueError, match="absolute JSON Pointer"):
        OwnershipField(
            surface=OwnershipSurface.OPTIONS,
            path="enabled",
            ownership=FieldOwnership.USER_CHOICE,
            label="Enabled",
            description="A user-selected option.",
        )


@pytest.mark.parametrize("path", ("/", "/empty//token", "/bad~escape", "/bad~"))
def test_ownership_paths_reject_empty_tokens_and_invalid_pointer_escapes(path: str) -> None:
    with pytest.raises(ValueError):
        OwnershipField(
            surface=OwnershipSurface.OPTIONS,
            path=path,
            ownership=FieldOwnership.USER_CHOICE,
            label="Invalid",
            description="An invalid pointer must fail closed.",
        )


def test_ownership_schema_resolution_handles_ref_siblings_and_tuple_items() -> None:
    contract = OwnershipContract(
        fields=(
            OwnershipField(
                surface=OwnershipSurface.INPUT,
                path="/extension",
                ownership=FieldOwnership.PROGRAM_FACT,
                label="Extension",
                description="A property declared beside a local reference.",
            ),
            OwnershipField(
                surface=OwnershipSurface.OPTIONS,
                path="/-/choice",
                ownership=FieldOwnership.USER_CHOICE,
                label="Choice",
                description="A field in one positional array item schema.",
            ),
        )
    )

    validate_ownership_contract(
        contract,
        input_schema={
            "$defs": {"Base": {"type": "object", "properties": {"base": {"type": "str"}}}},
            "$ref": "#/$defs/Base",
            "properties": {"extension": {"type": "string"}},
        },
        options_schema={
            "type": "array",
            "prefixItems": [{"type": "object", "properties": {"choice": {"type": "string"}}}],
        },
        artifact_content_schema=None,
    )


def test_ownership_schema_resolution_fails_closed_for_cyclic_or_external_refs() -> None:
    contract = OwnershipContract(
        fields=(
            OwnershipField(
                surface=OwnershipSurface.INPUT,
                path="/missing",
                ownership=FieldOwnership.PROGRAM_FACT,
                label="Missing",
                description="The reference does not establish this property.",
            ),
        )
    )
    for schema in (
        {"$defs": {"Loop": {"$ref": "#/$defs/Loop"}}, "$ref": "#/$defs/Loop"},
        {"$ref": "https://schemas.example/input.json"},
    ):
        with pytest.raises(ValueError, match="does not resolve"):
            validate_ownership_contract(
                contract,
                input_schema=schema,
                options_schema={},
                artifact_content_schema=None,
            )
