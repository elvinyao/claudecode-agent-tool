from __future__ import annotations

import asyncio
import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_core.audit import AuditEventType, AuditLogger, InMemoryAuditSink
from agent_core.contracts import (
    ActionMode,
    AgentRequest,
    ArtifactInput,
    ArtifactOutput,
    ProviderResult,
    RunStatus,
    StrictFrozenModel,
)
from agent_core.progress import ProgressEventType, WorkflowProgress
from agent_core.providers import ProviderCapabilities, ProviderRegistry
from agent_core.registry import PluginManifest, PluginRegistry
from agent_core.runtime import (
    AgentRuntime,
    RuntimeConfigurationError,
    RuntimeContractError,
    RuntimeInputError,
)
from agent_core.workflow import ActionNode, NodeExecutionError, TransformNode, Workflow

ROOT = Path(__file__).resolve().parents[2]
TOY_SOURCE = ROOT / "tests" / "fixtures" / "toy-plugin" / "src"


@dataclass(slots=True)
class FakeProvider:
    name: str = "fake"
    model: str = "fake-model"
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    requests: list[AgentRequest[Any]] = field(default_factory=list)

    async def execute(
        self,
        request: AgentRequest[Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ProviderResult[Any]:
        del timeout_seconds
        self.requests.append(request)
        text = request.metadata["toy_text"]
        return ProviderResult(
            request_id=request.request_id,
            provider=self.name,
            model=self.model,
            output=request.response_model(
                original_text=text,
                reversed_text=text[::-1],
                character_count=len(text),
                schema_sentinel="TOY_RESPONSE_SCHEMA_V1",
            ),
        )


class TypedArtifactOptions(StrictFrozenModel):
    pass


class TypedArtifactInput(ArtifactInput[TypedArtifactOptions]):
    pass


class TypedArtifactDocument(StrictFrozenModel):
    count: int
    label: str


class TypedArtifactOutput(ArtifactOutput):
    filename: str = "typed-artifact.json"


class TypedArtifactPlugin:
    plugin_id = "typed_artifact"
    api_version = "1.0"
    manifest = PluginManifest(
        plugin_id=plugin_id,
        api_version=api_version,
        version="1.0.0",
        display_name="Typed artifact contract fixture",
        input_model=TypedArtifactInput,
        options_model=TypedArtifactOptions,
        output_model=TypedArtifactOutput,
        artifact_content_model=TypedArtifactDocument,
    )

    def __init__(self, *, content: bytes, media_type: str) -> None:
        self.content = content
        self.media_type = media_type

    def create_workflow(self, runtime: Any) -> Workflow:
        return Workflow(
            input_type=TypedArtifactInput,
            nodes=(
                TransformNode(
                    id="render",
                    input_type=TypedArtifactInput,
                    output_type=TypedArtifactOutput,
                    handler=lambda _value, _context: TypedArtifactOutput(
                        content=self.content,
                        media_type=self.media_type,
                    ),
                ),
            ),
        )


def typed_artifact_runtime(
    *,
    content: bytes,
    media_type: str = "application/json",
    audit_logger: AuditLogger | None = None,
) -> AgentRuntime:
    plugin_registry = PluginRegistry()
    plugin_registry.register(
        "typed_artifact",
        lambda: TypedArtifactPlugin(content=content, media_type=media_type),
    )
    provider_registry = ProviderRegistry()
    provider_registry.register("fake", lambda **_kwargs: FakeProvider())
    return AgentRuntime(
        plugin_registry,
        provider_registry=provider_registry,
        audit_logger=audit_logger,
    )


async def run_typed_artifact(runtime: AgentRuntime, *, run_id: str) -> Any:
    return await runtime.run(
        plugin_id="typed_artifact",
        provider="fake",
        input_bytes=b"source",
        options={},
        run_id=run_id,
    )


def runtime_fixture(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, AgentRuntime, list[Any], Any]:
    monkeypatch.syspath_prepend(str(TOY_SOURCE))
    toy = importlib.import_module("toy_agent_plugin.plugin")
    plugin_registry = PluginRegistry()
    plugin_registry.register("toy", toy.create_plugin)
    providers: list[FakeProvider] = []
    provider_registry = ProviderRegistry()

    def provider_factory(*, model: str | None, skills: tuple[Any, ...]) -> FakeProvider:
        assert skills == ()
        provider = FakeProvider(model=model or "fake-model")
        providers.append(provider)
        return provider

    provider_registry.register("fake", provider_factory)
    sink = InMemoryAuditSink()
    runtime = AgentRuntime(
        plugin_registry,
        provider_registry=provider_registry,
        audit_logger=AuditLogger(sink),
        default_deadline_seconds=5,
        attempt_timeout_seconds=1,
    )
    return toy, runtime, providers, sink


@pytest.mark.asyncio
async def test_runtime_executes_raw_artifact_through_toy_plugin_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toy, runtime, providers, sink = runtime_fixture(monkeypatch)
    raw = json.dumps({"phrases": ["Codex", "通用框架"]}).encode("utf-8")
    progress: list[WorkflowProgress] = []

    async def collect(event: WorkflowProgress) -> None:
        progress.append(event)

    result = await runtime.run(
        plugin_id="toy",
        provider="fake",
        input_bytes=raw,
        input_filename="phrases.json",
        options={"preserve_case": True},
        model="fake-v1",
        run_id="runtime-success",
        progress_sink=collect,
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.plugin_id == "toy"
    assert result.artifact.filename == "toy-result.json"
    assert [request.metadata["toy_text"] for request in providers[0].requests] == [
        "Codex",
        "通用框架",
    ]
    assert all(request.response_model is toy.ToyAgentAnswer for request in providers[0].requests)
    records = sink.records
    assert records[0].event_type is AuditEventType.RUN_STARTED
    assert records[-1].event_type is AuditEventType.RUN_COMPLETED
    assert records[-1].status is RunStatus.SUCCEEDED
    assert sum(item.event_type is AuditEventType.PROVIDER_REQUESTED for item in records) == 2
    assert sum(item.event_type is AuditEventType.NODE_STARTED for item in records) == 4
    assert sum(item.event_type is AuditEventType.NODE_COMPLETED for item in records) == 4
    assert progress[0].type is ProgressEventType.STEP_STARTED
    assert any(item.type is ProgressEventType.AGENT_BATCH_STARTED for item in progress)
    assert progress[-1].type is ProgressEventType.STEP_COMPLETED
    persisted_shape = "\n".join(item.model_dump_json() for item in records)
    assert "通用框架" not in persisted_shape
    assert "TOY_SENTINEL_PROMPT" not in persisted_shape


@pytest.mark.asyncio
async def test_runtime_accepts_strict_typed_json_artifact() -> None:
    content = b'{"count":2,"label":"safe"}'
    runtime = typed_artifact_runtime(
        content=content,
        media_type="application/vnd.example.result+json; charset=utf-8",
    )

    result = await run_typed_artifact(runtime, run_id="typed-artifact-valid")

    assert result.artifact.content == content
    assert result.status is RunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_runtime_rejects_coercible_json_for_strict_artifact_schema() -> None:
    secret = "artifact-secret-value"
    runtime = typed_artifact_runtime(
        content=json.dumps({"count": "2", "label": secret}).encode(),
    )

    with pytest.raises(RuntimeContractError) as captured:
        await run_typed_artifact(runtime, run_id="typed-artifact-invalid")

    assert str(captured.value) == (
        "plugin artifact content does not satisfy its declared typed schema"
    )
    assert secret not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.asyncio
async def test_runtime_rejects_malformed_json_without_retaining_content() -> None:
    secret = "malformed-artifact-secret"
    runtime = typed_artifact_runtime(
        content=f'{{"count":2,"label":"{secret}"'.encode(),
    )

    with pytest.raises(RuntimeContractError) as captured:
        await run_typed_artifact(runtime, run_id="typed-artifact-malformed")

    assert secret not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.asyncio
async def test_runtime_rejects_typed_artifact_with_non_json_media_type() -> None:
    runtime = typed_artifact_runtime(
        content=b'{"count":2,"label":"safe"}',
        media_type="text/plain",
    )

    with pytest.raises(
        RuntimeContractError,
        match="artifact_content_model requires a JSON artifact media type",
    ):
        await run_typed_artifact(runtime, run_id="typed-artifact-media-type")


@pytest.mark.asyncio
async def test_runtime_delivers_progress_before_failing_closed_on_audit_error() -> None:
    class ExpectedAuditError(RuntimeError):
        pass

    class FailingNodeAuditSink:
        def emit(self, record: Any) -> None:
            if record.event_type is AuditEventType.NODE_STARTED:
                raise ExpectedAuditError("audit unavailable")

    runtime = typed_artifact_runtime(
        content=b'{"count":2,"label":"safe"}',
        audit_logger=AuditLogger(FailingNodeAuditSink()),
    )
    progress: list[WorkflowProgress] = []

    async def collect(event: WorkflowProgress) -> None:
        progress.append(event)

    with pytest.raises(ExpectedAuditError, match="audit unavailable"):
        await runtime.run(
            plugin_id="typed_artifact",
            provider="fake",
            input_bytes=b"source",
            options={},
            run_id="typed-artifact-audit-failure",
            progress_sink=collect,
        )

    assert [event.type for event in progress] == [ProgressEventType.STEP_STARTED]
    assert progress[0].node_id == "render"


@pytest.mark.asyncio
async def test_runtime_strict_options_failure_is_audited_without_calling_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _toy, runtime, providers, sink = runtime_fixture(monkeypatch)

    with pytest.raises(RuntimeInputError, match="invalid plugin options"):
        await runtime.run(
            plugin_id="toy",
            provider="fake",
            input_bytes=b'{"phrases":["x"]}',
            options={"preserve_case": "yes"},
            run_id="runtime-invalid",
        )

    assert providers == []
    assert sink.records[-1].event_type is AuditEventType.RUN_COMPLETED
    assert sink.records[-1].status is RunStatus.FAILED


@pytest.mark.asyncio
async def test_runtime_run_policy_blocks_plugin_web_request_before_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _toy, runtime, providers, sink = runtime_fixture(monkeypatch)

    with pytest.raises(NodeExecutionError, match="web access is disabled"):
        await runtime.run(
            plugin_id="toy",
            provider="fake",
            input_bytes=b'{"phrases":["offline"]}',
            options={"preserve_case": True, "web_access": True},
            allow_web_access=False,
            run_id="runtime-web-blocked",
        )

    # The Toy AgentNode has no fallback, so the provider-policy error becomes
    # a workflow failure and never reaches the delegated provider.
    assert providers[0].requests == []
    assert sink.records[-1].status is RunStatus.FAILED


@pytest.mark.asyncio
async def test_runtime_keeps_one_artifact_alongside_gated_terminal_action() -> None:
    class Options(StrictFrozenModel):
        pass

    class Input(ArtifactInput[Options]):
        pass

    class Output(ArtifactOutput):
        filename: str = "action-result.txt"

    class Receipt(StrictFrozenModel):
        applied: bool

    applied: list[bytes] = []

    class Plugin:
        plugin_id = "action_demo"
        api_version = "1.0"
        manifest = PluginManifest(
            plugin_id=plugin_id,
            api_version=api_version,
            version="1.0.0",
            display_name="Action demo",
            input_model=Input,
            options_model=Options,
            output_model=Output,
        )

        def create_workflow(self, runtime: Any) -> Workflow:
            def apply(value: Input, _context: Any) -> Receipt:
                applied.append(value.content)
                return Receipt(applied=True)

            return Workflow(
                input_type=Input,
                nodes=(
                    TransformNode(
                        id="render",
                        input_type=Input,
                        output_type=Output,
                        handler=lambda value, _context: Output(content=value.content),
                    ),
                    ActionNode(
                        id="publish",
                        input_type=Input,
                        output_type=Receipt,
                        apply_handler=apply,
                    ),
                ),
            )

    plugin_registry = PluginRegistry()
    plugin_registry.register("action_demo", Plugin)
    provider_registry = ProviderRegistry()
    provider_registry.register("fake", lambda **_kwargs: FakeProvider())
    sink = InMemoryAuditSink()
    runtime = AgentRuntime(
        plugin_registry,
        provider_registry=provider_registry,
        audit_logger=AuditLogger(sink),
    )

    disabled = await runtime.run(
        plugin_id="action_demo",
        provider="fake",
        input_bytes=b"safe",
        options={},
        action_mode=ActionMode.DISABLED,
        run_id="action-disabled",
    )
    enabled = await runtime.run(
        plugin_id="action_demo",
        provider="fake",
        input_bytes=b"apply",
        options={},
        action_mode=ActionMode.APPLY,
        run_id="action-apply",
    )

    assert disabled.artifact.content == b"safe"
    assert enabled.artifact.content == b"apply"
    assert applied == [b"apply"]
    decisions = [item for item in sink.records if item.event_type is AuditEventType.ACTION_DECIDED]
    assert [item.metadata_dict["action.executed"] for item in decisions] == [
        False,
        True,
    ]


@pytest.mark.asyncio
async def test_runtime_cancel_event_cancels_provider_and_releases_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(TOY_SOURCE))
    toy = importlib.import_module("toy_agent_plugin.plugin")
    plugin_registry = PluginRegistry()
    plugin_registry.register("toy", toy.create_plugin)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingProvider(FakeProvider):
        async def execute(self, request, *, timeout_seconds=None):
            del request, timeout_seconds
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    provider_registry = ProviderRegistry()
    provider_registry.register(
        "fake",
        lambda **_kwargs: BlockingProvider(),
    )
    sink = InMemoryAuditSink()
    runtime = AgentRuntime(
        plugin_registry,
        provider_registry=provider_registry,
        audit_logger=AuditLogger(sink),
        default_deadline_seconds=5,
    )
    cancel_event = asyncio.Event()
    task = asyncio.create_task(
        runtime.run(
            plugin_id="toy",
            provider="fake",
            input_bytes=b'{"phrases":["wait"]}',
            options={"preserve_case": True},
            cancel_event=cancel_event,
            run_id="runtime-cancel",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    cancel_event.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert cancelled.is_set()
    assert sink.records[-1].event_type is AuditEventType.RUN_COMPLETED
    assert sink.records[-1].status is RunStatus.CANCELLED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"plugin_id": ""}, "plugin_id"),
        ({"provider": None}, "provider"),
        ({"input_filename": 7}, "input_filename"),
        ({"model": 7}, "model"),
        ({"allow_web_access": "yes"}, "allow_web_access"),
        ({"run_id": 7}, "run_id"),
        ({"metadata": []}, "metadata"),
        ({"cancel_event": object()}, "cancel_event"),
    ],
)
async def test_runtime_rejects_malformed_public_arguments(
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, Any],
    message: str,
) -> None:
    _toy, runtime, _providers, _sink = runtime_fixture(monkeypatch)
    values: dict[str, Any] = {
        "plugin_id": "toy",
        "provider": "fake",
        "input_bytes": b'{"phrases":["safe"]}',
        "options": {"preserve_case": True},
    }
    values.update(updates)

    with pytest.raises((RuntimeConfigurationError, RuntimeInputError), match=message):
        await runtime.run(**values)
