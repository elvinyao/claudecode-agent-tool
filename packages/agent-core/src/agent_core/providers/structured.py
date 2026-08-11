"""Shared Pydantic validation for heterogeneous SDK response shapes."""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from agent_core.providers.errors import ProviderResponseError

OutputT = TypeVar("OutputT", bound=BaseModel)


def validate_structured_output(
    raw: Any,
    response_model: type[OutputT],
    *,
    provider: str,
) -> OutputT:
    """Validate JSON text, mappings, or an already validated model instance."""

    if isinstance(raw, response_model):
        return raw
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise ProviderResponseError(
            "provider returned no final structured output",
            provider=provider,
        )
    try:
        if isinstance(raw, (str, bytes, bytearray)):
            return response_model.model_validate_json(raw)
        return response_model.model_validate(raw)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ProviderResponseError(
            f"provider output does not match {response_model.__name__}",
            provider=provider,
        ) from exc


__all__ = ["validate_structured_output"]
