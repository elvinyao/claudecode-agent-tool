from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from agent_core.contracts import ActionMode, AgentCoreError, RunStatus
from agent_core.workflow import (
    ActionNode,
    AgentAttemptTimeoutError,
    AgentNode,
    DuplicateNodeIdError,
    NodeExecutionError,
    NodeStatus,
    RetryPolicy,
    TransformNode,
    UnknownDependencyError,
    Workflow,
    WorkflowCycleError,
    WorkflowDeadlineExceededError,
    WorkflowDefinitionError,
    WorkflowTypeError,
)


def _identity(value: Any, _context: Any) -> Any:
    return value


def test_dag_rejects_duplicate_unknown_cycle_and_type_mismatch() -> None:
    first = TransformNode(
        id="first",
        input_type=str,
        output_type=int,
        handler=lambda value, _context: len(value),
    )
    with pytest.raises(DuplicateNodeIdError):
        Workflow((first, first), input_type=str)

    unknown = TransformNode(
        id="unknown",
        depends_on=("missing",),
        input_type=int,
        output_type=int,
        handler=_identity,
    )
    with pytest.raises(UnknownDependencyError):
        Workflow((unknown,), input_type=str)

    left = TransformNode(
        id="left",
        depends_on=("right",),
        input_type=int,
        output_type=int,
        handler=_identity,
    )
    right = TransformNode(
        id="right",
        depends_on=("left",),
        input_type=int,
        output_type=int,
        handler=_identity,
    )
    with pytest.raises(WorkflowCycleError):
        Workflow((left, right), input_type=int)

    wrong = TransformNode(
        id="wrong",
        depends_on=("first",),
        input_type=str,
        output_type=str,
        handler=_identity,
    )
    with pytest.raises(WorkflowTypeError):
        Workflow((first, wrong), input_type=str)


@pytest.mark.asyncio
async def test_agent_fanout_is_concurrent_but_output_order_is_stable() -> None:
    transform = TransformNode(
        id="prepare",
        input_type=str,
        output_type=int,
        output_many=True,
        handler=lambda _value, _context: [3, 1, 2],
    )

    async def analyze(value: int, _context: Any) -> str:
        await asyncio.sleep(value * 0.001)
        return str(value)

    agent = AgentNode(
        id="analyze",
        depends_on=("prepare",),
        input_type=int,
        output_type=str,
        handler=analyze,
        retry_policy=RetryPolicy(
            initial_backoff_seconds=0.0,
            max_backoff_seconds=0.0,
            jitter_ratio=0.0,
        ),
    )
    merge = TransformNode(
        id="merge",
        depends_on=("analyze",),
        input_type=str,
        input_many=True,
        output_type=str,
        handler=lambda values, _context: ",".join(values),
    )

    result = await Workflow((transform, agent, merge), input_type=str).execute("input")

    assert result.status is RunStatus.SUCCEEDED
    assert result.node("analyze").output == ("3", "1", "2")
    assert result.final_outputs == ("3,1,2",)


@pytest.mark.asyncio
async def test_action_modes_never_apply_when_disabled_or_dry_run() -> None:
    calls: list[str] = []
    prepare = TransformNode(
        id="prepare",
        input_type=str,
        output_type=str,
        handler=_identity,
    )
    action = ActionNode(
        id="publish",
        depends_on=("prepare",),
        input_type=str,
        output_type=str,
        dry_run_handler=lambda value, _context: calls.append("preview") or f"dry:{value}",
        apply_handler=lambda value, _context: calls.append("apply") or f"apply:{value}",
    )
    workflow = Workflow((prepare, action), input_type=str)

    disabled = await workflow.execute("x", action_mode=ActionMode.DISABLED)
    dry_run = await workflow.execute("x", action_mode=ActionMode.DRY_RUN)
    applied = await workflow.execute("x", action_mode=ActionMode.APPLY)

    assert disabled.node("publish").status is NodeStatus.SKIPPED
    assert disabled.node("publish").output is None
    assert dry_run.node("publish").output == "dry:x"
    assert applied.node("publish").output == "apply:x"
    assert calls == ["preview", "apply"]


def test_action_and_conditional_nodes_must_be_terminal() -> None:
    action = ActionNode(
        id="action",
        input_type=str,
        output_type=str,
        apply_handler=_identity,
    )
    after = TransformNode(
        id="after",
        depends_on=("action",),
        input_type=str,
        output_type=str,
        handler=_identity,
    )
    with pytest.raises(WorkflowDefinitionError, match="action nodes"):
        Workflow((action, after), input_type=str)

    conditional = TransformNode(
        id="conditional",
        input_type=str,
        output_type=str,
        handler=_identity,
        when=lambda _value, _context: False,
    )
    downstream = after.model_copy(update={"depends_on": ("conditional",)})
    with pytest.raises(WorkflowDefinitionError, match="conditional nodes"):
        Workflow((conditional, downstream), input_type=str)


class BatchOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    value: str
    partial: bool = False
    warnings: tuple[str, ...] = ()


class ExpectedAgentFailure(AgentCoreError):
    pass


class RetryableFailure(ExpectedAgentFailure):
    retryable = True
    retry_after_seconds = 0.0


@pytest.mark.asyncio
async def test_agent_retries_only_retryable_failures_and_uses_final_fallback() -> None:
    attempts: dict[str, int] = {"retry": 0, "fallback": 0}

    async def analyze(value: str, _context: Any) -> BatchOutcome:
        attempts[value] += 1
        if value == "retry" and attempts[value] < 3:
            raise RetryableFailure("temporary")
        raise ExpectedAgentFailure("permanent")

    async def fallback(value: str, error: Exception, _context: Any) -> BatchOutcome:
        return BatchOutcome(
            value=f"fallback:{value}:{type(error).__name__}",
            partial=True,
            warnings=(f"{value} degraded",),
        )

    agent = AgentNode(
        id="agent",
        input_type=str,
        output_type=BatchOutcome,
        handler=analyze,
        fallback_handler=fallback,
        retry_policy=RetryPolicy(
            max_attempts=3,
            initial_backoff_seconds=0.0,
            max_backoff_seconds=0.0,
            jitter_ratio=0.0,
        ),
    )
    result = await Workflow((agent,), input_type=str, input_many=True).execute(
        ("retry", "fallback")
    )

    assert attempts == {"retry": 3, "fallback": 1}
    assert result.status is RunStatus.DEGRADED
    assert result.warnings == ("retry degraded", "fallback degraded")
    assert [item.value for item in result.final_outputs[0]] == [
        "fallback:retry:ExpectedAgentFailure",
        "fallback:fallback:ExpectedAgentFailure",
    ]


@pytest.mark.asyncio
async def test_agent_does_not_hide_programming_errors_behind_fallback() -> None:
    fallback_calls = 0

    async def broken(_value: str, _context: Any) -> BatchOutcome:
        raise TypeError("programming defect")

    async def fallback(
        _value: str,
        _error: Exception,
        _context: Any,
    ) -> BatchOutcome:
        nonlocal fallback_calls
        fallback_calls += 1
        return BatchOutcome(value="should-not-run", partial=True)

    workflow = Workflow(
        (
            AgentNode(
                id="agent",
                input_type=str,
                output_type=BatchOutcome,
                handler=broken,
                fallback_handler=fallback,
                retry_policy=RetryPolicy(max_attempts=1),
            ),
        ),
        input_type=str,
        input_many=True,
    )

    with pytest.raises(NodeExecutionError) as captured:
        await workflow.execute(("x",))

    assert isinstance(captured.value.__cause__, TypeError)
    assert fallback_calls == 0


@pytest.mark.asyncio
async def test_attempt_timeout_and_total_deadline_are_typed() -> None:
    async def slow(value: str, _context: Any) -> str:
        await asyncio.sleep(0.05)
        return value

    agent = AgentNode(
        id="agent",
        input_type=str,
        output_type=str,
        handler=slow,
        retry_policy=RetryPolicy(
            max_attempts=1,
            initial_backoff_seconds=0.0,
            max_backoff_seconds=0.0,
            jitter_ratio=0.0,
            attempt_timeout_seconds=0.001,
        ),
    )
    with pytest.raises(NodeExecutionError) as attempt_error:
        await Workflow((agent,), input_type=str, input_many=True).execute(("x",))
    assert isinstance(attempt_error.value.__cause__, AgentAttemptTimeoutError)

    transform = TransformNode(
        id="slow",
        input_type=str,
        output_type=str,
        handler=slow,
    )
    with pytest.raises(WorkflowDeadlineExceededError):
        await Workflow((transform,), input_type=str).execute("x", deadline_seconds=0.001)
