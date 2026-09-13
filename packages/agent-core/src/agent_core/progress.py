"""Safe, provider-neutral progress events emitted while a workflow is running."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, TypeAlias

from pydantic import Field, field_validator

from agent_core.contracts import StrictFrozenModel

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ProgressEventType(str, Enum):
    """Workflow lifecycle events that never contain prompts or node payloads."""

    STEP_STARTED = "step.started"
    STEP_COMPLETED = "step.completed"
    STEP_CANCELLED = "step.cancelled"
    STEP_FAILED = "step.failed"
    AGENT_BATCH_STARTED = "agent.batch.started"
    AGENT_BATCH_COMPLETED = "agent.batch.completed"
    RETRY_SCHEDULED = "retry.scheduled"
    VALIDATION_COMPLETED = "validation.completed"


class WorkflowProgress(StrictFrozenModel):
    """Bounded metadata suitable for audit records and a workbench timeline."""

    run_id: str
    type: ProgressEventType
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    node_id: str
    node_kind: Literal["transform", "agent", "action"]
    node_status: Literal["running", "succeeded", "skipped", "cancelled", "failed"] | None = None
    attempt: int | None = Field(default=None, ge=1, le=10)
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    delay_seconds: float | None = Field(default=None, ge=0, le=3600)
    batch_size: int | None = Field(default=None, ge=0, le=1_000_000)
    accepted_count: int | None = Field(default=None, ge=0, le=1_000_000)
    duration_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    error_code: str | None = Field(default=None, max_length=128)

    @field_validator("run_id", "node_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if _IDENTIFIER.fullmatch(value) is None:
            raise ValueError("progress identifiers must use framework identifier syntax")
        return value

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("progress occurred_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        if value is not None and _IDENTIFIER.fullmatch(value) is None:
            raise ValueError("progress error_code must use framework identifier syntax")
        return value


ProgressSink: TypeAlias = Callable[[WorkflowProgress], Awaitable[None]]


__all__ = [
    "ProgressEventType",
    "ProgressSink",
    "WorkflowProgress",
]
