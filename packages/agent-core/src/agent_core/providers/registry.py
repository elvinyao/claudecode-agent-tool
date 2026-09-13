"""Extensible provider factory without eager optional-SDK imports."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from agent_core.providers.antigravity import AntigravityProvider
from agent_core.providers.base import ProviderAdapter
from agent_core.providers.claude import ClaudeProvider
from agent_core.providers.codex import CodexProvider
from agent_core.providers.errors import ProviderConfigurationError
from agent_core.skills import SkillSpec

ProviderFactory = Callable[..., ProviderAdapter]
_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")


class ProviderRegistry:
    """Map normalized provider names to lazily executing adapter factories."""

    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))

    def register(self, name: str, factory: ProviderFactory) -> None:
        if not isinstance(name, str):
            raise ProviderConfigurationError("provider name must be a string")
        normalized = name.strip().lower()
        if _PROVIDER_NAME.fullmatch(normalized) is None:
            raise ProviderConfigurationError("provider name is invalid")
        if not callable(factory):
            raise ProviderConfigurationError("provider factory must be callable")
        if normalized in self._factories:
            raise ProviderConfigurationError(f"provider is already registered: {normalized}")
        self._factories[normalized] = factory

    def create(
        self,
        name: str,
        *,
        model: str | None = None,
        skills: Sequence[SkillSpec] = (),
    ) -> ProviderAdapter:
        if not isinstance(name, str):
            raise ProviderConfigurationError("provider name must be a string")
        normalized = name.strip().lower()
        try:
            factory = self._factories[normalized]
        except KeyError as exc:
            available = ", ".join(self.names) or "none"
            raise ProviderConfigurationError(
                f"unknown provider {name!r}; available providers: {available}"
            ) from exc
        return factory(model=model, skills=skills)


DEFAULT_PROVIDER_REGISTRY = ProviderRegistry()
DEFAULT_PROVIDER_REGISTRY.register("antigravity", AntigravityProvider)
DEFAULT_PROVIDER_REGISTRY.register("codex", CodexProvider)
DEFAULT_PROVIDER_REGISTRY.register("claude", ClaudeProvider)


def create_provider(
    name: str,
    *,
    model: str | None = None,
    skills: Sequence[SkillSpec] = (),
) -> ProviderAdapter:
    """Create one built-in adapter without importing its optional SDK."""

    return DEFAULT_PROVIDER_REGISTRY.create(name, model=model, skills=skills)


__all__ = [
    "DEFAULT_PROVIDER_REGISTRY",
    "ProviderFactory",
    "ProviderRegistry",
    "create_provider",
]
