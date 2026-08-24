"""Ankify domain plugin composition and provider request planning."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from agent_core.contracts import AgentRequest, ProviderResult, ToolPolicy
from agent_core.providers import ProviderAdapter
from agent_core.providers.errors import ProviderResponseError
from agent_core.registry import PluginManifest
from agent_core.skills import SkillSpec, load_skill
from agent_core.workflow import AgentNode, RetryPolicy, TransformNode, Workflow, WorkflowContext
from ankify.domain import merge_batch_outcomes, plan_batches
from ankify.models import (
    AnkifyAgentBatchOutcome,
    AnkifyArtifact,
    AnkifyRenderContext,
    AnkifyRunOptions,
    AnkifyWorkflowInput,
    ParsedAnkifyRun,
    ProviderCardBatch,
)
from ankify.prompts import SYSTEM_PROMPT, build_generation_prompt
from ankify.renderer import render_artifact
from ankify.source import parse_workflow_input

PLUGIN_ID = "ankify"
PLUGIN_API_VERSION = "1.0"
PLUGIN_VERSION = "0.1.0"


class PluginRuntime(Protocol):
    provider: ProviderAdapter
    provider_name: str
    model: str | None
    skill_name: str | None
    attempt_timeout_seconds: float | None
    max_agent_concurrency: int


def bundled_skill_path() -> Path:
    return Path(__file__).resolve().parent / "bundled_skills" / "ankify-authoring" / "SKILL.md"


def load_bundled_skill() -> SkillSpec:
    return load_skill(bundled_skill_path())


def plan_agent_requests(
    parsed: ParsedAnkifyRun,
) -> tuple[AgentRequest[ProviderCardBatch], ...]:
    return tuple(
        AgentRequest[ProviderCardBatch](
            request_id=batch.batch_id,
            system_prompt=SYSTEM_PROMPT,
            prompt=build_generation_prompt(parsed, batch),
            response_model=ProviderCardBatch,
            tool_policy=ToolPolicy(web_access=False),
            metadata={
                "domain": PLUGIN_ID,
                "batch_id": batch.batch_id,
                "source_block_ids": tuple(block.block_id for block in batch.blocks),
                "strategy_profile": parsed.strategy.profile.value,
                "strategy_version": parsed.strategy.version,
                "target_card_count": batch.target_card_count,
            },
        )
        for batch in plan_batches(parsed)
    )


class AnkifyPlugin:
    """Stateless plugin; source, candidates, and results remain in node values."""

    plugin_id = PLUGIN_ID
    api_version = PLUGIN_API_VERSION
    manifest = PluginManifest(
        plugin_id=PLUGIN_ID,
        api_version=PLUGIN_API_VERSION,
        version=PLUGIN_VERSION,
        display_name="Ankify Evidence-Grounded Card Generator",
        input_model=AnkifyWorkflowInput,
        options_model=AnkifyRunOptions,
        output_model=AnkifyArtifact,
        required_capabilities=("structured_output",),
    )

    def skills_for_options(self, options: AnkifyRunOptions) -> tuple[SkillSpec, ...]:
        return (load_bundled_skill(),) if options.use_bundled_skill else ()

    def create_workflow(self, runtime: PluginRuntime) -> Workflow:
        async def generate_one(
            request: AgentRequest[ProviderCardBatch],
            _context: WorkflowContext,
        ) -> AnkifyAgentBatchOutcome:
            result: ProviderResult[ProviderCardBatch] = await runtime.provider.execute(request)
            allowed_ids = set(request.metadata["source_block_ids"])
            returned_ids = {
                block_id
                for card in result.output.cards
                for block_id in card.source_block_ids
            }
            if returned_ids - allowed_ids:
                raise ProviderResponseError(
                    "Ankify provider output contains a cross-batch source_block_id",
                    provider=runtime.provider_name,
                )
            return AnkifyAgentBatchOutcome(
                batch_id=request.request_id,
                result=result,
                partial=result.partial,
            )

        def failed_batch(
            request: AgentRequest[ProviderCardBatch],
            error: Exception,
            _context: WorkflowContext,
        ) -> AnkifyAgentBatchOutcome:
            return AnkifyAgentBatchOutcome(
                batch_id=request.request_id,
                error_code=str(getattr(error, "code", "provider_execution")),
                error_message=f"{runtime.provider_name} did not return usable card candidates",
                partial=True,
            )

        def merge(payload: tuple[Any, ...], _context: WorkflowContext) -> AnkifyRenderContext:
            parsed, outcomes = payload
            if not isinstance(parsed, ParsedAnkifyRun):
                raise TypeError("Ankify merge requires ParsedAnkifyRun")
            if not isinstance(outcomes, tuple) or any(
                not isinstance(item, AnkifyAgentBatchOutcome) for item in outcomes
            ):
                raise TypeError("Ankify merge requires ordered batch outcomes")
            return merge_batch_outcomes(parsed, outcomes)

        retry_policy = RetryPolicy(
            attempt_timeout_seconds=runtime.attempt_timeout_seconds,
        )
        return Workflow(
            input_type=AnkifyWorkflowInput,
            nodes=(
                TransformNode(
                    id="parse",
                    input_type=AnkifyWorkflowInput,
                    output_type=ParsedAnkifyRun,
                    handler=lambda value, _context: parse_workflow_input(value),
                ),
                TransformNode(
                    id="plan",
                    depends_on=("parse",),
                    input_type=ParsedAnkifyRun,
                    output_type=AgentRequest,
                    output_many=True,
                    handler=lambda value, _context: plan_agent_requests(value),
                ),
                AgentNode(
                    id="generate",
                    depends_on=("plan",),
                    input_type=AgentRequest,
                    output_type=AnkifyAgentBatchOutcome,
                    handler=generate_one,
                    fallback_handler=failed_batch,
                    max_concurrency=runtime.max_agent_concurrency,
                    retry_policy=retry_policy,
                ),
                TransformNode(
                    id="merge",
                    depends_on=("parse", "generate"),
                    input_type=tuple,
                    output_type=AnkifyRenderContext,
                    handler=merge,
                ),
                TransformNode(
                    id="render",
                    depends_on=("merge",),
                    input_type=AnkifyRenderContext,
                    output_type=AnkifyArtifact,
                    handler=lambda value, _context: render_artifact(value),
                ),
            ),
        )


def create_plugin() -> AnkifyPlugin:
    return AnkifyPlugin()


__all__ = [
    "PLUGIN_API_VERSION",
    "PLUGIN_ID",
    "PLUGIN_VERSION",
    "AnkifyPlugin",
    "PluginRuntime",
    "bundled_skill_path",
    "create_plugin",
    "load_bundled_skill",
    "plan_agent_requests",
]
