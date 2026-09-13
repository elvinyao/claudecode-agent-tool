"""Ankify domain plugin composition and provider request planning."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from agent_core.contracts import AgentRequest, ProviderResult, ToolPolicy
from agent_core.ownership import (
    FieldOwnership,
    OwnershipContract,
    OwnershipField,
    OwnershipSurface,
)
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
    AnkifyResultDocument,
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

_OWNERSHIP = OwnershipContract(
    fields=(
        OwnershipField(
            surface=OwnershipSurface.INPUT,
            path="/content",
            ownership=FieldOwnership.USER_CHOICE,
            label="学习材料",
            description="用户选择并提交的源材料；运行开始后作为不可变输入快照。",
        ),
        OwnershipField(
            surface=OwnershipSurface.INPUT,
            path="/filename",
            ownership=FieldOwnership.USER_CHOICE,
            label="材料名称",
            description="用户提交材料时提供的安全文件名。",
        ),
        *(
            OwnershipField(
                surface=OwnershipSurface.OPTIONS,
                path=f"/{name}",
                ownership=FieldOwnership.USER_CHOICE,
                label=label,
                description="用户在运行前明确选择的制卡参数。",
            )
            for name, label in (
                ("study_purpose", "学习目的"),
                ("guide_profile", "指导配置"),
                ("strategy_version", "策略版本"),
                ("source_mode", "材料模式"),
                ("deck_name", "牌组名称"),
                ("requested_card_count", "目标卡片数"),
                ("batch_size", "批次大小"),
                ("language", "输出语言"),
                ("user_tags", "用户标签"),
                ("use_bundled_skill", "内置技能"),
                ("exam_subject", "中学受験科目"),
                ("exam_stage", "中学受験阶段"),
                ("learning_language", "学习语言"),
                ("native_language", "母语"),
                ("target_exam", "目标考试"),
                ("language_task", "语言任务"),
                ("translation_direction", "翻译方向"),
                ("max_front_length", "正面长度上限"),
                ("max_back_length", "背面长度上限"),
            )
        ),
        *(
            OwnershipField(
                surface=OwnershipSurface.ARTIFACT_CONTENT,
                path=f"/{name}",
                ownership=FieldOwnership.PROGRAM_FACT,
                label=label,
                description="由确定性解析、校验或渲染逻辑产生并锁定。",
            )
            for name, label in (
                ("schema_version", "结果 schema 版本"),
                ("plugin", "插件标识"),
                ("plugin_version", "插件版本"),
                ("strategy_profile", "已解析策略"),
                ("strategy_version", "已解析策略版本"),
                ("document_id", "材料稳定 ID"),
                ("status", "结果状态"),
                ("partial", "部分结果标记"),
                ("rejections", "拒绝记录"),
                ("warnings", "运行警告"),
            )
        ),
        OwnershipField(
            surface=OwnershipSurface.ARTIFACT_CONTENT,
            path="/deck_name",
            ownership=FieldOwnership.USER_CHOICE,
            label="牌组名称",
            description="保留用户确认的牌组名称。",
        ),
        OwnershipField(
            surface=OwnershipSurface.ARTIFACT_CONTENT,
            path="/source_name",
            ownership=FieldOwnership.USER_CHOICE,
            label="材料名称",
            description="保留用户提交的安全材料名称。",
        ),
        OwnershipField(
            surface=OwnershipSurface.ARTIFACT_CONTENT,
            path="/cards/-/note_type",
            ownership=FieldOwnership.POLICY_LOCKED,
            label="卡片类型",
            description="Ankify v1 策略固定为 Basic，用户和 AI 均不能覆盖。",
        ),
        *(
            OwnershipField(
                surface=OwnershipSurface.ARTIFACT_CONTENT,
                path=f"/cards/-/{name}",
                ownership=FieldOwnership.AI_CANDIDATE,
                label=label,
                description="AI 提议的候选值；写入前必须通过 schema 和确定性规则。",
            )
            for name, label in (
                ("front", "卡片正面"),
                ("back", "卡片背面"),
                ("provenance", "来源类型"),
                ("source_block_ids", "来源块引用"),
                ("evidence_quotes", "证据摘录"),
                ("learning_objective", "学习目标"),
                ("confidence", "候选置信度"),
            )
        ),
        OwnershipField(
            surface=OwnershipSurface.ARTIFACT_CONTENT,
            path="/cards/-/tags",
            ownership=FieldOwnership.AI_CANDIDATE,
            label="合并标签",
            description=("包含用户/策略标签与 AI 建议标签；最终集合由确定性规则去重并校验。"),
        ),
        *(
            OwnershipField(
                surface=OwnershipSurface.ARTIFACT_CONTENT,
                path=f"/cards/-/{name}",
                ownership=FieldOwnership.PROGRAM_FACT,
                label=label,
                description="由确定性规则计算、合并或验证。",
            )
            for name, label in (
                ("note_id", "稳定卡片 ID"),
                ("quality_issues", "质量问题"),
            )
        ),
    )
)


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
        artifact_content_model=AnkifyResultDocument,
        ownership=_OWNERSHIP,
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
                block_id for card in result.output.cards for block_id in card.source_block_ids
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
