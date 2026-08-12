from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

import agent_core.providers.codex as codex_module
from agent_core.contracts import AgentRequest
from agent_core.providers import CodexProvider, ProviderConfigurationError
from agent_core.providers.codex_schema import validate_codex_output_schema

VALID_NESTED_SCHEMA: dict[str, Any] = {
    "$defs": {
        "Detail": {
            "additionalProperties": False,
            "properties": {
                "answer": {"type": "string"},
                "source": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
            "required": ["answer", "source"],
            "type": "object",
        }
    },
    "additionalProperties": False,
    "properties": {"detail": {"$ref": "#/$defs/Detail"}},
    "required": ["detail"],
    "type": "object",
}


def test_codex_schema_preflight_accepts_nested_defs() -> None:
    validate_codex_output_schema(VALID_NESTED_SCHEMA)


@pytest.mark.parametrize(
    "required",
    [
        [],
        ["detail", "undeclared"],
        ["detail", "detail"],
    ],
)
def test_codex_schema_preflight_requires_exact_root_property_coverage(
    required: list[str],
) -> None:
    schema = deepcopy(VALID_NESTED_SCHEMA)
    schema["required"] = required

    with pytest.raises(ProviderConfigurationError, match="exactly match") as exc_info:
        validate_codex_output_schema(schema)

    assert exc_info.value.code == "provider_configuration"
    assert exc_info.value.public_message == "Provider configuration is invalid"


def test_codex_schema_preflight_requires_required_in_nested_defs() -> None:
    schema = deepcopy(VALID_NESTED_SCHEMA)
    del schema["$defs"]["Detail"]["required"]

    with pytest.raises(ProviderConfigurationError, match="define required"):
        validate_codex_output_schema(schema)


def test_codex_schema_preflight_rejects_open_additional_properties() -> None:
    schema = deepcopy(VALID_NESTED_SCHEMA)
    schema["$defs"]["Detail"]["additionalProperties"] = True

    with pytest.raises(ProviderConfigurationError, match="additionalProperties"):
        validate_codex_output_schema(schema)


class InvalidNestedDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    optional_source: str | None = None


class InvalidNestedOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: InvalidNestedDetail


@pytest.mark.asyncio
async def test_codex_schema_preflight_runs_before_sdk_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported = False

    def should_not_import(name: str) -> Any:
        nonlocal imported
        imported = True
        raise AssertionError(name)

    monkeypatch.setattr(codex_module, "import_module", should_not_import)
    request = AgentRequest[InvalidNestedOutput](
        prompt="Do not expose this prompt.",
        response_model=InvalidNestedOutput,
    )

    with pytest.raises(ProviderConfigurationError, match="exactly match"):
        await CodexProvider().execute(request)

    assert imported is False
