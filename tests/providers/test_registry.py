from __future__ import annotations

import pytest

from agent_core.providers import (
    ClaudeProvider,
    CodexProvider,
    ProviderConfigurationError,
    ProviderRegistry,
    create_provider,
)


def test_builtin_registry_is_lazy_and_normalizes_names() -> None:
    assert isinstance(create_provider(" CODEX "), CodexProvider)
    assert isinstance(create_provider("claude", model="model-x"), ClaudeProvider)


def test_builtin_registry_rejects_unknown_provider() -> None:
    with pytest.raises(ProviderConfigurationError, match="claude, codex"):
        create_provider("unknown")


@pytest.mark.parametrize("name", ["", "has space", "../escape", "a" * 65])
def test_registry_rejects_invalid_provider_names(name: str) -> None:
    registry = ProviderRegistry()
    with pytest.raises(ProviderConfigurationError, match="provider name"):
        registry.register(name, CodexProvider)

    with pytest.raises(ProviderConfigurationError, match="string"):
        registry.create(None)  # type: ignore[arg-type]
    with pytest.raises(ProviderConfigurationError, match="callable"):
        registry.register("valid", None)  # type: ignore[arg-type]
