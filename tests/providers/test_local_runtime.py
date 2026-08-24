from __future__ import annotations

import os
from pathlib import Path

import pytest

import agent_core.providers.local_runtime as runtime_module
from agent_core.providers import ProviderConfigurationError
from agent_core.providers.local_runtime import resolve_local_executable


def executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_explicit_runtime_override_wins_over_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    override = executable(tmp_path / "configured-codex")
    path_entry = executable(tmp_path / "path-codex")
    monkeypatch.setenv("AGENT_CORE_CODEX_BIN", str(override))
    monkeypatch.setattr(runtime_module.shutil, "which", lambda name: str(path_entry))

    resolved = resolve_local_executable("codex", ("codex",))

    assert resolved == override.resolve()


def test_path_runtime_is_selected_before_sdk_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    local_claude = executable(tmp_path / "claude")
    monkeypatch.delenv("AGENT_CORE_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(runtime_module.shutil, "which", lambda name: str(local_claude))

    assert resolve_local_executable("claude", ("claude",)) == local_claude.resolve()


def test_missing_path_runtime_returns_none_for_sdk_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_CORE_ANTIGRAVITY_BIN", raising=False)
    monkeypatch.setattr(runtime_module.shutil, "which", lambda name: None)

    assert resolve_local_executable("antigravity", ("agy",)) is None


@pytest.mark.parametrize("value", ["", " relative/codex ", "relative/codex"])
def test_invalid_explicit_runtime_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("AGENT_CORE_CODEX_BIN", value)

    with pytest.raises(ProviderConfigurationError, match="AGENT_CORE_CODEX_BIN"):
        resolve_local_executable("codex", ("codex",))


def test_non_executable_override_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "agy"
    binary.write_text("not executable\n", encoding="utf-8")
    binary.chmod(0o644)
    monkeypatch.setenv("AGENT_CORE_ANTIGRAVITY_BIN", os.fspath(binary))

    with pytest.raises(ProviderConfigurationError, match="executable file"):
        resolve_local_executable("antigravity", ("agy",))
