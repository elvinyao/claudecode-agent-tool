from __future__ import annotations

from importlib import metadata
from pathlib import Path

import pytest

from agent_core.providers import diagnostics
from agent_core.providers.diagnostics import ProviderReadiness
from agent_core.providers.errors import ProviderConfigurationError


def test_antigravity_cli_is_standalone_without_exposing_absolute_path(
    tmp_path,
    monkeypatch,
) -> None:
    executable = tmp_path / "agy"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setenv("AGENT_CORE_ANTIGRAVITY_BIN", str(executable))

    result = diagnostics.diagnose_provider_runtime("antigravity")

    assert result.status is ProviderReadiness.READY
    assert result.runtime == "cli"
    assert result.detail == (
        "agy executable is available via AGENT_CORE_ANTIGRAVITY_BIN."
    )
    assert str(tmp_path) not in result.detail


def test_codex_cli_is_reported_only_with_required_sdk(monkeypatch) -> None:
    monkeypatch.setattr(
        diagnostics,
        "resolve_local_executable",
        lambda *_args: Path("/usr/local/bin/codex"),
    )
    monkeypatch.setattr(diagnostics.metadata, "version", lambda _name: "0.144.4")

    result = diagnostics.diagnose_provider_runtime("codex")

    assert result.status is ProviderReadiness.READY
    assert result.runtime == "sdk+cli"
    assert result.detail == (
        "openai-codex 0.144.4 is installed; codex is available via PATH."
    )


@pytest.mark.parametrize(
    ("provider", "distribution", "extra", "environment_variable", "executable_name"),
    (
        (
            "claude",
            "claude-agent-sdk",
            "claude",
            "AGENT_CORE_CLAUDE_BIN",
            "claude",
        ),
        (
            "codex",
            "openai-codex",
            "codex",
            "AGENT_CORE_CODEX_BIN",
            "codex",
        ),
    ),
)
def test_auxiliary_cli_without_required_sdk_is_unavailable(
    provider: str,
    distribution: str,
    extra: str,
    environment_variable: str,
    executable_name: str,
    tmp_path,
    monkeypatch,
) -> None:
    executable = tmp_path / executable_name
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setenv(environment_variable, str(executable))

    def missing_distribution(name: str) -> str:
        assert name == distribution
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(diagnostics.metadata, "version", missing_distribution)

    result = diagnostics.diagnose_provider_runtime(provider)

    assert result.status is ProviderReadiness.UNAVAILABLE
    assert result.runtime == "none"
    assert result.detail == f"Install agent-core[{extra}]; the Python SDK is required."


def test_diagnostic_uses_installed_sdk_when_no_cli_is_available(monkeypatch) -> None:
    monkeypatch.setattr(diagnostics, "resolve_local_executable", lambda *_args: None)
    monkeypatch.setattr(
        diagnostics.metadata,
        "version",
        lambda distribution: "0.2.130" if distribution == "claude-agent-sdk" else "",
    )

    result = diagnostics.diagnose_provider_runtime("claude")

    assert result.status is ProviderReadiness.READY
    assert result.runtime == "sdk"
    assert result.detail == "claude-agent-sdk 0.2.130 is installed."


def test_diagnostic_reports_missing_runtime_with_actionable_install_hint(
    monkeypatch,
) -> None:
    monkeypatch.setattr(diagnostics, "resolve_local_executable", lambda *_args: None)

    def missing_distribution(distribution: str) -> str:
        raise metadata.PackageNotFoundError(distribution)

    monkeypatch.setattr(diagnostics.metadata, "version", missing_distribution)

    result = diagnostics.diagnose_provider_runtime("antigravity")

    assert result.status is ProviderReadiness.UNAVAILABLE
    assert result.runtime == "none"
    assert result.detail == (
        "Install agent-core[antigravity] or make agy available on PATH."
    )


def test_diagnostic_fails_closed_for_invalid_explicit_override(monkeypatch) -> None:
    def invalid_override(*_args):
        raise ProviderConfigurationError(
            "AGENT_CORE_CLAUDE_BIN does not point to an existing executable",
            provider="claude",
        )

    monkeypatch.setattr(diagnostics, "resolve_local_executable", invalid_override)
    monkeypatch.setattr(diagnostics.metadata, "version", lambda _name: "1.0")

    result = diagnostics.diagnose_provider_runtime("claude")

    assert result.status is ProviderReadiness.MISCONFIGURED
    assert result.runtime == "cli"
    assert result.detail == (
        "AGENT_CORE_CLAUDE_BIN does not point to an existing executable"
    )


def test_custom_provider_is_explicitly_unchecked() -> None:
    result = diagnostics.diagnose_provider_runtime("custom")

    assert result.status is ProviderReadiness.UNCHECKED
    assert result.runtime == "custom"
    assert "no offline runtime check" in result.detail
