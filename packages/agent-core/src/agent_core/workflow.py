"""Validated DAG execution for deterministic, agent, and action nodes."""

from __future__ import annotations

import asyncio
import inspect
import random
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal, TypeAlias
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from agent_core.contracts import (
    ActionMode,
    AgentCoreError,
    RunStatus,
    StrictFrozenModel,
)
from agent_core.progress import ProgressEventType, ProgressSink, WorkflowProgress

NodeHandler: TypeAlias = Callable[[Any, "WorkflowContext"], Any | Awaitable[Any]]
NodeCondition: TypeAlias = Callable[[Any, "WorkflowContext"], bool | Awaitable[bool]]
AgentFallbackHandler: TypeAlias = Callable[
    [Any, Exception, "WorkflowContext"], Any | Awaitable[Any]
]
_NODE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PROGRESS_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class WorkflowError(AgentCoreError):
    """Base class for workflow definition and execution failures."""


class WorkflowDefinitionError(WorkflowError, ValueError):
    """A workflow graph is invalid before execution."""


class DuplicateNodeIdError(WorkflowDefinitionError):
    """Two nodes declare the same identifier."""


class UnknownDependencyError(WorkflowDefinitionError):
    """A node references an identifier absent from the workflow."""


class WorkflowCycleError(WorkflowDefinitionError):
    """The workflow dependency graph contains a cycle."""


class WorkflowTypeError(WorkflowDefinitionError, TypeError):
    """Connected node ports have incompatible types or cardinality."""


class NodeExecutionError(WorkflowError):
    """One node failed or returned a value outside its declared contract."""

    def __init__(self, node_id: str, message: str) -> None:
        super().__init__(f"node {node_id!r}: {message}")
        self.node_id = node_id
        self.message = message


class WorkflowDeadlineExceededError(WorkflowError, TimeoutError):
    """The run-wide workflow deadline expired."""


class AgentAttemptTimeoutError(WorkflowError, TimeoutError):
    """One AgentNode attempt exceeded its configured timeout."""

    retryable = True
    retry_after_seconds: float | None = None


class RetryPolicy(StrictFrozenModel):
    """Bounded retry budget used only by AgentNode provider calls."""

    max_attempts: int = Field(default=3, ge=1, le=10)
    initial_backoff_seconds: float = Field(default=0.25, ge=0)
    max_backoff_seconds: float = Field(default=8.0, ge=0)
    multiplier: float = Field(default=2.0, ge=1)
    jitter_ratio: float = Field(default=0.2, ge=0, le=1)
    attempt_timeout_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_backoff_bounds(self) -> RetryPolicy:
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("max_backoff_seconds must not be below initial_backoff_seconds")
        return self

    def delay_seconds(self, failed_attempt: int, *, random_value: float) -> float:
        """Return capped exponential backoff with symmetric bounded jitter."""

        base = min(
            self.max_backoff_seconds,
            self.initial_backoff_seconds * self.multiplier ** max(0, failed_attempt - 1),
        )
        jitter = base * self.jitter_ratio * ((2 * random_value) - 1)
        return max(0.0, base + jitter)


class NodeStatus(str, Enum):
    """Execution status of an individual workflow node."""

    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"


class WorkflowContext(StrictFrozenModel):
    """Run-scoped context passed explicitly to every node handler."""

    run_id: str
    action_mode: ActionMode = ActionMode.DISABLED
    metadata: Mapping[str, Any] = Field(default_factory=dict)
    deadline_at: datetime | None = None
    attempt_timeout_seconds: float | None = Field(default=None, gt=0)
    progress_sink: ProgressSink | None = Field(default=None, exclude=True, repr=False)

    def remaining_seconds(self) -> float | None:
        """Return wall-clock time remaining for handler-visible deadline checks."""

        if self.deadline_at is None:
            return None
        return max(0.0, (self.deadline_at - datetime.now(timezone.utc)).total_seconds())


class BaseNode(StrictFrozenModel):
    """Shared typed ports and dependency declaration for a DAG node."""

    id: str
    depends_on: tuple[str, ...] = ()
    input_type: type[Any] = object
    output_type: type[Any] = object
    input_many: bool = False
    output_many: bool = False
    when: NodeCondition | None = Field(default=None, exclude=True, repr=False)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if _NODE_ID.fullmatch(value) is None:
            raise ValueError(
                "node id must start with a lowercase letter and contain at most "
                "64 lowercase letters, digits, underscores, or hyphens"
            )
        return value

    @field_validator("depends_on")
    @classmethod
    def validate_dependencies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("node dependencies must be unique")
        if any(_NODE_ID.fullmatch(item) is None for item in value):
            raise ValueError("dependency ids must be valid node ids")
        return value


class TransformNode(BaseNode):
    """A deterministic transformation with no framework-authorized side effect."""

    kind: Literal["transform"] = "transform"
    handler: NodeHandler = Field(exclude=True, repr=False)


class AgentNode(BaseNode):
    """Fan out an ordered sequence of requests and preserve result order."""

    kind: Literal["agent"] = "agent"
    input_many: Literal[True] = True
    output_many: Literal[True] = True
    handler: NodeHandler = Field(exclude=True, repr=False)
    fallback_handler: AgentFallbackHandler | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    fallback_error_types: tuple[type[Exception], ...] = Field(
        default=(AgentCoreError,),
        exclude=True,
        repr=False,
    )
    max_concurrency: int | None = Field(default=None, ge=1)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)

    @field_validator("fallback_error_types")
    @classmethod
    def validate_fallback_error_types(
        cls,
        value: tuple[type[Exception], ...],
    ) -> tuple[type[Exception], ...]:
        if not value:
            raise ValueError("fallback_error_types must not be empty")
        if any(
            not isinstance(error_type, type) or not issubclass(error_type, Exception)
            for error_type in value
        ):
            raise ValueError("fallback_error_types must contain Exception classes")
        return value


class ActionNode(BaseNode):
    """A terminal side-effect boundary with separate preview and apply handlers."""

    kind: Literal["action"] = "action"
    dry_run_handler: NodeHandler | None = Field(default=None, exclude=True, repr=False)
    apply_handler: NodeHandler = Field(exclude=True, repr=False)


WorkflowNode: TypeAlias = TransformNode | AgentNode | ActionNode


class NodeExecution(StrictFrozenModel):
    """Immutable record of one completed or intentionally skipped node."""

    node_id: str
    kind: Literal["transform", "agent", "action"]
    status: NodeStatus
    output: Any = None
    started_at: datetime
    finished_at: datetime


class WorkflowResult(StrictFrozenModel):
    """Ordered result of a successful or intentionally degraded workflow run."""

    run_id: str
    status: RunStatus
    nodes: tuple[NodeExecution, ...]
    final_node_ids: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def node(self, node_id: str) -> NodeExecution:
        """Return one node result or raise a typed lookup error."""

        for result in self.nodes:
            if result.node_id == node_id:
                return result
        raise UnknownDependencyError(f"workflow result has no node {node_id!r}")

    @property
    def final_outputs(self) -> tuple[Any, ...]:
        """Outputs of terminal nodes in stable declaration order."""

        return tuple(self.node(node_id).output for node_id in self.final_node_ids)


class Workflow:
    """A validated DAG with deterministic topological execution order."""

    def __init__(
        self,
        nodes: Sequence[WorkflowNode],
        *,
        input_type: type[Any] = object,
        input_many: bool = False,
    ) -> None:
        self._nodes = tuple(nodes)
        self.input_type = input_type
        self.input_many = input_many
        self._node_by_id = self._validate_ids_and_dependencies()
        self._order = self._topological_order()
        self._validate_types()
        self._validate_action_nodes_are_terminal()
        self._validate_conditional_nodes_are_terminal()

    @property
    def nodes(self) -> tuple[WorkflowNode, ...]:
        return self._nodes

    @property
    def ordered_node_ids(self) -> tuple[str, ...]:
        return tuple(node.id for node in self._order)

    @property
    def final_node_ids(self) -> tuple[str, ...]:
        depended_on = {dependency for node in self._nodes for dependency in node.depends_on}
        return tuple(node.id for node in self._nodes if node.id not in depended_on)

    def _validate_ids_and_dependencies(self) -> dict[str, WorkflowNode]:
        by_id: dict[str, WorkflowNode] = {}
        for node in self._nodes:
            if node.id in by_id:
                raise DuplicateNodeIdError(f"duplicate workflow node id: {node.id!r}")
            by_id[node.id] = node
        for node in self._nodes:
            for dependency in node.depends_on:
                if dependency not in by_id:
                    raise UnknownDependencyError(
                        f"node {node.id!r} depends on unknown node {dependency!r}"
                    )
        return by_id

    def _topological_order(self) -> tuple[WorkflowNode, ...]:
        completed: set[str] = set()
        remaining = list(self._nodes)
        ordered: list[WorkflowNode] = []
        while remaining:
            ready = [node for node in remaining if set(node.depends_on) <= completed]
            if not ready:
                involved = ", ".join(node.id for node in remaining)
                raise WorkflowCycleError(f"workflow contains a dependency cycle: {involved}")
            for node in ready:
                ordered.append(node)
                completed.add(node.id)
                remaining.remove(node)
        return tuple(ordered)

    def _validate_types(self) -> None:
        for node in self._nodes:
            if not node.depends_on:
                self._validate_port(
                    source_id="workflow-input",
                    source_type=self.input_type,
                    source_many=self.input_many,
                    target=node,
                )
                continue
            if len(node.depends_on) > 1:
                if node.input_type is not tuple or node.input_many:
                    raise WorkflowTypeError(
                        f"fan-in node {node.id!r} must declare input_type=tuple and "
                        "input_many=False"
                    )
                continue
            upstream = self._node_by_id[node.depends_on[0]]
            self._validate_port(
                source_id=upstream.id,
                source_type=upstream.output_type,
                source_many=upstream.output_many,
                target=node,
            )

    @staticmethod
    def _validate_port(
        *,
        source_id: str,
        source_type: type[Any],
        source_many: bool,
        target: WorkflowNode,
    ) -> None:
        if source_many != target.input_many:
            raise WorkflowTypeError(
                f"{source_id!r} cardinality does not match input of {target.id!r}"
            )
        try:
            compatible = issubclass(source_type, target.input_type)
        except TypeError:
            compatible = source_type == target.input_type
        if not compatible:
            raise WorkflowTypeError(
                f"{source_id!r} outputs {source_type!r}, but {target.id!r} expects "
                f"{target.input_type!r}"
            )

    def _validate_action_nodes_are_terminal(self) -> None:
        dependencies = {dependency for node in self._nodes for dependency in node.depends_on}
        non_terminal = [
            node.id
            for node in self._nodes
            if isinstance(node, ActionNode) and node.id in dependencies
        ]
        if non_terminal:
            raise WorkflowDefinitionError(
                "action nodes must be terminal: " + ", ".join(non_terminal)
            )

    def _validate_conditional_nodes_are_terminal(self) -> None:
        dependencies = {dependency for node in self._nodes for dependency in node.depends_on}
        non_terminal = [
            node.id for node in self._nodes if node.when is not None and node.id in dependencies
        ]
        if non_terminal:
            raise WorkflowDefinitionError(
                "conditional nodes must be terminal because skipped outputs have no value: "
                + ", ".join(non_terminal)
            )

    async def execute(
        self,
        initial_input: Any,
        *,
        action_mode: ActionMode = ActionMode.DISABLED,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        deadline_seconds: float | None = None,
        attempt_timeout_seconds: float | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> WorkflowResult:
        """Execute the graph and preserve declared order at every fanout boundary."""

        if deadline_seconds is not None and deadline_seconds <= 0:
            raise WorkflowDefinitionError("deadline_seconds must be greater than zero")
        if attempt_timeout_seconds is not None and attempt_timeout_seconds <= 0:
            raise WorkflowDefinitionError("attempt_timeout_seconds must be greater than zero")
        now = datetime.now(timezone.utc)
        context = WorkflowContext(
            run_id=run_id or uuid4().hex,
            action_mode=action_mode,
            metadata={} if metadata is None else metadata,
            deadline_at=(
                now + timedelta(seconds=deadline_seconds) if deadline_seconds is not None else None
            ),
            attempt_timeout_seconds=attempt_timeout_seconds,
            progress_sink=progress_sink,
        )
        self._validate_value(
            node_id="workflow-input",
            value=initial_input,
            expected_type=self.input_type,
            many=self.input_many,
        )

        execution = self._execute_graph(initial_input, context)
        if deadline_seconds is None:
            return await execution
        try:
            return await asyncio.wait_for(execution, timeout=deadline_seconds)
        except asyncio.TimeoutError as exc:
            raise WorkflowDeadlineExceededError(
                f"workflow {context.run_id!r} exceeded its {deadline_seconds:g}-second deadline"
            ) from exc

    async def _execute_graph(
        self,
        initial_input: Any,
        context: WorkflowContext,
    ) -> WorkflowResult:
        """Execute validated nodes under an already constructed run context."""

        outputs: dict[str, Any] = {}
        executions: list[NodeExecution] = []
        warnings: list[str] = []
        degraded = False

        for node in self._order:
            started_at = datetime.now(timezone.utc)
            payload = self._payload_for(node, initial_input, outputs)
            await _emit_workflow_progress(
                context,
                ProgressEventType.STEP_STARTED,
                node,
                node_status="running",
            )
            try:
                if node.when is not None:
                    condition = await _invoke_condition(node.when, payload, context)
                    if not condition:
                        status, output = NodeStatus.SKIPPED, None
                    elif isinstance(node, ActionNode):
                        status, output = await self._execute_action(node, payload, context)
                    elif isinstance(node, AgentNode):
                        status = NodeStatus.SUCCEEDED
                        output = await self._execute_agent(node, payload, context)
                    else:
                        status = NodeStatus.SUCCEEDED
                        output = await _invoke(node.handler, payload, context)
                        output = self._validate_value(
                            node_id=node.id,
                            value=output,
                            expected_type=node.output_type,
                            many=node.output_many,
                        )
                elif isinstance(node, ActionNode):
                    status, output = await self._execute_action(node, payload, context)
                elif isinstance(node, AgentNode):
                    status = NodeStatus.SUCCEEDED
                    output = await self._execute_agent(node, payload, context)
                else:
                    status = NodeStatus.SUCCEEDED
                    output = await _invoke(node.handler, payload, context)
                    output = self._validate_value(
                        node_id=node.id,
                        value=output,
                        expected_type=node.output_type,
                        many=node.output_many,
                    )
            except asyncio.CancelledError:
                await _emit_workflow_progress_best_effort(
                    context,
                    ProgressEventType.STEP_CANCELLED,
                    node,
                    node_status="cancelled",
                    error_code="node_cancelled",
                    duration_ms=max(
                        0.0,
                        (datetime.now(timezone.utc) - started_at).total_seconds() * 1000,
                    ),
                )
                raise
            except (NodeExecutionError, WorkflowDeadlineExceededError) as exc:
                await _emit_workflow_progress_best_effort(
                    context,
                    ProgressEventType.STEP_FAILED,
                    node,
                    node_status="failed",
                    error_code=_safe_progress_error_code(exc),
                    duration_ms=max(
                        0.0,
                        (datetime.now(timezone.utc) - started_at).total_seconds() * 1000,
                    ),
                )
                raise
            except Exception as exc:
                await _emit_workflow_progress_best_effort(
                    context,
                    ProgressEventType.STEP_FAILED,
                    node,
                    node_status="failed",
                    error_code=_safe_progress_error_code(exc),
                    duration_ms=max(
                        0.0,
                        (datetime.now(timezone.utc) - started_at).total_seconds() * 1000,
                    ),
                )
                raise NodeExecutionError(node.id, str(exc)) from exc

            outputs[node.id] = output
            if _contains_partial_provider_result(output):
                degraded = True
            warnings.extend(_provider_warnings(output))
            finished_at = datetime.now(timezone.utc)
            if status is NodeStatus.SUCCEEDED:
                if node.output_many:
                    assert isinstance(output, tuple)
                await _emit_workflow_progress(
                    context,
                    ProgressEventType.VALIDATION_COMPLETED,
                    node,
                    node_status="succeeded",
                    accepted_count=(
                        len(output) if isinstance(output, tuple) and node.output_many else 1
                    ),
                )
            await _emit_workflow_progress(
                context,
                ProgressEventType.STEP_COMPLETED,
                node,
                node_status=status.value,
                duration_ms=max(0.0, (finished_at - started_at).total_seconds() * 1000),
            )
            executions.append(
                NodeExecution(
                    node_id=node.id,
                    kind=node.kind,
                    status=status,
                    output=output,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            )

        return WorkflowResult(
            run_id=context.run_id,
            status=RunStatus.DEGRADED if degraded else RunStatus.SUCCEEDED,
            nodes=tuple(executions),
            final_node_ids=self.final_node_ids,
            warnings=tuple(warnings),
        )

    async def run(self, initial_input: Any, **kwargs: Any) -> WorkflowResult:
        """Compatibility alias for transports that call workflows as runs."""

        return await self.execute(initial_input, **kwargs)

    @staticmethod
    def _payload_for(
        node: WorkflowNode,
        initial_input: Any,
        outputs: Mapping[str, Any],
    ) -> Any:
        if not node.depends_on:
            return initial_input
        if len(node.depends_on) == 1:
            return outputs[node.depends_on[0]]
        return tuple(outputs[dependency] for dependency in node.depends_on)

    async def _execute_agent(
        self,
        node: AgentNode,
        payload: Any,
        context: WorkflowContext,
    ) -> tuple[Any, ...]:
        items = self._validate_value(
            node_id=node.id,
            value=payload,
            expected_type=node.input_type,
            many=True,
        )
        await _emit_workflow_progress(
            context,
            ProgressEventType.AGENT_BATCH_STARTED,
            node,
            node_status="running",
            batch_size=len(items),
        )
        semaphore = (
            asyncio.Semaphore(node.max_concurrency) if node.max_concurrency is not None else None
        )

        async def execute_attempts(item: Any) -> Any:
            result: Any = None
            for attempt in range(1, node.retry_policy.max_attempts + 1):
                try:
                    timeout = (
                        node.retry_policy.attempt_timeout_seconds or context.attempt_timeout_seconds
                    )
                    invocation = _invoke(node.handler, item, context)
                    result = (
                        await invocation
                        if timeout is None
                        else await asyncio.wait_for(invocation, timeout=timeout)
                    )
                    break
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError as exc:
                    error: Exception = AgentAttemptTimeoutError(
                        f"AgentNode {node.id!r} attempt {attempt} timed out"
                    )
                    error.__cause__ = exc
                except Exception as exc:
                    error = exc

                retry_after = getattr(error, "retry_after_seconds", None)
                retryable = bool(getattr(error, "retryable", False)) or retry_after is not None
                if not retryable or attempt >= node.retry_policy.max_attempts:
                    if node.fallback_handler is None or not isinstance(
                        error,
                        node.fallback_error_types,
                    ):
                        raise error
                    result = await _invoke_fallback(
                        node.fallback_handler,
                        item,
                        error,
                        context,
                    )
                    break
                delay = (
                    float(retry_after)
                    if isinstance(retry_after, (int, float)) and retry_after >= 0
                    else node.retry_policy.delay_seconds(
                        attempt,
                        random_value=random.random(),
                    )
                )
                remaining = context.remaining_seconds()
                if remaining is not None and delay >= remaining:
                    raise WorkflowDeadlineExceededError(
                        f"retry delay for node {node.id!r} exceeds the workflow deadline"
                    ) from error
                await _emit_workflow_progress(
                    context,
                    ProgressEventType.RETRY_SCHEDULED,
                    node,
                    node_status="running",
                    attempt=attempt,
                    max_attempts=node.retry_policy.max_attempts,
                    delay_seconds=delay,
                    error_code=_safe_progress_error_code(error),
                )
                if delay:
                    await asyncio.sleep(delay)

            return self._validate_value(
                node_id=node.id,
                value=result,
                expected_type=node.output_type,
                many=False,
            )

        async def execute_one(item: Any) -> Any:
            if semaphore is None:
                return await execute_attempts(item)
            async with semaphore:
                return await execute_attempts(item)

        # asyncio.gather executes concurrently but returns results in input order.
        results = tuple(await asyncio.gather(*(execute_one(item) for item in items)))
        await _emit_workflow_progress(
            context,
            ProgressEventType.AGENT_BATCH_COMPLETED,
            node,
            node_status="succeeded",
            batch_size=len(items),
            accepted_count=len(results),
        )
        return results

    async def _execute_action(
        self,
        node: ActionNode,
        payload: Any,
        context: WorkflowContext,
    ) -> tuple[NodeStatus, Any]:
        self._validate_value(
            node_id=node.id,
            value=payload,
            expected_type=node.input_type,
            many=node.input_many,
        )
        if context.action_mode is ActionMode.DISABLED:
            return NodeStatus.SKIPPED, None
        if context.action_mode is ActionMode.DRY_RUN:
            if node.dry_run_handler is None:
                return NodeStatus.SKIPPED, None
            output = await _invoke(node.dry_run_handler, payload, context)
        else:
            output = await _invoke(node.apply_handler, payload, context)
        return NodeStatus.SUCCEEDED, self._validate_value(
            node_id=node.id,
            value=output,
            expected_type=node.output_type,
            many=node.output_many,
        )

    @staticmethod
    def _validate_value(
        *,
        node_id: str,
        value: Any,
        expected_type: type[Any],
        many: bool,
    ) -> Any:
        if many:
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
                raise NodeExecutionError(node_id, "expected an ordered sequence")
            values = tuple(value)
            invalid = [item for item in values if not isinstance(item, expected_type)]
            if invalid:
                raise NodeExecutionError(
                    node_id,
                    f"sequence item does not match declared type {expected_type!r}",
                )
            return values
        if not isinstance(value, expected_type):
            raise NodeExecutionError(
                node_id,
                f"value {type(value)!r} does not match declared type {expected_type!r}",
            )
        return value


async def _invoke(handler: NodeHandler, payload: Any, context: WorkflowContext) -> Any:
    result = handler(payload, context)
    return await result if inspect.isawaitable(result) else result


async def _invoke_condition(
    condition: NodeCondition,
    payload: Any,
    context: WorkflowContext,
) -> bool:
    result = condition(payload, context)
    value = await result if inspect.isawaitable(result) else result
    if not isinstance(value, bool):
        raise TypeError("node condition must return bool")
    return value


async def _invoke_fallback(
    handler: AgentFallbackHandler,
    item: Any,
    error: Exception,
    context: WorkflowContext,
) -> Any:
    result = handler(item, error, context)
    return await result if inspect.isawaitable(result) else result


def _contains_partial_provider_result(value: Any) -> bool:
    partial = getattr(value, "partial", None)
    if isinstance(partial, bool):
        return partial
    if isinstance(value, tuple):
        return any(_contains_partial_provider_result(item) for item in value)
    return False


def _provider_warnings(value: Any) -> list[str]:
    candidate = getattr(value, "warnings", None)
    if isinstance(candidate, (list, tuple)) and all(isinstance(item, str) for item in candidate):
        return list(candidate)
    if isinstance(value, tuple):
        warnings: list[str] = []
        for item in value:
            warnings.extend(_provider_warnings(item))
        return warnings
    return []


async def _emit_workflow_progress(
    context: WorkflowContext,
    event_type: ProgressEventType,
    node: WorkflowNode,
    **details: Any,
) -> None:
    sink = context.progress_sink
    if sink is None:
        return
    await sink(
        WorkflowProgress(
            run_id=context.run_id,
            type=event_type,
            node_id=node.id,
            node_kind=node.kind,
            **details,
        )
    )


async def _emit_workflow_progress_best_effort(
    context: WorkflowContext,
    event_type: ProgressEventType,
    node: WorkflowNode,
    **details: Any,
) -> None:
    """Preserve an active node exception if its observer also fails."""

    try:
        await _emit_workflow_progress(context, event_type, node, **details)
    except Exception:
        return


def _safe_progress_error_code(error: BaseException) -> str:
    candidate = getattr(error, "code", None)
    if isinstance(candidate, str) and _PROGRESS_CODE.fullmatch(candidate) is not None:
        return candidate
    if isinstance(error, WorkflowDeadlineExceededError):
        return "workflow_deadline"
    if isinstance(error, AgentAttemptTimeoutError):
        return "agent_attempt_timeout"
    return "node_execution"


__all__ = [
    "ActionNode",
    "AgentFallbackHandler",
    "AgentAttemptTimeoutError",
    "AgentNode",
    "BaseNode",
    "DuplicateNodeIdError",
    "NodeExecution",
    "NodeExecutionError",
    "NodeHandler",
    "NodeCondition",
    "NodeStatus",
    "ProgressSink",
    "TransformNode",
    "RetryPolicy",
    "UnknownDependencyError",
    "Workflow",
    "WorkflowContext",
    "WorkflowCycleError",
    "WorkflowDeadlineExceededError",
    "WorkflowDefinitionError",
    "WorkflowError",
    "WorkflowNode",
    "WorkflowResult",
    "WorkflowTypeError",
]
