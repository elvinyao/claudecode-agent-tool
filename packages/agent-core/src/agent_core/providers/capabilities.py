"""Provider capability declarations and request preflight checks."""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_core.contracts import AgentRequest
from agent_core.providers.errors import ProviderCapabilityError


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Security-relevant features implemented by one adapter."""

    structured_output: bool = True
    skills: bool = True
    web_access: bool = False
    read_only_workspace: bool = True
    allowed_tools: frozenset[str] = field(default_factory=frozenset)

    def validate_request(
        self,
        request: AgentRequest,
        *,
        provider: str,
        has_skills: bool,
    ) -> None:
        """Reject unsupported capabilities before importing or calling an SDK."""

        policy = request.tool_policy
        if not self.structured_output:
            raise ProviderCapabilityError(
                "provider does not support structured output",
                provider=provider,
            )
        if has_skills and not self.skills:
            raise ProviderCapabilityError("provider does not support skills", provider=provider)
        if policy.web_access and not self.web_access:
            raise ProviderCapabilityError(
                "provider does not support controlled web access",
                provider=provider,
            )

        requested_tools = frozenset(policy.allowed_tools)
        unsupported = sorted(requested_tools - self.allowed_tools)
        if unsupported:
            raise ProviderCapabilityError(
                f"provider does not expose requested tools: {', '.join(unsupported)}",
                provider=provider,
            )


__all__ = ["ProviderCapabilities"]
