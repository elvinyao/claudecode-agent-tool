"""Local validation for the Codex strict structured-output schema subset."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from agent_core.providers.errors import ProviderConfigurationError

_SCHEMA_MAPPING_KEYWORDS = (
    "$defs",
    "definitions",
    "properties",
    "patternProperties",
    "dependentSchemas",
)
_SCHEMA_SINGLE_KEYWORDS = (
    "additionalProperties",
    "contains",
    "contentSchema",
    "else",
    "if",
    "items",
    "not",
    "propertyNames",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
)
_SCHEMA_SEQUENCE_KEYWORDS = ("allOf", "anyOf", "oneOf", "prefixItems")


def _schema_children(schema: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    for keyword in _SCHEMA_MAPPING_KEYWORDS:
        value = schema.get(keyword)
        if isinstance(value, Mapping):
            for child in value.values():
                if isinstance(child, Mapping):
                    yield child

    for keyword in _SCHEMA_SINGLE_KEYWORDS:
        value = schema.get(keyword)
        if isinstance(value, Mapping):
            yield value

    for keyword in _SCHEMA_SEQUENCE_KEYWORDS:
        value = schema.get(keyword)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for child in value:
                if isinstance(child, Mapping):
                    yield child


def _is_object_schema(schema: Mapping[str, Any]) -> bool:
    schema_type = schema.get("type")
    return (
        schema_type == "object"
        or (
            isinstance(schema_type, Sequence)
            and not isinstance(schema_type, (str, bytes, bytearray))
            and "object" in schema_type
        )
        or "properties" in schema
    )


def validate_codex_output_schema(schema: Mapping[str, Any]) -> None:
    """Fail locally when an object schema cannot be used in Codex strict mode.

    Codex structured outputs require every object property to be explicitly
    required. Optional values therefore need a nullable type while the field
    itself remains present. The check walks references' definitions and schema
    composition branches, not only the root model.
    """

    if not isinstance(schema, Mapping):
        raise ProviderConfigurationError(
            "Codex structured-output schema must be a JSON object",
            provider="codex",
        )

    pending = [schema]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in visited:
            continue
        visited.add(identity)

        if _is_object_schema(current):
            properties = current.get("properties", {})
            if not isinstance(properties, Mapping) or not all(
                isinstance(name, str) for name in properties
            ):
                raise ProviderConfigurationError(
                    "Codex structured-output object properties must be a JSON object",
                    provider="codex",
                )

            required = current.get("required")
            if not isinstance(required, list) or not all(
                isinstance(name, str) for name in required
            ):
                raise ProviderConfigurationError(
                    "Codex structured-output object schemas must define required as an array",
                    provider="codex",
                )

            property_names = set(properties)
            if len(required) != len(property_names) or set(required) != property_names:
                raise ProviderConfigurationError(
                    "Codex structured-output required fields must exactly match object properties",
                    provider="codex",
                )

            if current.get("additionalProperties") is not False:
                raise ProviderConfigurationError(
                    "Codex structured-output object schemas must set additionalProperties to false",
                    provider="codex",
                )

        pending.extend(_schema_children(current))


__all__ = ["validate_codex_output_schema"]
