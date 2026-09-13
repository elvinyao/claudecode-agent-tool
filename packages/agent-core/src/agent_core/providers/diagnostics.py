"""Offline provider-runtime diagnostics for operators and deployment checks."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from importlib import metadata
from pathlib import Path

from agent_core.providers.errors import ProviderConfigurationError
from agent_core.providers.local_runtime import resolve_local_executable


class ProviderReadiness(str, Enum):
    """Result of an offline provider runtime check."""

    READY = "ready"
    UNAVAILABLE = "unavailable"
    MISCONFIGURED = "misconfigured"
    UNCHECKED = "unchecked"


@dataclass(frozen=True, slots=True)
class ProviderDiagnostic:
    """Secret-free diagnostic metadata for one registered provider."""

    name: str
    status: ProviderReadiness
    runtime: str
    detail: str

    @property
    def ready(self) -> bool:
        return self.status is ProviderReadiness.READY


@dataclass(frozen=True, slots=True)
class _RuntimeSpec:
    executable_candidates: tuple[str, ...]
    environment_variable: str
    distribution: str
    install_extra: str
    standalone_cli: bool


_RUNTIME_SPECS = {
    "antigravity": _RuntimeSpec(
        executable_candidates=("agy",),
        environment_variable="AGENT_CORE_ANTIGRAVITY_BIN",
        distribution="google-antigravity",
        install_extra="antigravity",
        standalone_cli=True,
    ),
    "claude": _RuntimeSpec(
        executable_candidates=("claude",),
        environment_variable="AGENT_CORE_CLAUDE_BIN",
        distribution="claude-agent-sdk",
        install_extra="claude",
        standalone_cli=False,
    ),
    "codex": _RuntimeSpec(
        executable_candidates=("codex",),
        environment_variable="AGENT_CORE_CODEX_BIN",
        distribution="openai-codex",
        install_extra="codex",
        standalone_cli=False,
    ),
}


def diagnose_provider_runtime(provider: str) -> ProviderDiagnostic:
    """Check local executable/SDK availability without importing or calling it.

    Authentication and network access are deliberately outside this check. The
    check follows each adapter's runtime-resolution order, and an invalid
    executable override is never silently replaced after its required runtime
    prerequisites are present.
    """

    normalized = provider.strip().lower()
    spec = _RUNTIME_SPECS.get(normalized)
    if spec is None:
        return ProviderDiagnostic(
            name=normalized,
            status=ProviderReadiness.UNCHECKED,
            runtime="custom",
            detail="Registered custom provider; no offline runtime check is defined.",
        )

    if spec.standalone_cli:
        return _diagnose_cli_or_sdk(normalized, spec)
    return _diagnose_sdk_with_auxiliary_cli(normalized, spec)


def _diagnose_cli_or_sdk(provider: str, spec: _RuntimeSpec) -> ProviderDiagnostic:
    executable_or_error = _resolve_executable(provider, spec)
    if isinstance(executable_or_error, ProviderDiagnostic):
        return executable_or_error
    executable = executable_or_error
    if executable is not None:
        return ProviderDiagnostic(
            name=provider,
            status=ProviderReadiness.READY,
            runtime="cli",
            detail=(f"{executable.name} executable is available via {_executable_source(spec)}."),
        )

    version = _installed_version(spec.distribution)
    if version is None:
        candidates = "/".join(spec.executable_candidates)
        return ProviderDiagnostic(
            name=provider,
            status=ProviderReadiness.UNAVAILABLE,
            runtime="none",
            detail=(
                f"Install agent-core[{spec.install_extra}] or make {candidates} available on PATH."
            ),
        )
    return _sdk_ready(provider, spec, version)


def _diagnose_sdk_with_auxiliary_cli(
    provider: str,
    spec: _RuntimeSpec,
) -> ProviderDiagnostic:
    # ClaudeProvider and CodexProvider import and validate their Python SDK
    # before resolving the SDK's optional local executable path. Mirror that
    # order here so a standalone CLI is never presented as runnable.
    version = _installed_version(spec.distribution)
    if version is None:
        return ProviderDiagnostic(
            name=provider,
            status=ProviderReadiness.UNAVAILABLE,
            runtime="none",
            detail=(f"Install agent-core[{spec.install_extra}]; the Python SDK is required."),
        )

    executable_or_error = _resolve_executable(provider, spec)
    if isinstance(executable_or_error, ProviderDiagnostic):
        return executable_or_error
    executable = executable_or_error
    if executable is None:
        return _sdk_ready(provider, spec, version)
    return ProviderDiagnostic(
        name=provider,
        status=ProviderReadiness.READY,
        runtime="sdk+cli",
        detail=(
            f"{spec.distribution} {version} is installed; {executable.name} "
            f"is available via {_executable_source(spec)}."
        ),
    )


def _resolve_executable(
    provider: str,
    spec: _RuntimeSpec,
) -> Path | ProviderDiagnostic | None:
    try:
        return resolve_local_executable(provider, spec.executable_candidates)
    except ProviderConfigurationError as exc:
        return ProviderDiagnostic(
            name=provider,
            status=ProviderReadiness.MISCONFIGURED,
            runtime="cli",
            detail=str(exc),
        )


def _installed_version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def _executable_source(spec: _RuntimeSpec) -> str:
    return spec.environment_variable if spec.environment_variable in os.environ else "PATH"


def _sdk_ready(
    provider: str,
    spec: _RuntimeSpec,
    version: str,
) -> ProviderDiagnostic:
    return ProviderDiagnostic(
        name=provider,
        status=ProviderReadiness.READY,
        runtime="sdk",
        detail=f"{spec.distribution} {version} is installed.",
    )


__all__ = [
    "ProviderDiagnostic",
    "ProviderReadiness",
    "diagnose_provider_runtime",
]
