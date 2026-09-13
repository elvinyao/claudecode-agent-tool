from __future__ import annotations

import asyncio
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

import agent_core.providers.antigravity as antigravity_module
from agent_core.contracts import AgentRequest, ToolPolicy
from agent_core.providers import (
    AntigravityProvider,
    ProviderCapabilityError,
    ProviderConfigurationError,
    ProviderUnavailableError,
)
from agent_core.skills import load_skill


class DemoOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str


def request(*, web: bool = False) -> AgentRequest[DemoOutput]:
    return AgentRequest[DemoOutput](
        request_id="antigravity-test",
        system_prompt="Return a typed answer and do not mutate the workspace.",
        prompt="Answer this Antigravity request.",
        response_model=DemoOutput,
        tool_policy=ToolPolicy(web_access=web),
    )


@pytest.fixture(autouse=True)
def no_retired_override(monkeypatch):
    monkeypatch.delenv("AGENT_CORE_ANTIGRAVITY_BIN", raising=False)


@pytest.fixture
def sdk_agent(monkeypatch):
    # Use real SDK configuration and policies, replacing only the remote session.
    sdk = import_module("google.antigravity")
    state: dict[str, Any] = {"started": asyncio.Event(), "block": False}

    class Response:
        async def structured_output(self):
            return {"answer": "sdk"}

    class Agent:
        def __init__(self, config):
            state["config"] = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            state["closed"] = True

        async def chat(self, prompt):
            state["prompt"] = prompt
            state["started"].set()
            if state["block"]:
                await asyncio.Event().wait()
            return Response()

    monkeypatch.setattr(sdk, "Agent", Agent)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("web", [False, True])
async def test_sdk_enforces_tool_policy_even_with_agy_on_path(
    web, sdk_agent, monkeypatch, tmp_path
):
    agy = tmp_path / "agy"
    agy.write_text("#!/bin/sh\nexit 99\n")
    agy.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    result = await AntigravityProvider().execute(request(web=web))
    assert result.output.answer == "sdk"

    config = sdk_agent["config"]
    assert config.capabilities.enable_subagents is False
    assert not config.mcp_servers
    assert not config.tools
    assert not config.subagents
    allowed = {tool.value for tool in config.capabilities.enabled_tools}
    assert allowed == ({"finish", "search_web", "read_url_content"} if web else {"finish"})

    policy = import_module("google.antigravity.hooks.policy")
    hooks = import_module("google.antigravity.hooks")
    types = import_module("google.antigravity.types")
    guard = policy.enforce(config.policies)
    for name in ("run_command", "write_to_file", "start_subagent", "custom_tool", "mcp_tool"):
        decision = await guard.run(hooks.HookContext(), types.ToolCall(name=name))
        assert decision.allow is False, name
    for name in ("search_web", "read_url_content", "finish"):
        decision = await guard.run(hooks.HookContext(), types.ToolCall(name=name))
        assert decision.allow is (name == "finish" or web), name
    assert sdk_agent["closed"]
    assert not Path(config.workspaces[0]).exists()


@pytest.mark.asyncio
async def test_sdk_stages_selected_skills(sdk_agent, tmp_path):
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: Test guidance.\n---\nRead the prompt.\n"
    )
    await AntigravityProvider(skills=(load_skill(skill_dir),)).execute(request())
    config = sdk_agent["config"]
    assert len(config.skills_paths) == 1
    assert config.skills_paths[0].endswith("/.agents/skills/test-skill")
    assert sdk_agent["prompt"].startswith("/test-skill\n\n")


@pytest.mark.asyncio
async def test_sdk_cancellation_closes_session_and_workspace(sdk_agent):
    sdk_agent["block"] = True
    task = asyncio.create_task(AntigravityProvider().execute(request()))
    await sdk_agent["started"].wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sdk_agent["closed"]
    assert not Path(sdk_agent["config"].workspaces[0]).exists()


@pytest.mark.asyncio
async def test_missing_sdk_does_not_fall_back_to_cli(monkeypatch, tmp_path):
    agy = tmp_path / "agy"
    agy.write_text("#!/bin/sh\nexit 99\n")
    agy.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    def missing(name):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(antigravity_module, "import_module", missing)
    with pytest.raises(ProviderUnavailableError, match=r"agent-core\[antigravity\]"):
        await AntigravityProvider().execute(request())


@pytest.mark.asyncio
async def test_retired_cli_override_has_explicit_migration_error(monkeypatch):
    monkeypatch.setenv("AGENT_CORE_ANTIGRAVITY_BIN", "/private/secret/agy")
    with pytest.raises(ProviderConfigurationError, match="no longer supported") as error:
        await AntigravityProvider().execute(request())
    assert "/private/secret" not in str(error.value)


@pytest.mark.asyncio
async def test_unsafe_tool_is_rejected_before_sdk_import(monkeypatch):
    def unexpected(name):
        raise AssertionError(name)

    monkeypatch.setattr(antigravity_module, "import_module", unexpected)
    with pytest.raises(ProviderCapabilityError, match="run_command"):
        await AntigravityProvider().execute(
            AgentRequest[DemoOutput](
                prompt="test",
                response_model=DemoOutput,
                tool_policy=ToolPolicy(allowed_tools=("run_command",)),
            )
        )
