"""Tiny standalone plugin proving that agent-core has no Trivy assumptions."""

from __future__ import annotations

import json
from typing import Literal, Protocol

from pydantic import Field, field_validator

from agent_core.contracts import (
    AgentRequest,
    ArtifactInput,
    ArtifactOutput,
    StrictFrozenModel,
    ToolPolicy,
)
from agent_core.providers import ProviderAdapter
from agent_core.registry import CORE_API_VERSION, PluginManifest
from agent_core.workflow import AgentNode, TransformNode, Workflow, WorkflowContext

PLUGIN_ID = "toy"
SYSTEM_PROMPT = (
    "TOY_SENTINEL_SYSTEM_V1: reverse the supplied text and return only the "
    "declared ToyAgentAnswer schema."
)


class ToyOptions(StrictFrozenModel):
    """Public options schema exposed by the plugin registry."""

    preserve_case: bool = True
    web_access: bool = False


class ToyInput(ArtifactInput[ToyOptions]):
    """Raw JSON artifact accepted by the generic composition runtime."""


class ToyDocument(StrictFrozenModel):
    """Deterministically parsed phrases owned by this domain plugin."""

    phrases: tuple[str, ...] = Field(min_length=1)
    options: ToyOptions

    @field_validator("phrases")
    @classmethod
    def validate_phrases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not phrase for phrase in value):
            raise ValueError("toy phrases must be non-empty")
        return value


class ToyAgentAnswer(StrictFrozenModel):
    """Distinctive structured response requested from the provider."""

    original_text: str
    reversed_text: str
    character_count: int = Field(ge=0)
    schema_sentinel: Literal["TOY_RESPONSE_SCHEMA_V1"]


class ToyArtifact(ArtifactOutput):
    """Final deterministic artifact assembled from ordered provider answers."""

    media_type: Literal["application/vnd.agent-core.toy+json"] = (
        "application/vnd.agent-core.toy+json"
    )
    format_sentinel: Literal["TOY_ARTIFACT_V1"] = "TOY_ARTIFACT_V1"
    filename: str = "toy-result.json"


class ToyRuntime(Protocol):
    """Only the provider boundary is required from the composition root."""

    provider: ProviderAdapter


def parse_input(value: ToyInput) -> ToyDocument:
    """Parse and validate untrusted JSON before any Agent call is planned."""

    try:
        decoded = json.loads(value.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("toy input must be UTF-8 JSON") from exc
    if not isinstance(decoded, dict) or set(decoded) != {"phrases"}:
        raise ValueError("toy input must contain exactly the phrases field")
    phrases = decoded["phrases"]
    if not isinstance(phrases, list) or any(not isinstance(item, str) for item in phrases):
        raise ValueError("toy phrases must be a JSON string array")
    normalized = tuple(
        phrases if value.options.preserve_case else [item.lower() for item in phrases]
    )
    return ToyDocument(phrases=normalized, options=value.options)


def plan_requests(value: ToyDocument) -> tuple[AgentRequest[ToyAgentAnswer], ...]:
    """Build one typed request per phrase without any provider-specific types."""

    return tuple(
        AgentRequest[ToyAgentAnswer](
            request_id=f"toy-{index}",
            system_prompt=SYSTEM_PROMPT,
            prompt=(
                "TOY_SENTINEL_PROMPT_V1\n"
                "Reverse exactly this JSON string and count its characters: "
                f"{json.dumps(phrase, ensure_ascii=False)}"
            ),
            response_model=ToyAgentAnswer,
            tool_policy=ToolPolicy(web_access=value.options.web_access),
            metadata={"domain": PLUGIN_ID, "toy_text": phrase},
        )
        for index, phrase in enumerate(value.phrases)
    )


class ToyPlugin:
    """Stateless plugin whose factory is safe to call once per framework run."""

    plugin_id = PLUGIN_ID
    api_version = CORE_API_VERSION
    manifest = PluginManifest(
        plugin_id=PLUGIN_ID,
        api_version=CORE_API_VERSION,
        version="0.1.0",
        display_name="Toy Text Reverser",
        input_model=ToyInput,
        options_model=ToyOptions,
        output_model=ToyArtifact,
        required_capabilities=("structured_output",),
    )

    def create_workflow(self, runtime: ToyRuntime) -> Workflow:
        async def invoke_agent(
            request: AgentRequest[ToyAgentAnswer],
            context: WorkflowContext,
        ) -> ToyAgentAnswer:
            result = await runtime.provider.execute(
                request,
                timeout_seconds=context.attempt_timeout_seconds,
            )
            return result.output

        def render_artifact(
            answers: tuple[ToyAgentAnswer, ...],
            _context: WorkflowContext,
        ) -> ToyArtifact:
            payload = [answer.model_dump(mode="json") for answer in answers]
            return ToyArtifact(
                content=json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(
                    "utf-8"
                ),
            )

        return Workflow(
            input_type=ToyInput,
            nodes=(
                TransformNode(
                    id="toy_parse",
                    input_type=ToyInput,
                    output_type=ToyDocument,
                    handler=lambda value, _context: parse_input(value),
                ),
                TransformNode(
                    id="toy_plan",
                    depends_on=("toy_parse",),
                    input_type=ToyDocument,
                    output_type=AgentRequest,
                    output_many=True,
                    handler=lambda value, _context: plan_requests(value),
                ),
                AgentNode(
                    id="toy_agent",
                    depends_on=("toy_plan",),
                    input_type=AgentRequest,
                    output_type=ToyAgentAnswer,
                    handler=invoke_agent,
                ),
                TransformNode(
                    id="toy_render",
                    depends_on=("toy_agent",),
                    input_type=ToyAgentAnswer,
                    input_many=True,
                    output_type=ToyArtifact,
                    handler=render_artifact,
                ),
            ),
        )


def create_plugin() -> ToyPlugin:
    """Standard entry-point factory returning a fresh plugin instance."""

    return ToyPlugin()


__all__ = [
    "PLUGIN_ID",
    "SYSTEM_PROMPT",
    "ToyAgentAnswer",
    "ToyArtifact",
    "ToyDocument",
    "ToyInput",
    "ToyOptions",
    "ToyPlugin",
    "ToyRuntime",
    "create_plugin",
    "parse_input",
    "plan_requests",
]
