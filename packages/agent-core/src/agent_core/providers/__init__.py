"""Domain-neutral Codex and Claude provider adapters."""

from agent_core.providers.base import BaseProvider, ProviderAdapter
from agent_core.providers.capabilities import ProviderCapabilities
from agent_core.providers.claude import ClaudeProvider
from agent_core.providers.codex import CodexProvider
from agent_core.providers.errors import (
    ProviderAuthenticationError,
    ProviderCapabilityError,
    ProviderConfigurationError,
    ProviderError,
    ProviderExecutionError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderTransportError,
    ProviderUnavailableError,
)
from agent_core.providers.registry import (
    DEFAULT_PROVIDER_REGISTRY,
    ProviderRegistry,
    create_provider,
)

__all__ = [
    "DEFAULT_PROVIDER_REGISTRY",
    "BaseProvider",
    "ClaudeProvider",
    "CodexProvider",
    "ProviderAdapter",
    "ProviderAuthenticationError",
    "ProviderCapabilities",
    "ProviderCapabilityError",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderExecutionError",
    "ProviderPermissionError",
    "ProviderRateLimitError",
    "ProviderRegistry",
    "ProviderResponseError",
    "ProviderTimeoutError",
    "ProviderTransportError",
    "ProviderUnavailableError",
    "create_provider",
]
