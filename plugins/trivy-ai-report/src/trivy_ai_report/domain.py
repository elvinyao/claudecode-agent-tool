"""Pure domain operations used by the Trivy workflow plugin."""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_core.contracts import ArtifactInput, ArtifactOutput, ProviderResult
from trivy_ai_report.models import (
    AnalysisOutcome,
    Finding,
    NormalizedReport,
    Recommendation,
    RecommendationBatch,
    ResearchStatus,
    TrivyRunOptions,
)
from trivy_ai_report.renderer import render_html
from trivy_ai_report.rules import merge_recommendations
from trivy_ai_report.trivy import TrivyInputError, parse_trivy_report


class TrivyBatch(BaseModel):
    """One stable, ordered unit of Agent work."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: str = Field(pattern=r"^batch-[0-9]{4}$")
    findings: tuple[Finding, ...]


class ParsedTrivyRun(BaseModel):
    """Request-local parsed document and validated plugin options."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report: NormalizedReport
    options: TrivyRunOptions


class TrivyWorkflowInput(ArtifactInput[TrivyRunOptions]):
    """Raw artifact and options accepted by the Trivy workflow root."""

    options: TrivyRunOptions = Field(default_factory=TrivyRunOptions)
    filename: str = "trivy-report.json"


class RenderedTrivyReport(ArtifactOutput):
    """Final plugin artifact, including domain quality metadata."""

    media_type: str = "text/html; charset=utf-8"
    filename: str = "trivy-report.html"
    partial: bool = False
    warnings: tuple[str, ...] = ()


class TrivyAgentBatchOutcome(BaseModel):
    """Success or exhausted Provider failure for one planned batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: str = Field(pattern=r"^batch-[0-9]{4}$")
    result: ProviderResult[RecommendationBatch] | None = None
    error_code: str | None = Field(default=None, max_length=100)
    error_message: str | None = Field(default=None, max_length=500)
    partial: bool = False
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_one_outcome(self) -> TrivyAgentBatchOutcome:
        if (self.result is None) == (self.error_code is None):
            raise ValueError("exactly one of result or error_code is required")
        if self.result is not None and self.result.request_id != self.batch_id:
            raise ValueError("Provider result request_id must match batch_id")
        if self.error_code is not None and not self.error_message:
            raise ValueError("failed batch must include a safe error_message")
        return self


class TrivyRenderContext(BaseModel):
    """Validated report plus merged advice passed to the renderer node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    parsed: ParsedTrivyRun
    analysis: AnalysisOutcome


def parse_input_bytes(raw: bytes, options: TrivyRunOptions) -> ParsedTrivyRun:
    """Decode UTF-8 JSON and strictly normalize one Trivy native v2 report."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TrivyInputError("Trivy 输入必须是 UTF-8 JSON") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TrivyInputError(
            f"Trivy 输入不是有效 JSON（第 {exc.lineno} 行，第 {exc.colno} 列）"
        ) from exc
    return ParsedTrivyRun(report=parse_trivy_report(data), options=options)


def parse_workflow_input(value: TrivyWorkflowInput) -> ParsedTrivyRun:
    return parse_input_bytes(value.content, value.options)


def plan_batches(parsed: ParsedTrivyRun) -> tuple[TrivyBatch, ...]:
    """Create stable batches; web-enriched work is deliberately smaller."""

    size = parsed.options.batch_size or (10 if parsed.options.enrich_web else 25)
    findings = parsed.report.findings
    return tuple(
        TrivyBatch(
            batch_id=f"batch-{index + 1:04d}",
            findings=tuple(findings[offset : offset + size]),
        )
        for index, offset in enumerate(range(0, len(findings), size))
    )


def finalize_analysis(
    parsed: ParsedTrivyRun,
    recommendations: list[Recommendation],
    *,
    provider: str,
    model: str,
    skill_name: str | None,
    warnings: list[str] | None = None,
    failure_reason: str | None = None,
    force_partial: bool = False,
) -> AnalysisOutcome:
    """Apply domain guardrails, fill missing advice, and build render input."""

    candidates = recommendations
    if not parsed.options.enrich_web:
        candidates = [
            item.model_copy(
                update={"research_status": ResearchStatus.NOT_REQUESTED, "evidence": []}
            )
            for item in candidates
        ]
    merged, validation_warnings, partial = merge_recommendations(
        parsed.report.findings,
        candidates,
        failure_reason=failure_reason,
    )
    return AnalysisOutcome(
        provider=provider,
        model=model,
        skill_name=skill_name,
        enrich_web=parsed.options.enrich_web,
        recommendations=merged,
        partial=force_partial or partial or bool(warnings),
        warnings=[*(warnings or []), *validation_warnings],
    )


def finalize_batch_outcomes(
    parsed: ParsedTrivyRun,
    outcomes: tuple[TrivyAgentBatchOutcome, ...],
    *,
    provider: str,
    requested_model: str | None,
    skill_name: str | None,
) -> AnalysisOutcome:
    """Flatten successful batches and deterministically replace failed work."""

    recommendations: list[Recommendation] = []
    warnings: list[str] = []
    failures: list[TrivyAgentBatchOutcome] = []
    provider_partial = False
    resolved_model = requested_model or "provider-default"
    for outcome in outcomes:
        warnings.extend(outcome.warnings)
        if outcome.result is None:
            failures.append(outcome)
            warnings.append(
                f"{provider} {outcome.batch_id} 分析失败（{outcome.error_code}），"
                "已使用本地保守规则。"
            )
            continue
        resolved_model = outcome.result.model
        provider_partial = provider_partial or outcome.partial or outcome.result.partial
        warnings.extend(outcome.result.warnings)
        recommendations.extend(outcome.result.output.recommendations)

    failure_reason = None
    if failures and len(failures) == len(outcomes):
        failure_reason = failures[0].error_message
    return finalize_analysis(
        parsed,
        recommendations,
        provider=provider,
        model=resolved_model,
        skill_name=skill_name,
        warnings=warnings,
        failure_reason=failure_reason,
        force_partial=provider_partial,
    )


def render_report(
    parsed: ParsedTrivyRun,
    outcome: AnalysisOutcome,
    *,
    filename: str = "trivy-report.html",
) -> RenderedTrivyReport:
    html = render_html(parsed.report, outcome)
    return RenderedTrivyReport(
        content=html.encode("utf-8"),
        filename=filename,
        partial=outcome.partial,
        warnings=tuple(outcome.warnings),
    )


__all__ = [
    "ParsedTrivyRun",
    "RenderedTrivyReport",
    "TrivyAgentBatchOutcome",
    "TrivyBatch",
    "TrivyRenderContext",
    "TrivyWorkflowInput",
    "finalize_analysis",
    "parse_input_bytes",
    "parse_workflow_input",
    "plan_batches",
    "render_report",
]
