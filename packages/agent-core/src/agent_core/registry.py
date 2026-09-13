"""Fail-closed discovery and per-run construction of trusted domain plugins."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator, model_validator

from agent_core.contracts import (
    AgentCoreError,
    ArtifactInput,
    ArtifactOutput,
    StrictFrozenModel,
)
from agent_core.ownership import OwnershipContract, validate_ownership_contract

CORE_API_VERSION = "1.0"
DOMAIN_PLUGIN_ENTRYPOINT_GROUP = "agent_core.domain_plugins"
_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class PluginRegistryError(AgentCoreError):
    """Base class for plugin discovery and construction failures."""


class DuplicatePluginError(PluginRegistryError, ValueError):
    """Two registrations use the same plugin identifier."""


class UnknownPluginError(PluginRegistryError, LookupError):
    """No plugin is registered under the requested identifier."""


class PluginApiVersionError(PluginRegistryError, ValueError):
    """A plugin targets a different core API version."""


class PluginLoadError(PluginRegistryError, RuntimeError):
    """An entry point cannot be loaded or its factory cannot be invoked."""


class PluginContractError(PluginRegistryError, TypeError):
    """A factory product does not satisfy the minimal plugin contract."""


@runtime_checkable
class DomainPlugin(Protocol):
    """Trusted plugin instance created for exactly one framework run."""

    manifest: PluginManifest
    plugin_id: str
    api_version: str

    def create_workflow(self, runtime: Any) -> Any: ...


PluginFactory = Callable[[], DomainPlugin]


class PluginManifest(StrictFrozenModel):
    """Typed plugin identity, schemas, and required framework capabilities."""

    plugin_id: str
    api_version: str = Field(min_length=1)
    version: str = Field(min_length=1)
    display_name: str = Field(min_length=1, max_length=200)
    input_model: type[BaseModel]
    options_model: type[BaseModel]
    output_model: type[BaseModel]
    artifact_content_model: type[BaseModel] | None = None
    ownership: OwnershipContract | None = None
    required_capabilities: tuple[str, ...] = ()

    @field_validator("plugin_id")
    @classmethod
    def validate_plugin_id(cls, value: str) -> str:
        if _PLUGIN_ID.fullmatch(value) is None:
            raise ValueError(
                "plugin_id must start with a lowercase letter and contain at most "
                "64 lowercase letters, digits, underscores, or hyphens"
            )
        return value

    @field_validator("api_version", "version", "display_name")
    @classmethod
    def validate_trimmed_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("plugin manifest strings must be trimmed")
        return value

    @field_validator("required_capabilities")
    @classmethod
    def validate_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("required plugin capabilities must be unique")
        if any(_PLUGIN_ID.fullmatch(item) is None for item in value):
            raise ValueError("required plugin capabilities must be lowercase identifiers")
        return value

    @field_validator("input_model")
    @classmethod
    def validate_input_model(cls, value: type[BaseModel]) -> type[BaseModel]:
        if not issubclass(value, ArtifactInput):
            raise ValueError("plugin input_model must inherit ArtifactInput")
        return value

    @field_validator("output_model")
    @classmethod
    def validate_output_model(cls, value: type[BaseModel]) -> type[BaseModel]:
        if not issubclass(value, ArtifactOutput):
            raise ValueError("plugin output_model must inherit ArtifactOutput")
        return value

    @model_validator(mode="after")
    def validate_ownership(self) -> PluginManifest:
        if self.ownership is not None:
            validate_ownership_contract(
                self.ownership,
                input_schema=self.input_model.model_json_schema(),
                options_schema=self.options_model.model_json_schema(),
                artifact_content_schema=(
                    self.artifact_content_model.model_json_schema()
                    if self.artifact_content_model is not None
                    else None
                ),
            )
        return self


class PluginDescriptor(StrictFrozenModel):
    """JSON-schema metadata safely exposed to CLI and Web transports."""

    plugin_id: str
    api_version: str
    version: str
    display_name: str
    source: str
    required_capabilities: tuple[str, ...] = ()
    input_schema: dict[str, Any]
    options_schema: dict[str, Any]
    output_schema: dict[str, Any]
    artifact_content_schema: dict[str, Any] | None = None
    ownership: OwnershipContract | None = None

    @classmethod
    def from_manifest(cls, manifest: PluginManifest, *, source: str) -> PluginDescriptor:
        return cls(
            plugin_id=manifest.plugin_id,
            api_version=manifest.api_version,
            version=manifest.version,
            display_name=manifest.display_name,
            source=source,
            required_capabilities=manifest.required_capabilities,
            input_schema=manifest.input_model.model_json_schema(),
            options_schema=manifest.options_model.model_json_schema(),
            output_schema=manifest.output_model.model_json_schema(),
            artifact_content_schema=(
                manifest.artifact_content_model.model_json_schema()
                if manifest.artifact_content_model is not None
                else None
            ),
            ownership=manifest.ownership,
        )


@dataclass(slots=True)
class _Registration:
    descriptor: PluginDescriptor
    manifest: PluginManifest
    factory: PluginFactory
    last_instance: DomainPlugin | None = None


class PluginRegistry:
    """Registry that stores factories and returns a fresh plugin for every run."""

    def __init__(self, *, core_api_version: str = CORE_API_VERSION) -> None:
        if not core_api_version:
            raise ValueError("core_api_version must not be empty")
        self.core_api_version = core_api_version
        self._registrations: dict[str, _Registration] = {}

    def register(
        self,
        plugin_id: str,
        factory: PluginFactory,
        *,
        api_version: str | None = None,
        manifest: PluginManifest | None = None,
        source: str = "explicit",
    ) -> PluginDescriptor:
        """Register a factory and validate its manifest before accepting traffic."""

        if not isinstance(plugin_id, str) or _PLUGIN_ID.fullmatch(plugin_id) is None:
            raise PluginContractError("registration plugin_id is invalid")
        if not callable(factory):
            raise PluginContractError(f"plugin factory for {plugin_id!r} is not callable")
        prototype: DomainPlugin | None = None
        if manifest is None:
            prototype = self._invoke_factory(plugin_id, factory)
            manifest = _plugin_manifest(prototype, plugin_id)
        if manifest.plugin_id != plugin_id:
            raise PluginContractError(
                f"registration {plugin_id!r} received manifest for {manifest.plugin_id!r}"
            )
        if api_version is not None and manifest.api_version != api_version:
            raise PluginApiVersionError(
                f"plugin {plugin_id!r} manifest API {manifest.api_version!r} does not match "
                f"registration API {api_version!r}"
            )
        descriptor = PluginDescriptor.from_manifest(manifest, source=source)
        self._require_compatible_api(descriptor)
        if descriptor.plugin_id in self._registrations:
            raise DuplicatePluginError(f"duplicate plugin id: {descriptor.plugin_id!r}")
        self._registrations[descriptor.plugin_id] = _Registration(
            descriptor=descriptor,
            manifest=manifest,
            factory=factory,
            last_instance=prototype,
        )
        return descriptor

    def discover(
        self,
        *,
        group: str = DOMAIN_PLUGIN_ENTRYPOINT_GROUP,
        entry_points: Iterable[Any] | None = None,
    ) -> tuple[PluginDescriptor, ...]:
        """Discover factories from the standard entry-point group and fail closed."""

        discovered = (
            tuple(entry_points) if entry_points is not None else _entry_points_for_group(group)
        )
        descriptors: list[PluginDescriptor] = []
        for entry_point in sorted(discovered, key=lambda item: item.name):
            entry_group = getattr(entry_point, "group", group)
            if entry_group != group:
                continue
            plugin_id = entry_point.name
            if not isinstance(plugin_id, str) or _PLUGIN_ID.fullmatch(plugin_id) is None:
                raise PluginContractError("plugin entry-point name is invalid")
            source = f"entrypoint:{getattr(entry_point, 'value', plugin_id)}"
            try:
                factory = entry_point.load()
            except Exception as exc:
                raise PluginLoadError(f"cannot load plugin entry point {plugin_id!r}") from exc
            if not callable(factory):
                raise PluginContractError(
                    f"entry point {plugin_id!r} must load a zero-argument factory"
                )

            descriptor = self.register(
                plugin_id,
                factory,
                source=source,
            )
            descriptors.append(descriptor)
        return tuple(descriptors)

    @classmethod
    def from_entry_points(
        cls,
        *,
        core_api_version: str = CORE_API_VERSION,
        group: str = DOMAIN_PLUGIN_ENTRYPOINT_GROUP,
        entry_points: Iterable[Any] | None = None,
    ) -> PluginRegistry:
        """Create a registry populated from installed plugin entry points."""

        registry = cls(core_api_version=core_api_version)
        registry.discover(group=group, entry_points=entry_points)
        return registry

    def list(self) -> tuple[PluginDescriptor, ...]:
        """Return deterministic, public metadata without loading plugin code again."""

        return tuple(
            registration.descriptor
            for plugin_id, registration in sorted(self._registrations.items())
        )

    def get(self, plugin_id: str) -> PluginDescriptor:
        """Return one descriptor without constructing a plugin instance."""

        try:
            return self._registrations[plugin_id].descriptor
        except KeyError as exc:
            raise UnknownPluginError(f"unknown plugin: {plugin_id!r}") from exc

    def create(self, plugin_id: str) -> DomainPlugin:
        """Invoke the registered factory and enforce a new instance per call/run."""

        try:
            registration = self._registrations[plugin_id]
        except KeyError as exc:
            raise UnknownPluginError(f"unknown plugin: {plugin_id!r}") from exc

        plugin = self._invoke_factory(plugin_id, registration.factory)
        if plugin is registration.last_instance:
            raise PluginContractError(
                f"plugin factory for {plugin_id!r} reused an instance; factories must be per-run"
            )
        manifest = _plugin_manifest(plugin, plugin_id)
        if manifest != registration.manifest:
            raise PluginContractError(f"plugin {plugin_id!r} changed its registered manifest")
        self._require_compatible_api(registration.descriptor)
        registration.last_instance = plugin
        return plugin

    def create_for_run(self, plugin_id: str) -> DomainPlugin:
        """Explicit alias documenting that plugin instances are run-scoped."""

        return self.create(plugin_id)

    def _require_compatible_api(self, descriptor: PluginDescriptor) -> None:
        if descriptor.api_version != self.core_api_version:
            raise PluginApiVersionError(
                f"plugin {descriptor.plugin_id!r} requires core API "
                f"{descriptor.api_version!r}; running core API is {self.core_api_version!r}"
            )

    @staticmethod
    def _invoke_factory(plugin_id: str, factory: PluginFactory) -> DomainPlugin:
        try:
            plugin = factory()
        except Exception as exc:
            raise PluginLoadError(f"plugin factory {plugin_id!r} failed") from exc
        if plugin is None:
            raise PluginContractError(f"plugin factory {plugin_id!r} returned None")
        return plugin


def _plugin_manifest(plugin: Any, plugin_id: str) -> PluginManifest:
    manifest = getattr(plugin, "manifest", None)
    if not isinstance(manifest, PluginManifest):
        raise PluginContractError(
            f"plugin {plugin_id!r} must expose a PluginManifest as 'manifest'"
        )
    if not callable(getattr(plugin, "create_workflow", None)):
        raise PluginContractError(f"plugin {plugin_id!r} must expose create_workflow(runtime)")
    for attribute, expected in (
        ("plugin_id", manifest.plugin_id),
        ("api_version", manifest.api_version),
    ):
        value = getattr(plugin, attribute, None)
        if value != expected:
            raise PluginContractError(
                f"plugin {plugin_id!r} attribute {attribute!r} must match its manifest"
            )
    if manifest.plugin_id != plugin_id:
        raise PluginContractError(
            f"factory for {plugin_id!r} produced manifest id {manifest.plugin_id!r}"
        )
    return manifest


def _entry_points_for_group(group: str) -> tuple[Any, ...]:
    installed = metadata.entry_points()
    if hasattr(installed, "select"):
        return tuple(installed.select(group=group))
    return tuple(installed.get(group, ()))  # type: ignore[union-attr]


__all__ = [
    "CORE_API_VERSION",
    "DOMAIN_PLUGIN_ENTRYPOINT_GROUP",
    "DomainPlugin",
    "DuplicatePluginError",
    "PluginApiVersionError",
    "PluginContractError",
    "PluginDescriptor",
    "PluginFactory",
    "PluginLoadError",
    "PluginManifest",
    "PluginRegistry",
    "PluginRegistryError",
    "UnknownPluginError",
]
