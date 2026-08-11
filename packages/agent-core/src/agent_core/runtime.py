"""Generic composition runtime for one domain-plugin workflow run.

The runtime owns composition, not domain policy: it validates a plugin's public
models, creates fresh plugin/provider instances, supplies run-scoped services,
and maps the plugin's terminal artifact to a transport-neutral result.
"""

from __future__ import annotations

import asyncio
import math
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4
from weakref import WeakKeyDictionary

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from agent_core.audit import AuditEventType, AuditLogger
from agent_core.contracts import (
    ActionMode,
    AgentCoreError,
    AgentRequest,
    ArtifactOutput,
    ProviderResult,
    RunStatus,
    StrictFrozenModel,
)
from agent_core.providers import (
    DEFAULT_PROVIDER_REGISTRY,
    ProviderAdapter,
    ProviderCapabilityError,
    ProviderRegistry,
)
from agent_core.registry import DomainPlugin, PluginRegistry
from agent_core.skills import SkillSpec, load_skill
from agent_core.workflow import Workflow, WorkflowResult

OutputT = TypeVar("OutputT", bound=BaseModel)
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class AgentRuntimeError(AgentCoreError):
    """Base class for runtime composition and artifact failures."""


class RuntimeConfigurationError(AgentRuntimeError, ValueError):
    """A run asks for an invalid plugin, provider, option, or limit."""


class RuntimeInputError(AgentRuntimeError, ValueError):
    """Input bytes or plugin options do not satisfy the selected plugin."""


class RuntimeContractError(AgentRuntimeError, TypeError):
    """A trusted plugin violates its registered runtime contract."""


class RuntimeArtifact(StrictFrozenModel):
    """Transport-neutral file artifact produced by a domain plugin."""

    content: bytes
    media_type: str = Field(default="application/octet-stream", min_length=1, max_length=255)
    filename: str = Field(default="artifact.bin", min_length=1, max_length=255)

    @field_validator("media_type", "filename")
    @classmethod
    def validate_safe_text(cls, value: str) -> str:
        if value != value.strip() or "\r" in value or "\n" in value:
            raise ValueError("artifact metadata must be trimmed single-line text")
        return value

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("artifact filename must not contain a path")
        return value


class RuntimeResult(StrictFrozenModel):
    """Stable result returned to CLI, Web, and future transports."""

    run_id: str = Field(min_length=1, max_length=128)
    plugin_id: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    status: RunStatus
    artifact: RuntimeArtifact
    warnings: tuple[str, ...] = ()
    partial: bool = False

    @field_validator("plugin_id", "provider", "model")
    @classmethod
    def validate_trimmed_names(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("runtime names must be trimmed")
        return value

    @field_validator("warnings")
    @classmethod
    def validate_warnings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not warning or warning != warning.strip() for warning in value):
            raise ValueError("runtime warnings must be non-empty and trimmed")
        return value

    @model_validator(mode="after")
    def validate_terminal_state(self) -> RuntimeResult:
        if self.status not in {RunStatus.SUCCEEDED, RunStatus.DEGRADED}:
            raise ValueError("a runtime result must have a successful terminal status")
        if self.partial != (self.status is RunStatus.DEGRADED):
            raise ValueError("partial must match the degraded runtime status")
        return self


@dataclass(frozen=True, slots=True)
class PluginRuntimeContext:
    """Frozen, run-scoped services passed to ``DomainPlugin.create_workflow``."""

    provider: ProviderAdapter
    provider_name: str
    model: str | None
    skill_name: str | None
    attempt_timeout_seconds: float | None
    max_agent_concurrency: int

    def __post_init__(self) -> None:
        if not self.provider_name or self.provider_name != self.provider_name.strip():
            raise RuntimeConfigurationError("provider_name must be non-empty and trimmed")
        if self.model is not None and (not self.model or self.model != self.model.strip()):
            raise RuntimeConfigurationError("model must be non-empty and trimmed")
        if self.skill_name is not None and (
            not self.skill_name or self.skill_name != self.skill_name.strip()
        ):
            raise RuntimeConfigurationError("skill_name must be non-empty and trimmed")
        if self.attempt_timeout_seconds is not None and not _positive_finite(
            self.attempt_timeout_seconds
        ):
            raise RuntimeConfigurationError(
                "attempt_timeout_seconds must be finite and greater than zero"
            )
        if self.max_agent_concurrency < 1:
            raise RuntimeConfigurationError("max_agent_concurrency must be positive")


class _ProviderSemaphorePool:
    """One provider gate per event loop, shared by all runtime instances.

    Asyncio synchronization primitives are loop-bound under contention. Keeping
    one process-global pool partitioned by loop preserves the global-per-server
    invariant without retaining closed test or worker loops.
    """

    _guard = threading.Lock()
    _by_loop: WeakKeyDictionary[
        asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]
    ] = WeakKeyDictionary()

    @classmethod
    def get(cls, provider_name: str) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        with cls._guard:
            gates = cls._by_loop.setdefault(loop, {})
            return gates.setdefault(provider_name, asyncio.Semaphore(1))


class _SerializedProvider:
    """Provider proxy enforcing the runtime-wide limit and emitting safe audit events."""

    def __init__(
        self,
        delegate: ProviderAdapter,
        *,
        audit_logger: AuditLogger,
        run_id: str,
        allow_web_access: bool,
    ) -> None:
        self._delegate = delegate
        self._audit_logger = audit_logger
        self._run_id = run_id
        self._allow_web_access = allow_web_access
        self.name = delegate.name
        self.model = delegate.model
        self.capabilities = delegate.capabilities

    async def execute(
        self,
        request: AgentRequest[OutputT],
        *,
        timeout_seconds: float | None = None,
    ) -> ProviderResult[OutputT]:
        await _emit_audit(
            self._audit_logger,
            run_id=self._run_id,
            event_type=AuditEventType.PROVIDER_REQUESTED,
            subject_id=request.request_id,
            payload=f"{request.system_prompt}\0{request.prompt}",
            status=RunStatus.RUNNING,
            metadata={
                "provider.name": self.name,
                "provider.model": self.model,
                "provider.web_access": request.tool_policy.web_access,
            },
        )
        if request.tool_policy.web_access and not self._allow_web_access:
            error = ProviderCapabilityError(
                "web access is disabled by the run policy",
                provider=self.name,
            )
            await _emit_audit(
                self._audit_logger,
                run_id=self._run_id,
                event_type=AuditEventType.PROVIDER_COMPLETED,
                subject_id=request.request_id,
                status=RunStatus.FAILED,
                metadata={
                    "provider.name": self.name,
                    "provider.model": self.model,
                    "error.code": error.code,
                    "error.type": error.__class__.__name__,
                },
            )
            raise error
        gate = _ProviderSemaphorePool.get(self.name)
        try:
            async with gate:
                result = await self._delegate.execute(
                    request,
                    timeout_seconds=timeout_seconds,
                )
        except asyncio.CancelledError:
            await _emit_audit_best_effort(
                self._audit_logger,
                run_id=self._run_id,
                event_type=AuditEventType.PROVIDER_COMPLETED,
                subject_id=request.request_id,
                status=RunStatus.CANCELLED,
                metadata={"provider.name": self.name, "provider.model": self.model},
            )
            raise
        except Exception as exc:
            await _emit_audit_best_effort(
                self._audit_logger,
                run_id=self._run_id,
                event_type=AuditEventType.PROVIDER_COMPLETED,
                subject_id=request.request_id,
                status=RunStatus.FAILED,
                metadata={
                    "provider.name": self.name,
                    "provider.model": self.model,
                    "error.code": str(getattr(exc, "code", "provider_execution")),
                    "error.type": exc.__class__.__name__,
                },
            )
            raise
        await _emit_audit(
            self._audit_logger,
            run_id=self._run_id,
            event_type=AuditEventType.PROVIDER_COMPLETED,
            subject_id=request.request_id,
            payload=result.output,
            status=RunStatus.DEGRADED if result.partial else RunStatus.SUCCEEDED,
            metadata={
                "provider.name": result.provider,
                "provider.model": result.model,
                "provider.partial": result.partial,
                "provider.warning_count": len(result.warnings),
            },
        )
        return result


class AgentRuntime:
    """Compose and execute fresh plugin/provider instances for every run."""

    def __init__(
        self,
        plugin_registry: PluginRegistry,
        *,
        provider_registry: ProviderRegistry = DEFAULT_PROVIDER_REGISTRY,
        audit_logger: AuditLogger | None = None,
        default_deadline_seconds: float | None = 900.0,
        attempt_timeout_seconds: float | None = 300.0,
        max_agent_concurrency: int = 4,
    ) -> None:
        if default_deadline_seconds is not None and not _positive_finite(
            default_deadline_seconds
        ):
            raise RuntimeConfigurationError(
                "default_deadline_seconds must be finite and greater than zero"
            )
        if attempt_timeout_seconds is not None and not _positive_finite(
            attempt_timeout_seconds
        ):
            raise RuntimeConfigurationError(
                "attempt_timeout_seconds must be finite and greater than zero"
            )
        if max_agent_concurrency < 1:
            raise RuntimeConfigurationError("max_agent_concurrency must be positive")
        self.plugin_registry = plugin_registry
        self.provider_registry = provider_registry
        self.audit_logger = audit_logger or AuditLogger()
        self.default_deadline_seconds = default_deadline_seconds
        self.attempt_timeout_seconds = attempt_timeout_seconds
        self.max_agent_concurrency = max_agent_concurrency

    async def run(
        self,
        *,
        plugin_id: str,
        provider: str,
        input_bytes: bytes,
        input_filename: str = "input.bin",
        options: Mapping[str, Any] | None = None,
        model: str | None = None,
        skill: SkillSpec | str | Path | None = None,
        action_mode: ActionMode = ActionMode.DISABLED,
        allow_web_access: bool = True,
        deadline_seconds: float | None = None,
        cancel_event: asyncio.Event | None = None,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RuntimeResult:
        """Validate, compose, run, and normalize one plugin workflow.

        ``deadline_seconds=None`` uses the runtime default. Caller cancellation
        and an optional cancellation event both cancel the workflow task and,
        through the provider adapter, its underlying SDK operation.
        """

        if not isinstance(plugin_id, str) or not plugin_id:
            raise RuntimeConfigurationError("plugin_id must be a non-empty string")
        if not isinstance(provider, str) or not provider:
            raise RuntimeConfigurationError("provider must be a non-empty string")
        if not isinstance(input_bytes, bytes):
            raise RuntimeInputError("input_bytes must be bytes")
        if not isinstance(input_filename, str) or (
            not input_filename or input_filename != input_filename.strip()
        ):
            raise RuntimeInputError("input_filename must be non-empty and trimmed")
        if model is not None and (
            not isinstance(model, str) or not model or model != model.strip()
        ):
            raise RuntimeConfigurationError("model must be non-empty and trimmed")
        if not isinstance(action_mode, ActionMode):
            raise RuntimeConfigurationError("action_mode must be an ActionMode")
        if not isinstance(allow_web_access, bool):
            raise RuntimeConfigurationError("allow_web_access must be bool")
        effective_deadline = (
            self.default_deadline_seconds
            if deadline_seconds is None
            else deadline_seconds
        )
        if effective_deadline is not None and not _positive_finite(effective_deadline):
            raise RuntimeConfigurationError(
                "deadline_seconds must be finite and greater than zero"
            )
        if cancel_event is not None and not isinstance(cancel_event, asyncio.Event):
            raise RuntimeConfigurationError("cancel_event must be an asyncio.Event")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise RuntimeConfigurationError("metadata must be a mapping")
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError

        plugin = self.plugin_registry.create_for_run(plugin_id)
        manifest = plugin.manifest
        effective_run_id = _validated_run_id(run_id)
        base_audit_metadata = {
            "plugin.id": manifest.plugin_id,
            "plugin.version": manifest.version,
            "provider.name": provider.strip().lower(),
            "provider.model": model or "provider-default",
            "action.mode": action_mode.value,
            "input.filename": Path(input_filename).name,
        }
        await _emit_audit(
            self.audit_logger,
            run_id=effective_run_id,
            event_type=AuditEventType.RUN_STARTED,
            subject_id=manifest.plugin_id,
            payload=input_bytes,
            status=RunStatus.RUNNING,
            metadata=base_audit_metadata,
        )
        try:
            await _emit_audit(
                self.audit_logger,
                run_id=effective_run_id,
                event_type=AuditEventType.PLUGIN_CREATED,
                subject_id=manifest.plugin_id,
                status=RunStatus.RUNNING,
                metadata={
                    "plugin.api_version": manifest.api_version,
                    "plugin.version": manifest.version,
                },
            )
            validated_options = _validate_options(manifest.options_model, options or {})
            skills = _skills_for_run(plugin, validated_options, skill)
            adapter = self.provider_registry.create(provider, model=model, skills=skills)
            _require_capabilities(
                adapter,
                manifest.required_capabilities,
                has_skills=bool(skills),
            )
            serialized_provider = _SerializedProvider(
                adapter,
                audit_logger=self.audit_logger,
                run_id=effective_run_id,
                allow_web_access=allow_web_access,
            )
            context = PluginRuntimeContext(
                provider=serialized_provider,
                provider_name=adapter.name,
                model=model,
                skill_name=skills[0].name if skills else None,
                attempt_timeout_seconds=self.attempt_timeout_seconds,
                max_agent_concurrency=self.max_agent_concurrency,
            )
            workflow = _create_workflow(plugin, context)
            workflow_input = _validate_workflow_input(
                manifest.input_model,
                content=input_bytes,
                options=validated_options,
                filename=Path(input_filename).name,
            )
            workflow_metadata = {
                **({} if metadata is None else dict(metadata)),
                "plugin_id": manifest.plugin_id,
                "provider": adapter.name,
            }
            execution = workflow.execute(
                workflow_input,
                action_mode=action_mode,
                run_id=effective_run_id,
                metadata=workflow_metadata,
                deadline_seconds=effective_deadline,
                attempt_timeout_seconds=self.attempt_timeout_seconds,
            )
            workflow_result = await _await_workflow(execution, cancel_event=cancel_event)
            await _audit_workflow_nodes(
                self.audit_logger,
                workflow_result,
                action_mode=action_mode,
            )
            result = _normalize_result(
                workflow_result,
                output_model=manifest.output_model,
                plugin_id=manifest.plugin_id,
                provider_name=adapter.name,
                model=adapter.model,
            )
            await _emit_audit(
                self.audit_logger,
                run_id=effective_run_id,
                event_type=AuditEventType.RUN_COMPLETED,
                subject_id=manifest.plugin_id,
                payload=result.artifact.content,
                status=result.status,
                metadata={
                    **base_audit_metadata,
                    "run.partial": result.partial,
                    "run.warning_count": len(result.warnings),
                },
            )
            return result
        except asyncio.CancelledError:
            await _emit_audit_best_effort(
                self.audit_logger,
                run_id=effective_run_id,
                event_type=AuditEventType.RUN_COMPLETED,
                subject_id=manifest.plugin_id,
                status=RunStatus.CANCELLED,
                metadata=base_audit_metadata,
            )
            raise
        except Exception as exc:
            await _emit_audit_best_effort(
                self.audit_logger,
                run_id=effective_run_id,
                event_type=AuditEventType.RUN_COMPLETED,
                subject_id=manifest.plugin_id,
                status=RunStatus.FAILED,
                metadata={
                    **base_audit_metadata,
                    "error.code": str(getattr(exc, "code", "runtime_execution")),
                    "error.type": exc.__class__.__name__,
                },
            )
            raise


def _validated_run_id(value: str | None) -> str:
    run_id = value or uuid4().hex
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise RuntimeConfigurationError("run_id is not a valid framework identifier")
    return run_id


async def _emit_audit(logger: AuditLogger, **values: Any) -> None:
    await asyncio.to_thread(logger.record, **values)


async def _emit_audit_best_effort(logger: AuditLogger, **values: Any) -> None:
    try:
        await _emit_audit(logger, **values)
    except Exception:
        # Never replace the original provider/runtime exception with a
        # secondary audit-sink failure while unwinding an unsuccessful run.
        return


async def _audit_workflow_nodes(
    logger: AuditLogger,
    result: WorkflowResult,
    *,
    action_mode: ActionMode,
) -> None:
    for node in result.nodes:
        duration_ms = max(0.0, (node.finished_at - node.started_at).total_seconds() * 1000)
        await _emit_audit(
            logger,
            run_id=result.run_id,
            event_type=AuditEventType.NODE_COMPLETED,
            subject_id=node.node_id,
            status=RunStatus.SUCCEEDED,
            metadata={
                "node.kind": node.kind,
                "node.status": node.status.value,
                "node.duration_ms": round(duration_ms, 3),
            },
        )
        if node.kind == "action":
            await _emit_audit(
                logger,
                run_id=result.run_id,
                event_type=AuditEventType.ACTION_DECIDED,
                subject_id=node.node_id,
                status=RunStatus.SUCCEEDED,
                metadata={
                    "action.mode": action_mode.value,
                    "action.executed": node.status.value == "succeeded",
                },
            )


def _positive_finite(value: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and (
        value > 0 and math.isfinite(value)
    )


def _validate_options(
    options_model: type[BaseModel],
    raw_options: Mapping[str, Any],
) -> BaseModel:
    if not isinstance(raw_options, Mapping):
        raise RuntimeInputError("plugin options must be an object")
    try:
        return options_model.model_validate(dict(raw_options), strict=True)
    except ValidationError as exc:
        raise RuntimeInputError(f"invalid plugin options: {exc}") from exc


def _skills_for_run(
    plugin: DomainPlugin,
    options: BaseModel,
    selected: SkillSpec | str | Path | None,
) -> tuple[SkillSpec, ...]:
    if selected is not None:
        skills = (selected if isinstance(selected, SkillSpec) else load_skill(selected),)
    else:
        resolver = getattr(plugin, "skills_for_options", None)
        if resolver is None:
            skills = ()
        else:
            resolved = resolver(options)
            if not isinstance(resolved, Sequence) or isinstance(
                resolved, (str, bytes, bytearray)
            ):
                raise RuntimeContractError(
                    "plugin skills_for_options() must return an ordered sequence"
                )
            skills = tuple(resolved)
    if any(not isinstance(item, SkillSpec) for item in skills):
        raise RuntimeContractError("plugin skills_for_options() returned an invalid SkillSpec")
    if len(skills) > 1:
        raise RuntimeConfigurationError(
            "the current runtime contract supports at most one skill per run"
        )
    return skills


def _require_capabilities(
    provider: ProviderAdapter,
    required: Sequence[str],
    *,
    has_skills: bool,
) -> None:
    missing = [name for name in required if getattr(provider.capabilities, name, False) is not True]
    if has_skills and provider.capabilities.skills is not True:
        missing.append("skills")
    if missing:
        unique = ", ".join(dict.fromkeys(missing))
        raise ProviderCapabilityError(
            f"provider does not satisfy plugin capabilities: {unique}",
            provider=provider.name,
        )


def _create_workflow(plugin: DomainPlugin, context: PluginRuntimeContext) -> Workflow:
    try:
        workflow = plugin.create_workflow(context)
    except Exception as exc:
        raise RuntimeContractError(
            f"plugin {plugin.plugin_id!r} could not create its workflow"
        ) from exc
    if not isinstance(workflow, Workflow):
        raise RuntimeContractError(
            f"plugin {plugin.plugin_id!r} create_workflow() must return Workflow"
        )
    return workflow


def _validate_workflow_input(
    input_model: type[BaseModel],
    *,
    content: bytes,
    options: BaseModel,
    filename: str,
) -> BaseModel:
    try:
        return input_model.model_validate(
            {"content": content, "options": options, "filename": filename},
            strict=True,
        )
    except ValidationError as exc:
        raise RuntimeInputError(f"input does not satisfy the plugin schema: {exc}") from exc


async def _await_workflow(
    execution: Any,
    *,
    cancel_event: asyncio.Event | None,
) -> WorkflowResult:
    task = asyncio.create_task(execution, name="agent-core-workflow")
    if cancel_event is None:
        try:
            return await task
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    cancellation = asyncio.create_task(cancel_event.wait(), name="agent-core-cancellation")
    try:
        done, _ = await asyncio.wait(
            (task, cancellation),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation in done and cancel_event.is_set():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise asyncio.CancelledError
        return await task
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    finally:
        cancellation.cancel()
        await asyncio.gather(cancellation, return_exceptions=True)


def _normalize_result(
    workflow_result: WorkflowResult,
    *,
    output_model: type[BaseModel],
    plugin_id: str,
    provider_name: str,
    model: str,
) -> RuntimeResult:
    candidates = tuple(
        output
        for output in workflow_result.final_outputs
        if isinstance(output, output_model)
    )
    if len(candidates) != 1:
        raise RuntimeContractError(
            "plugin workflow must expose exactly one terminal output matching its "
            "manifest artifact schema"
        )
    output = candidates[0]

    if not isinstance(output, ArtifactOutput):
        raise RuntimeContractError("plugin terminal output must inherit ArtifactOutput")

    partial = output.partial or workflow_result.status is RunStatus.DEGRADED
    warnings = tuple(
        dict.fromkeys((*workflow_result.warnings, *output.warnings))
    )
    return RuntimeResult(
        run_id=workflow_result.run_id,
        plugin_id=plugin_id,
        provider=provider_name,
        model=model,
        status=RunStatus.DEGRADED if partial else RunStatus.SUCCEEDED,
        artifact=RuntimeArtifact(
            content=output.content,
            media_type=output.media_type,
            filename=output.filename,
        ),
        warnings=warnings,
        partial=partial,
    )


__all__ = [
    "AgentRuntime",
    "AgentRuntimeError",
    "PluginRuntimeContext",
    "RuntimeArtifact",
    "RuntimeConfigurationError",
    "RuntimeContractError",
    "RuntimeInputError",
    "RuntimeResult",
]
