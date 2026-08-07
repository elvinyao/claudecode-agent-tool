from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from agent_core.models.contracts import AnalysisOutcome, BaseFact


class DomainAdapter(ABC):
    name: str

    @abstractmethod
    def parse_input(self, raw_data: dict[str, Any]) -> list[BaseFact]: ...

    @abstractmethod
    def build_prompt(self, facts: list[BaseFact], enrich_web: bool) -> str: ...

    @abstractmethod
    def validate_and_merge(
        self, facts: list[BaseFact], raw_advices: list[Any], failure_reason: str | None
    ) -> tuple[list[Any], list[str], bool]: ...

    @abstractmethod
    def render_output(self, facts: list[BaseFact], outcome: AnalysisOutcome) -> str: ...


@dataclass(slots=True)
class PipelineResult:
    success: bool
    content: str
    outcome: AnalysisOutcome
    warnings: list[str] = field(default_factory=list)
    partial: bool = False


class AgentPipeline:
    def __init__(self, adapter: DomainAdapter):
        self.adapter = adapter

    async def run(
        self,
        provider: str,
        input_data: dict[str, Any],
        model: str | None = None,
        skill_path: str | None = None,
        enrich_web: bool = False,
        timeout_seconds: float = 300.0,
    ) -> PipelineResult:
        facts = self.adapter.parse_input(input_data)
        if not facts:
            outcome = AnalysisOutcome(
                provider=provider,
                model=model or "provider-default",
                enrich_web=enrich_web,
            )
            content = self.adapter.render_output([], outcome)
            return PipelineResult(success=True, content=content, outcome=outcome)

        # Mock / Provider execution boundary
        outcome = AnalysisOutcome(provider=provider, model=model or "default")
        merged, warnings, partial = self.adapter.validate_and_merge(facts, [], None)
        outcome.recommendations = merged
        outcome.warnings = warnings
        outcome.partial = partial

        content = self.adapter.render_output(facts, outcome)
        return PipelineResult(
            success=True,
            content=content,
            outcome=outcome,
            warnings=warnings,
            partial=partial,
        )
