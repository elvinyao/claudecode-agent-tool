"""Strict, domain-neutral contracts shared by the Agent Framework core."""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import Enum
from typing import Any, Generic, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

OutputT = TypeVar("OutputT", bound=BaseModel)
OptionsT = TypeVar("OptionsT", bound=BaseModel)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class AgentCoreError(Exception):
    """Base class for typed framework failures."""


class ContractViolationError(AgentCoreError, ValueError):
    """A value violates a framework contract outside Pydantic validation."""


class RunStatus(str, Enum):
    """Lifecycle status used by workflows and asynchronous job transports."""

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActionMode(str, Enum):
    """Maximum side-effect level authorized for action nodes."""

    DISABLED = "disabled"
    DRY_RUN = "dry_run"
    APPLY = "apply"


class StrictFrozenModel(BaseModel):
    """Base for immutable contracts that reject coercion and unknown fields."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        frozen=True,
        strict=True,
    )


class ToolPolicy(StrictFrozenModel):
    """Explicit allow-list for tools that a provider may expose to a model."""

    web_access: bool = False
    allowed_tools: tuple[str, ...] = ()

    @field_validator("allowed_tools")
    @classmethod
    def validate_allowed_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not tool or tool != tool.strip() for tool in value):
            raise ValueError("allowed tool names must be non-empty and trimmed")
        if len(set(value)) != len(value):
            raise ValueError("allowed tool names must be unique")
        return value


class ArtifactInput(StrictFrozenModel, Generic[OptionsT]):
    """Standard raw-artifact envelope accepted by every domain workflow."""

    content: bytes
    options: OptionsT
    filename: str = Field(min_length=1, max_length=255)

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if value != value.strip() or value in {".", ".."}:
            raise ValueError("artifact filename must be non-empty and trimmed")
        if "/" in value or "\\" in value or "\r" in value or "\n" in value:
            raise ValueError("artifact filename must not contain a path")
        return value


class ArtifactOutput(StrictFrozenModel):
    """Standard terminal artifact returned by every domain workflow."""

    content: bytes
    media_type: str = Field(default="application/octet-stream", min_length=1, max_length=255)
    filename: str = Field(default="artifact.bin", min_length=1, max_length=255)
    partial: bool = False
    warnings: tuple[str, ...] = ()

    @field_validator("media_type")
    @classmethod
    def validate_media_type(cls, value: str) -> str:
        if value != value.strip() or "\r" in value or "\n" in value:
            raise ValueError("artifact media type must be trimmed single-line text")
        return value

    @field_validator("filename")
    @classmethod
    def validate_output_filename(cls, value: str) -> str:
        if value != value.strip() or value in {".", ".."}:
            raise ValueError("artifact filename must be non-empty and trimmed")
        if "/" in value or "\\" in value or "\r" in value or "\n" in value:
            raise ValueError("artifact filename must not contain a path")
        return value

    @field_validator("warnings")
    @classmethod
    def validate_artifact_warnings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not warning or warning != warning.strip() for warning in value):
            raise ValueError("artifact warnings must be non-empty and trimmed")
        return value


class AgentRequest(StrictFrozenModel, Generic[OutputT]):
    """One provider-neutral request for typed structured output."""

    request_id: str = Field(default_factory=lambda: uuid4().hex)
    system_prompt: str = ""
    prompt: str = Field(min_length=1)
    response_model: type[OutputT]
    tool_policy: ToolPolicy = Field(default_factory=ToolPolicy)
    metadata: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        if _IDENTIFIER.fullmatch(value) is None:
            raise ValueError(
                "request_id must be 1-128 letters, digits, dots, underscores, colons, or hyphens"
            )
        return value


class ProviderResult(StrictFrozenModel, Generic[OutputT]):
    """Validated response returned by one provider adapter invocation."""

    request_id: str
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    output: OutputT
    warnings: tuple[str, ...] = ()
    partial: bool = False

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        if _IDENTIFIER.fullmatch(value) is None:
            raise ValueError("request_id is not a valid framework identifier")
        return value

    @field_validator("provider", "model")
    @classmethod
    def validate_trimmed_name(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("provider and model names must be trimmed")
        return value

    @field_validator("warnings")
    @classmethod
    def validate_warnings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not warning or warning != warning.strip() for warning in value):
            raise ValueError("warnings must be non-empty and trimmed")
        return value


__all__ = [
    "ActionMode",
    "AgentCoreError",
    "AgentRequest",
    "ArtifactInput",
    "ArtifactOutput",
    "ContractViolationError",
    "OutputT",
    "OptionsT",
    "ProviderResult",
    "RunStatus",
    "StrictFrozenModel",
    "ToolPolicy",
]
