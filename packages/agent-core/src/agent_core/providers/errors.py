"""Typed provider failures and conservative SDK exception classification."""

from __future__ import annotations

from typing import Any, ClassVar

from agent_core.contracts import AgentCoreError


class ProviderError(AgentCoreError, RuntimeError):
    """Base error surfaced by provider adapters.

    ``retryable`` is metadata for the workflow engine. Adapters never retry on
    their own, which keeps retry budgets and idempotency decisions centralized.
    """

    code: ClassVar[str] = "provider_error"
    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        retry_after_seconds: float | None = None,
        public_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.retry_after_seconds = retry_after_seconds
        self.public_message = public_message or _PUBLIC_MESSAGES.get(
            self.code,
            "Provider request failed",
        )


class ProviderConfigurationError(ProviderError):
    """The adapter or request configuration is invalid."""

    code = "provider_configuration"


class ProviderCapabilityError(ProviderConfigurationError):
    """The request asks for a capability the adapter cannot safely expose."""

    code = "provider_capability"


class ProviderUnavailableError(ProviderError):
    """The optional SDK or local provider runtime is unavailable."""

    code = "provider_unavailable"


class ProviderAuthenticationError(ProviderError):
    """Provider credentials or login state are missing or rejected."""

    code = "provider_authentication"


class ProviderPermissionError(ProviderError):
    """The provider rejected the operation for authorization reasons."""

    code = "provider_permission"


class ProviderRateLimitError(ProviderError):
    """The provider temporarily rejected the request because of a quota limit."""

    code = "provider_rate_limit"
    retryable = True


class ProviderTransportError(ProviderError):
    """A transient connection or upstream service failure occurred."""

    code = "provider_transport"
    retryable = True


class ProviderTimeoutError(ProviderError):
    """One provider request exceeded its attempt timeout."""

    code = "provider_timeout"
    retryable = True


class ProviderResponseError(ProviderError):
    """The provider did not return the requested structured response."""

    code = "provider_response"


class ProviderExecutionError(ProviderError):
    """The provider failed for an unclassified, non-retryable reason."""

    code = "provider_execution"


_PUBLIC_MESSAGES = {
    "provider_error": "Provider request failed",
    "provider_configuration": "Provider configuration is invalid",
    "provider_capability": "Provider capability is not available",
    "provider_unavailable": "Provider SDK or local runtime is unavailable",
    "provider_authentication": "Provider authentication failed",
    "provider_permission": "Provider permission was denied",
    "provider_rate_limit": "Provider rate limit was exceeded",
    "provider_transport": "Provider transport failed",
    "provider_timeout": "Provider request timed out",
    "provider_response": "Provider returned an invalid structured response",
    "provider_execution": "Provider execution failed",
}


def _status_code(exc: BaseException) -> int | None:
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        if isinstance(candidate, int):
            return candidate
    return None


def _retry_after(exc: BaseException) -> float | None:
    direct = getattr(exc, "retry_after", None)
    if isinstance(direct, (int, float)) and direct >= 0:
        return float(direct)
    headers: Any = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None:
        try:
            value = headers.get("retry-after")
            if value is not None:
                return max(0.0, float(value))
        except (AttributeError, TypeError, ValueError):
            pass
    return None


def classify_provider_exception(exc: Exception, *, provider: str) -> ProviderError:
    """Translate common SDK error shapes without coupling to optional packages."""

    if isinstance(exc, ProviderError):
        return exc
    status = _status_code(exc)
    message = str(exc).strip() or exc.__class__.__name__
    normalized_name = exc.__class__.__name__.lower()

    if "invalid_json_schema" in message.casefold():
        return ProviderConfigurationError(
            "provider rejected the structured-output schema",
            provider=provider,
        )

    if status == 401:
        return ProviderAuthenticationError(message, provider=provider)
    if status == 403 or any(
        token in normalized_name for token in ("forbidden", "permission")
    ):
        return ProviderPermissionError(message, provider=provider)
    if any(token in normalized_name for token in ("auth", "credential")):
        return ProviderAuthenticationError(message, provider=provider)
    if status == 429 or "ratelimit" in normalized_name or "rate_limit" in normalized_name:
        return ProviderRateLimitError(
            message,
            provider=provider,
            retry_after_seconds=_retry_after(exc),
        )
    if isinstance(exc, (ConnectionError, TimeoutError)) or (
        status is not None and (status == 408 or status >= 500)
    ):
        return ProviderTransportError(message, provider=provider)
    return ProviderExecutionError(message, provider=provider)


__all__ = [
    "ProviderAuthenticationError",
    "ProviderCapabilityError",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderExecutionError",
    "ProviderPermissionError",
    "ProviderRateLimitError",
    "ProviderResponseError",
    "ProviderTimeoutError",
    "ProviderTransportError",
    "ProviderUnavailableError",
    "classify_provider_exception",
]
