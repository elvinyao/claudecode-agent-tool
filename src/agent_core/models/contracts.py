from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BaseFact(BaseModel):
    """Domain fact extracted from input data, immutable by default."""

    model_config = ConfigDict(extra="allow", frozen=True)
    fact_id: str = ""
    summary: str = ""


class BaseAdvice(BaseModel):
    """Actionable recommendation mapped to a specific fact."""

    model_config = ConfigDict(extra="allow")
    fact_id: str
    title_zh: str
    actions_zh: list[str]


class AnalysisOutcome(BaseModel):
    """Outcome payload containing metadata, recommendations, and execution warnings."""

    model_config = ConfigDict(extra="allow")
    provider: str
    model: str = "provider-default"
    skill_name: str | None = None
    enrich_web: bool = False
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    recommendations: list[Any] = Field(default_factory=list)
    partial: bool = False
    warnings: list[str] = Field(default_factory=list)

