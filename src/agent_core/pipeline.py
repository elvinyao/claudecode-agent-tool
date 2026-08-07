from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from agent_core.models.contracts import AnalysisOutcome, BaseFact


class DomainAdapter(ABC):
    """Abstract base class for domain parsing, prompt building, validation, and rendering."""

    name: str

    @abstractmethod
    def parse_input(self, raw_data: dict[str, Any]) -> list[BaseFact]:
        """Parse raw input data into domain-specific facts."""
        ...

    @abstractmethod
    def build_prompt(self, facts: list[BaseFact], enrich_web: bool) -> str:
        """Build analysis prompt from extracted facts and web enrichment option."""
        ...

    @abstractmethod
    def validate_and_merge(
        self, facts: list[BaseFact], raw_advices: list[Any], failure_reason: str | None
    ) -> tuple[list[Any], list[str], bool]:
        """Validate raw model advice output against facts and merge results."""
        ...

    @abstractmethod
    def render_output(self, facts: list[BaseFact], outcome: AnalysisOutcome) -> str:
        """Render final formatted report string from facts and analysis outcome."""
        ...


@dataclass(slots=True)
class PipelineResult:
    """Result container for agent pipeline execution."""

    success: bool
    content: str
    outcome: AnalysisOutcome
    warnings: list[str] = field(default_factory=list)
    partial: bool = False


class AgentPipeline:
    """Orchestrates input parsing, prompt construction, model analysis, and output rendering."""

    def __init__(
        self,
        adapter: DomainAdapter,
        analyzer_factory: Any = None,
        skill_loader: Any = None,
    ):
        self.adapter = adapter
        self.analyzer_factory = analyzer_factory
        self.skill_loader = skill_loader

    async def run(
        self,
        provider: str,
        input_data: dict[str, Any],
        model: str | None = None,
        skill_path: str | None = None,
        skill: Any = None,
        enrich_web: bool = False,
        timeout_seconds: float = 300.0,
    ) -> PipelineResult:
        """Execute the agent pipeline with given provider settings and input data."""
        facts = self.adapter.parse_input(input_data)
        skill_name: str | None = None

        if skill is not None:
            skill_name = getattr(skill, "name", None)
        elif skill_path is not None:
            skill_name = skill_path
            loader = self.skill_loader
            if loader is None:
                try:
                    from agent_core.skills import load_skill

                    loader = load_skill
                except ImportError:
                    loader = None
            if loader is not None:
                try:
                    loaded_skill = loader(skill_path)
                    skill = loaded_skill
                    skill_name = getattr(loaded_skill, "name", skill_name)
                except Exception:
                    pass

        reported_model = model
        if reported_model is None:
            reported_model = "gemini-3.6-flash" if provider == "gemini" else "provider-default"

        if not facts:
            outcome = AnalysisOutcome(
                provider=provider,
                model=reported_model,
                skill_name=skill_name,
                enrich_web=enrich_web,
                recommendations=[],
            )
            content = self.adapter.render_output([], outcome)
            return PipelineResult(success=True, content=content, outcome=outcome)

        prompt = self.adapter.build_prompt(facts, enrich_web=enrich_web)  # noqa: F841

        factory = self.analyzer_factory
        if factory is None:
            from agent_core.providers import create_analyzer

            factory = create_analyzer

        provider_failed = False
        failure_reason: str | None = None
        raw_advices: list[Any] = []

        try:
            analyzer = factory(
                provider,
                model=model,
                enrich_web=enrich_web,
                batch_size=10 if enrich_web else 25,
                skill=skill,
            )
            provider_outcome = await analyzer.analyze(facts, timeout_seconds=timeout_seconds)
            raw_advices = getattr(provider_outcome, "recommendations", [])
            reported_model = getattr(provider_outcome, "model", reported_model)
            outcome_skill_name = getattr(provider_outcome, "skill_name", None)
            if outcome_skill_name:
                skill_name = outcome_skill_name
        except Exception as exc:
            provider_failed = True
            failure_reason = str(exc)

        merged, warnings, partial = self.adapter.validate_and_merge(
            facts, raw_advices, failure_reason
        )

        all_warnings: list[str] = []
        if provider_failed and failure_reason:
            all_warnings.append(f"Agent 分析失败：{failure_reason}")
        all_warnings.extend(warnings)

        outcome = AnalysisOutcome(
            provider=provider,
            model=reported_model,
            skill_name=skill_name,
            enrich_web=enrich_web,
            recommendations=merged,
            partial=partial or provider_failed,
            warnings=all_warnings,
        )

        content = self.adapter.render_output(facts, outcome)
        return PipelineResult(
            success=True,
            content=content,
            outcome=outcome,
            warnings=all_warnings,
            partial=outcome.partial,
        )

