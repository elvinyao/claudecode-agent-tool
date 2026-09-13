"""Versioned, machine-readable field ownership for workbench surfaces.

Ownership paths are RFC 6901 JSON Pointers over model *instances*.  The ``-``
token is the version-1 collection-item marker, so ``/cards/-/front`` describes
``front`` on every item in the ``cards`` array.  Contracts are checked against
the corresponding JSON Schema when a plugin manifest is constructed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import Enum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from agent_core.contracts import StrictFrozenModel

OWNERSHIP_CONTRACT_VERSION = "1.0"
ARRAY_ITEM_TOKEN = "-"
_INVALID_POINTER_ESCAPE = re.compile(r"~(?:[^01]|$)")


class FieldOwnership(str, Enum):
    """Who may author a field before deterministic validation and publication."""

    PROGRAM_FACT = "program_fact"
    USER_CHOICE = "user_choice"
    AI_CANDIDATE = "ai_candidate"
    POLICY_LOCKED = "policy_locked"
    ACTION_INPUT = "action_input"


class OwnershipSurface(str, Enum):
    """Schema surface against which an ownership pointer is resolved."""

    INPUT = "input"
    OPTIONS = "options"
    ARTIFACT_CONTENT = "artifact_content"


class OwnershipContractError(ValueError):
    """An ownership declaration cannot be resolved without ambiguity."""


class OwnershipField(StrictFrozenModel):
    """One owned field addressed through a version-1 instance JSON Pointer."""

    surface: OwnershipSurface
    path: str = Field(min_length=2, max_length=512)
    ownership: FieldOwnership
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        _pointer_tokens(value)
        return value

    @field_validator("label", "description")
    @classmethod
    def validate_display_text(cls, value: str) -> str:
        if value != value.strip() or "\x00" in value or "\r" in value or "\n" in value:
            raise ValueError("ownership display text must be trimmed single-line text")
        return value


class OwnershipContract(StrictFrozenModel):
    """Versioned ownership metadata attached to a plugin manifest."""

    version: Literal["1.0"] = OWNERSHIP_CONTRACT_VERSION
    fields: tuple[OwnershipField, ...] = ()

    @model_validator(mode="after")
    def validate_unambiguous_paths(self) -> OwnershipContract:
        seen: set[tuple[OwnershipSurface, str]] = set()
        by_surface: dict[OwnershipSurface, list[tuple[str, ...]]] = {}
        for declaration in self.fields:
            key = (declaration.surface, declaration.path)
            if key in seen:
                raise ValueError(
                    f"duplicate ownership path for {declaration.surface.value}: {declaration.path}"
                )
            seen.add(key)
            tokens = _pointer_tokens(declaration.path)
            existing_paths = by_surface.setdefault(declaration.surface, [])
            if any(
                _is_prefix(tokens, existing) or _is_prefix(existing, tokens)
                for existing in existing_paths
            ):
                raise ValueError(
                    f"ownership paths on the same surface must not overlap: {declaration.path}"
                )
            existing_paths.append(tokens)
        return self


def validate_ownership_contract(
    contract: OwnershipContract,
    *,
    input_schema: Mapping[str, Any],
    options_schema: Mapping[str, Any],
    artifact_content_schema: Mapping[str, Any] | None,
) -> None:
    """Resolve every declared field against its selected instance schema."""

    schemas: dict[OwnershipSurface, Mapping[str, Any] | None] = {
        OwnershipSurface.INPUT: input_schema,
        OwnershipSurface.OPTIONS: options_schema,
        OwnershipSurface.ARTIFACT_CONTENT: artifact_content_schema,
    }
    for declaration in contract.fields:
        schema = schemas[declaration.surface]
        if schema is None:
            raise OwnershipContractError(
                f"ownership path {declaration.path!r} targets unavailable "
                f"{declaration.surface.value} schema"
            )
        if not _schema_accepts_pointer(schema, declaration.path):
            raise OwnershipContractError(
                f"ownership path {declaration.path!r} does not resolve in "
                f"{declaration.surface.value} schema"
            )


def _pointer_tokens(pointer: str) -> tuple[str, ...]:
    if not pointer.startswith("/"):
        raise ValueError("ownership path must be an absolute JSON Pointer")
    raw_tokens = pointer[1:].split("/")
    if any(not token for token in raw_tokens):
        raise ValueError("ownership path tokens must not be empty")
    if any(_INVALID_POINTER_ESCAPE.search(token) for token in raw_tokens):
        raise ValueError("ownership path contains an invalid JSON Pointer escape")
    return tuple(token.replace("~1", "/").replace("~0", "~") for token in raw_tokens)


def _is_prefix(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return len(left) <= len(right) and right[: len(left)] == left


def _schema_accepts_pointer(root: Mapping[str, Any], pointer: str) -> bool:
    candidates: tuple[Mapping[str, Any], ...] = (root,)
    for token in _pointer_tokens(pointer):
        next_candidates: list[Mapping[str, Any]] = []
        for candidate in candidates:
            for expanded in _expanded_schema_nodes(root, candidate):
                properties = expanded.get("properties")
                if isinstance(properties, Mapping) and token in properties:
                    child = properties[token]
                    if isinstance(child, Mapping):
                        next_candidates.append(child)
                    continue
                if token == ARRAY_ITEM_TOKEN:
                    items = expanded.get("items")
                    if isinstance(items, Mapping):
                        next_candidates.append(items)
                    prefix_items = expanded.get("prefixItems")
                    if isinstance(prefix_items, list):
                        next_candidates.extend(
                            item for item in prefix_items if isinstance(item, Mapping)
                        )
        if not next_candidates:
            return False
        candidates = tuple(next_candidates)
    return bool(candidates)


def _expanded_schema_nodes(
    root: Mapping[str, Any],
    schema: Mapping[str, Any],
    *,
    visited_refs: frozenset[str] = frozenset(),
) -> tuple[Mapping[str, Any], ...]:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        siblings = {key: value for key, value in schema.items() if key != "$ref"}
        expanded_siblings = (
            _expanded_schema_nodes(root, siblings, visited_refs=visited_refs) if siblings else ()
        )
        if reference in visited_refs:
            return expanded_siblings
        resolved = _resolve_local_reference(root, reference)
        if resolved is None:
            return expanded_siblings
        return (
            *_expanded_schema_nodes(
                root,
                resolved,
                visited_refs=visited_refs | {reference},
            ),
            *expanded_siblings,
        )

    alternatives: list[Mapping[str, Any]] = []
    for keyword in ("allOf", "anyOf", "oneOf"):
        value = schema.get(keyword)
        if isinstance(value, list):
            alternatives.extend(item for item in value if isinstance(item, Mapping))
    if alternatives:
        expanded: list[Mapping[str, Any]] = []
        if "properties" in schema or "items" in schema or "prefixItems" in schema:
            expanded.append(schema)
        for alternative in alternatives:
            expanded.extend(_expanded_schema_nodes(root, alternative, visited_refs=visited_refs))
        return tuple(expanded)
    return (schema,)


def _resolve_local_reference(root: Mapping[str, Any], reference: str) -> Mapping[str, Any] | None:
    if not reference.startswith("#/"):
        return None
    current: Any = root
    try:
        tokens = _pointer_tokens(reference[1:])
    except ValueError:
        return None
    for token in tokens:
        if not isinstance(current, Mapping) or token not in current:
            return None
        current = current[token]
    return current if isinstance(current, Mapping) else None


__all__ = [
    "ARRAY_ITEM_TOKEN",
    "OWNERSHIP_CONTRACT_VERSION",
    "FieldOwnership",
    "OwnershipContract",
    "OwnershipContractError",
    "OwnershipField",
    "OwnershipSurface",
    "validate_ownership_contract",
]
