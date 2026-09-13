from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from agent_core.contracts import (
    ActionMode,
    AgentRequest,
    ProviderResult,
    RunStatus,
    ToolPolicy,
)


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    value: int


def test_agent_request_and_provider_result_are_strict_and_frozen() -> None:
    request = AgentRequest[Answer](
        request_id="request-1",
        prompt="answer deterministically",
        response_model=Answer,
        tool_policy=ToolPolicy(allowed_tools=("lookup",)),
        metadata={"domain": "test"},
    )
    result = ProviderResult[Answer](
        request_id=request.request_id,
        provider="fake",
        model="fixture",
        output=Answer(value=3),
    )

    assert result.output.value == 3
    assert request.tool_policy.allowed_tools == ("lookup",)
    with pytest.raises(ValidationError):
        request.prompt = "changed"  # ty: ignore[invalid-assignment]  # Verify frozen-model rejection.
    with pytest.raises(ValidationError):
        ToolPolicy(allowed_tools=["lookup"])  # ty: ignore[invalid-argument-type]  # Deliberately invalid input.
    with pytest.raises(ValidationError):
        AgentRequest[Answer](
            prompt="x",
            response_model=Answer,
            unknown=True,  # ty: ignore[unknown-argument]  # Deliberately unknown field.
        )


def test_contract_enums_expose_explicit_run_and_action_states() -> None:
    assert {mode.value for mode in ActionMode} == {"disabled", "dry_run", "apply"}
    assert RunStatus.DEGRADED.value == "degraded"
    assert RunStatus.CANCELLED.value == "cancelled"


def test_tool_policy_rejects_duplicate_or_untrimmed_tools() -> None:
    with pytest.raises(ValidationError, match="unique"):
        ToolPolicy(allowed_tools=("search", "search"))
    with pytest.raises(ValidationError, match="trimmed"):
        ToolPolicy(allowed_tools=(" search",))
