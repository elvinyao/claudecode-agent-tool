"""Trivy domain adapter plugin implementing the Agent Framework DomainAdapter interface."""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from agent_core.models.contracts import AnalysisOutcome as CoreAnalysisOutcome
from agent_core.models.contracts import BaseFact
from agent_core.pipeline import DomainAdapter
from trivy_ai_report.models import Finding, NormalizedReport, Recommendation
from trivy_ai_report.prompts import build_analysis_prompt
from trivy_ai_report.renderer import render_html
from trivy_ai_report.rules import merge_recommendations
from trivy_ai_report.trivy import parse_trivy_report


class TrivyDomainAdapter(DomainAdapter):
    """Domain adapter encapsulating Trivy JSON parsing, prompt building, and rendering."""

    name: str = "trivy"

    def __init__(self) -> None:
        self._current_report: NormalizedReport | None = None
        self._last_facts_count: int = 0

    @property
    def is_empty(self) -> bool:
        """Return True if the last parsed report contained no findings."""
        return self._last_facts_count == 0

    def parse_input(self, raw_data: dict[str, Any]) -> list[BaseFact]:
        """Parse raw Trivy JSON v2 payload into a list of Finding facts."""
        report = parse_trivy_report(raw_data)
        self._current_report = report
        self._last_facts_count = len(report.findings)
        return list(report.findings)

    def build_prompt(self, facts: list[BaseFact], enrich_web: bool) -> str:
        """Build Chinese remediation analysis prompt for Trivy findings."""
        findings = [f for f in facts if isinstance(f, Finding)]
        return build_analysis_prompt(findings, enrich_web=enrich_web)

    def validate_and_merge(
        self, facts: list[BaseFact], raw_advices: list[Any], failure_reason: str | None
    ) -> tuple[list[Recommendation], list[str], bool]:
        """Validate raw model advice against Trivy facts and merge with fallback rules."""
        findings = [f for f in facts if isinstance(f, Finding)]
        recommendations: list[Recommendation] = []
        for item in raw_advices:
            if isinstance(item, Recommendation):
                recommendations.append(item)
            elif isinstance(item, dict):
                with suppress(Exception):
                    recommendations.append(Recommendation.model_validate(item))
        return merge_recommendations(findings, recommendations, failure_reason=failure_reason)

    def render_output(self, facts: list[BaseFact], outcome: CoreAnalysisOutcome) -> str:
        """Render self-contained Chinese HTML report."""
        report = self._current_report
        if report is None:
            findings = [f for f in facts if isinstance(f, Finding)]
            artifact_name = findings[0].artifact_name if findings else "unknown"
            artifact_type = findings[0].artifact_type if findings else "unknown"
            os_family = findings[0].os_family if findings else ""
            os_version = findings[0].os_version if findings else ""
            os_eosl = findings[0].os_eosl if findings else False
            report = NormalizedReport(
                schema_version=2,
                created_at="",
                artifact_name=artifact_name,
                artifact_type=artifact_type,
                os_family=os_family,
                os_version=os_version,
                os_eosl=os_eosl,
                findings=findings,
            )
        return render_html(report, outcome)


__all__ = ["TrivyDomainAdapter"]
