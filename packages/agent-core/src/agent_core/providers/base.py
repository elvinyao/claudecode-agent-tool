"""Domain-neutral asynchronous provider boundary."""

from __future__ import annotations

import asyncio
import math
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from agent_core.contracts import AgentRequest, ProviderResult
from agent_core.providers.capabilities import ProviderCapabilities
from agent_core.providers.errors import (
    ProviderConfigurationError,
    ProviderError,
    ProviderTimeoutError,
    classify_provider_exception,
)
from agent_core.skills import SkillSpec

OutputT = TypeVar("OutputT", bound=BaseModel)


@runtime_checkable
class ProviderAdapter(Protocol):
    """Execute exactly one typed model request.

    Batching, retries, fallback rules, and domain validation belong to the
    workflow engine and domain plugin rather than this transport boundary.
    """

    name: str
    model: str
    capabilities: ProviderCapabilities

    async def execute(
        self,
        request: AgentRequest[OutputT],
        *,
        timeout_seconds: float | None = None,
    ) -> ProviderResult[OutputT]: ...


class BaseProvider(ABC):
    """Capability preflight, timeout, cancellation, and error normalization."""

    name: str
    capabilities: ProviderCapabilities

    def __init__(
        self,
        *,
        model: str | None = None,
        skills: Sequence[SkillSpec] = (),
    ) -> None:
        if model is not None and (not model or model != model.strip()):
            raise ProviderConfigurationError("model must be non-empty and trimmed")
        skill_names = [skill.name for skill in skills]
        if len(set(skill_names)) != len(skill_names):
            raise ProviderConfigurationError("skill names must be unique")
        self._requested_model = model
        self.model = model or "provider-default"
        self.skills = tuple(skills)

    async def execute(
        self,
        request: AgentRequest[OutputT],
        *,
        timeout_seconds: float | None = None,
    ) -> ProviderResult[OutputT]:
        if timeout_seconds is not None and (
            timeout_seconds <= 0 or not math.isfinite(timeout_seconds)
        ):
            raise ProviderConfigurationError(
                "timeout_seconds must be greater than zero",
                provider=self.name,
            )
        self.capabilities.validate_request(
            request,
            provider=self.name,
            has_skills=bool(self.skills),
        )

        task = asyncio.create_task(self._execute(request))
        try:
            if timeout_seconds is None:
                return await task
            return await asyncio.wait_for(task, timeout=timeout_seconds)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        except asyncio.TimeoutError as exc:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            detail = (
                f" exceeded {timeout_seconds:g} seconds"
                if timeout_seconds is not None
                else " timed out"
            )
            raise ProviderTimeoutError(
                f"{self.name} request{detail}",
                provider=self.name,
            ) from exc
        except ProviderError:
            raise
        except Exception as exc:
            error = classify_provider_exception(exc, provider=self.name)
            raise error from exc

    @abstractmethod
    async def _execute(self, request: AgentRequest[OutputT]) -> ProviderResult[OutputT]:
        """Call one SDK request and return its validated output."""


__all__ = ["BaseProvider", "OutputT", "ProviderAdapter"]
