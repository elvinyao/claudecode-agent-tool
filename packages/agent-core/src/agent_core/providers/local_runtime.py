"""Resolve configured local provider executables before SDK-bundled runtimes."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from agent_core.providers.errors import ProviderConfigurationError

_BINARY_ENV_VARS = {
    "antigravity": "AGENT_CORE_ANTIGRAVITY_BIN",
    "claude": "AGENT_CORE_CLAUDE_BIN",
    "codex": "AGENT_CORE_CODEX_BIN",
}


def _validated_executable(path: Path, *, provider: str, source: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ProviderConfigurationError(
            f"{source} does not point to an existing executable",
            provider=provider,
        ) from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ProviderConfigurationError(
            f"{source} does not point to an executable file",
            provider=provider,
        )
    return resolved


def resolve_local_executable(
    provider: str,
    candidates: tuple[str, ...],
) -> Path | None:
    """Return an explicit/PATH executable, or ``None`` for SDK fallback.

    A provider-specific environment override wins over PATH. Invalid explicit
    overrides fail closed rather than silently selecting a different runtime.
    """

    try:
        env_var = _BINARY_ENV_VARS[provider]
    except KeyError as exc:
        raise ProviderConfigurationError(
            f"local runtime discovery is not configured for provider {provider!r}",
            provider=provider,
        ) from exc

    override = os.environ.get(env_var)
    if override is not None:
        if not override or override != override.strip():
            raise ProviderConfigurationError(
                f"{env_var} must be a non-empty, trimmed absolute path",
                provider=provider,
            )
        override_path = Path(override).expanduser()
        if not override_path.is_absolute():
            raise ProviderConfigurationError(
                f"{env_var} must be an absolute path",
                provider=provider,
            )
        return _validated_executable(
            override_path,
            provider=provider,
            source=env_var,
        )

    for candidate in candidates:
        discovered = shutil.which(candidate)
        if discovered is None:
            continue
        return _validated_executable(
            Path(discovered),
            provider=provider,
            source=f"PATH entry for {candidate}",
        )
    return None


__all__ = ["resolve_local_executable"]
