"""Trivy domain plugin composition and Agent request planning."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from agent_core.contracts import AgentRequest, ProviderResult, ToolPolicy
from agent_core.providers import ProviderAdapter
from agent_core.providers.errors import ProviderResponseError
from agent_core.registry import PluginManifest
from agent_core.skills import SkillSpec, load_skill
from agent_core.workflow import AgentNode, RetryPolicy, TransformNode, Workflow, WorkflowContext
from trivy_ai_report.domain import (
    ParsedTrivyRun,
    RenderedTrivyReport,
    TrivyAgentBatchOutcome,
    TrivyRenderContext,
    TrivyWorkflowInput,
    finalize_batch_outcomes,
    parse_workflow_input,
    plan_batches,
    render_report,
)
from trivy_ai_report.models import (
    ProviderRecommendationBatch,
    RecommendationBatch,
    TrivyRunOptions,
)
from trivy_ai_report.prompts import SYSTEM_PROMPT, build_analysis_prompt

PLUGIN_ID = "trivy"
PLUGIN_API_VERSION = "1.0"


class PluginRuntime(Protocol):
    """Run-scoped services supplied by agent-core's composition runtime."""

    provider: ProviderAdapter
    provider_name: str
    model: str | None
    skill_name: str | None
    attempt_timeout_seconds: float | None
    max_agent_concurrency: int


def bundled_skill_path() -> Path:
    """Return the trusted Trivy Skill shipped inside the plugin wheel."""

    return (
        Path(__file__).resolve().parent
        / "bundled_skills"
        / "trivy-remediation"
        / "SKILL.md"
    )


def load_bundled_skill() -> SkillSpec:
    return load_skill(bundled_skill_path())


def plan_agent_requests(
    parsed: ParsedTrivyRun,
) -> tuple[AgentRequest[ProviderRecommendationBatch], ...]:
    """Turn deterministic Trivy batches into provider-neutral typed requests."""

    return tuple(
        AgentRequest[ProviderRecommendationBatch](
            request_id=batch.batch_id,
            system_prompt=SYSTEM_PROMPT,
            prompt=build_analysis_prompt(
                batch.findings,
                enrich_web=parsed.options.enrich_web,
            ),
            response_model=ProviderRecommendationBatch,
            tool_policy=ToolPolicy(web_access=parsed.options.enrich_web),
            metadata={
                "domain": PLUGIN_ID,
                "batch_id": batch.batch_id,
                "finding_ids": tuple(item.finding_id for item in batch.findings),
            },
        )
        for batch in plan_batches(parsed)
    )


def _to_domain_result(
    result: ProviderResult[ProviderRecommendationBatch],
) -> ProviderResult[RecommendationBatch]:
    """Convert validated wire output into the plugin's stable domain contract."""

    return ProviderResult[RecommendationBatch](
        request_id=result.request_id,
        provider=result.provider,
        model=result.model,
        output=result.output.to_domain(),
        warnings=result.warnings,
        partial=result.partial,
    )


class TrivyPlugin:
    """Stateless Trivy plugin; every workflow keeps all state in node outputs."""

    plugin_id = PLUGIN_ID
    api_version = PLUGIN_API_VERSION
    manifest = PluginManifest(
        plugin_id=PLUGIN_ID,
        api_version=PLUGIN_API_VERSION,
        version="0.2.0",
        display_name="Trivy AI Remediation Report",
        input_model=TrivyWorkflowInput,
        options_model=TrivyRunOptions,
        output_model=RenderedTrivyReport,
        required_capabilities=("structured_output",),
    )

    def skills_for_options(self, options: TrivyRunOptions) -> tuple[SkillSpec, ...]:
        return (load_bundled_skill(),) if options.use_bundled_skill else ()

    def create_workflow(self, runtime: PluginRuntime) -> Workflow:
        async def analyze_one(
            request: AgentRequest[ProviderRecommendationBatch],
            _context: WorkflowContext,
        ) -> TrivyAgentBatchOutcome:
            result: ProviderResult[ProviderRecommendationBatch] = (
                await runtime.provider.execute(request)
            )
            expected = set(request.metadata["finding_ids"])
            returned = [item.finding_id for item in result.output.recommendations]
            if len(returned) != len(set(returned)) or any(
                finding_id not in expected for finding_id in returned
            ):
                raise ProviderResponseError(
                    "Trivy provider output contains duplicate or cross-batch finding_id",
                    provider=runtime.provider_name,
                )
            return TrivyAgentBatchOutcome(
                batch_id=request.request_id,
                result=_to_domain_result(result),
                partial=result.partial,
            )

        def failed_batch(
            request: AgentRequest[ProviderRecommendationBatch],
            error: Exception,
            _context: WorkflowContext,
        ) -> TrivyAgentBatchOutcome:
            code = str(getattr(error, "code", "provider_execution"))
            return TrivyAgentBatchOutcome(
                batch_id=request.request_id,
                error_code=code,
                error_message=f"{runtime.provider_name} 未返回可用的结构化结果",
                partial=True,
            )

        def merge(payload: tuple[Any, ...], _context: WorkflowContext) -> TrivyRenderContext:
            parsed, outcomes = payload
            if not isinstance(parsed, ParsedTrivyRun):
                raise TypeError("Trivy merge requires ParsedTrivyRun")
            if not isinstance(outcomes, tuple) or any(
                not isinstance(item, TrivyAgentBatchOutcome) for item in outcomes
            ):
                raise TypeError("Trivy merge requires ordered batch outcomes")
            analysis = finalize_batch_outcomes(
                parsed,
                outcomes,
                provider=runtime.provider_name,
                requested_model=runtime.model,
                skill_name=runtime.skill_name,
            )
            return TrivyRenderContext(parsed=parsed, analysis=analysis)

        def render(value: TrivyRenderContext, _context: WorkflowContext) -> RenderedTrivyReport:
            return render_report(value.parsed, value.analysis)

        retry_policy = RetryPolicy(
            attempt_timeout_seconds=runtime.attempt_timeout_seconds,
        )
        return Workflow(
            input_type=TrivyWorkflowInput,
            nodes=(
                TransformNode(
                    id="parse",
                    input_type=TrivyWorkflowInput,
                    output_type=ParsedTrivyRun,
                    handler=lambda value, _context: parse_workflow_input(value),
                ),
                TransformNode(
                    id="plan",
                    depends_on=("parse",),
                    input_type=ParsedTrivyRun,
                    output_type=AgentRequest,
                    output_many=True,
                    handler=lambda value, _context: plan_agent_requests(value),
                ),
                AgentNode(
                    id="analyze",
                    depends_on=("plan",),
                    input_type=AgentRequest,
                    output_type=TrivyAgentBatchOutcome,
                    handler=analyze_one,
                    fallback_handler=failed_batch,
                    max_concurrency=runtime.max_agent_concurrency,
                    retry_policy=retry_policy,
                ),
                TransformNode(
                    id="merge",
                    depends_on=("parse", "analyze"),
                    input_type=tuple,
                    output_type=TrivyRenderContext,
                    handler=merge,
                ),
                TransformNode(
                    id="render",
                    depends_on=("merge",),
                    input_type=TrivyRenderContext,
                    output_type=RenderedTrivyReport,
                    handler=render,
                ),
            ),
        )


def create_plugin() -> TrivyPlugin:
    """Entry-point factory; registry calls it once per run."""

    return TrivyPlugin()


__all__ = [
    "PLUGIN_API_VERSION",
    "PLUGIN_ID",
    "PluginRuntime",
    "TrivyPlugin",
    "bundled_skill_path",
    "create_plugin",
    "load_bundled_skill",
    "plan_agent_requests",
]
