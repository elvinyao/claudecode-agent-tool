from __future__ import annotations

import asyncio
import json
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

import agent_core.providers.antigravity as antigravity_module
from agent_core.contracts import AgentRequest, ToolPolicy
from agent_core.providers import (
    AntigravityProvider,
    ProviderCapabilityError,
    ProviderUnavailableError,
)


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


@pytest.mark.asyncio
async def test_local_agy_cli_is_preferred_and_uses_headless_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, *, input: bytes) -> tuple[bytes, bytes]:
            state["input"] = input
            return (
                b'{"event":"init","init":{}}\n'
                b'{"event":"result","result":{"status":"SUCCESS",'
                b'"structured_output":{"answer":"local-agy"}}}\n',
                b"",
            )

    async def create_process(*command: str, **kwargs: Any) -> FakeProcess:
        state["command"] = command
        state["cwd"] = Path(kwargs["cwd"])
        state["agents"] = (state["cwd"] / "AGENTS.md").read_text(encoding="utf-8")
        state["schema"] = json.loads(
            (state["cwd"] / "response-schema.json").read_text(encoding="utf-8")
        )
        return FakeProcess()

    monkeypatch.setattr(
        antigravity_module,
        "resolve_local_executable",
        lambda *args: Path("/opt/local/bin/agy"),
    )
    monkeypatch.setattr(
        antigravity_module,
        "import_module",
        lambda name: (_ for _ in ()).throw(AssertionError(name)),
    )
    monkeypatch.setattr(antigravity_module.asyncio, "create_subprocess_exec", create_process)

    result = await AntigravityProvider(model="gemini-test").execute(request())

    assert result.output == DemoOutput(answer="local-agy")
    assert state["command"][0] == "/opt/local/bin/agy"
    assert "--input-format" in state["command"]
    assert "--output-format" in state["command"]
    assert "--json-schema" in state["command"]
    assert "--sandbox" in state["command"]
    assert state["command"][-2:] == ("--model", "gemini-test")
    sent = json.loads(state["input"])
    assert sent["message"]["content"] == "Answer this Antigravity request."
    assert "do not mutate" in state["agents"]
    assert "Do not access the web" in state["agents"]
    assert state["schema"]["properties"]["answer"]
    assert state["cwd"].exists() is False


@pytest.mark.asyncio
async def test_python_sdk_fallback_has_explicit_safe_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {}

    class FakeCapabilities:
        def __init__(self, **kwargs: Any) -> None:
            state["capabilities"] = kwargs

    class FakeConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.values = kwargs
            state["config"] = kwargs

    class FakeResponse:
        async def structured_output(self) -> dict[str, str]:
            return {"answer": "sdk-fallback"}

    class FakeAgent:
        def __init__(self, config: FakeConfig) -> None:
            self.config = config

        async def __aenter__(self) -> FakeAgent:
            state["workspace_exists"] = Path(self.config.values["workspaces"][0]).is_dir()
            return self

        async def __aexit__(self, *args: Any) -> None:
            state["closed"] = True

        async def chat(self, prompt: str) -> FakeResponse:
            state["prompt"] = prompt
            return FakeResponse()

    class FakeBuiltinTools(str, Enum):
        FINISH = "finish"
        SEARCH_WEB = "search-web"
        READ_URL_CONTENT = "read-url"

    sdk = SimpleNamespace(
        Agent=FakeAgent,
        BuiltinTools=FakeBuiltinTools,
        CapabilitiesConfig=FakeCapabilities,
        LocalAgentConfig=FakeConfig,
    )
    policy = SimpleNamespace(
        deny_all=lambda: "deny-all",
        allow=lambda tool: f"allow:{tool}",
    )
    monkeypatch.setattr(antigravity_module, "resolve_local_executable", lambda *args: None)
    monkeypatch.setattr(
        antigravity_module,
        "import_module",
        lambda name: policy if name.endswith(".policy") else sdk,
    )

    result = await AntigravityProvider(model="gemini-sdk").execute(request(web=True))

    assert result.output.answer == "sdk-fallback"
    assert state["capabilities"] == {
        "enable_subagents": False,
        "enabled_tools": ["finish", "search-web", "read-url"],
    }
    assert state["config"]["mcp_servers"] == []
    assert state["config"]["tools"] == []
    assert state["config"]["subagents"] == []
    assert state["config"]["policies"] == [
        "deny-all",
        "allow:finish",
        "allow:search-web",
        "allow:read-url",
    ]
    assert state["config"]["model"] == "gemini-sdk"
    assert state["config"]["response_schema"]["properties"]["answer"]
    assert state["workspace_exists"] is True
    assert state["closed"] is True
    assert Path(state["config"]["workspaces"][0]).exists() is False


@pytest.mark.asyncio
async def test_local_cli_cancellation_terminates_process_and_cleans_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    state: dict[str, Any] = {}

    class FakeProcess:
        returncode: int | None = None

        async def communicate(self, *, input: bytes) -> tuple[bytes, bytes]:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        def terminate(self) -> None:
            state["terminated"] = True
            self.returncode = -15

        async def wait(self) -> int:
            return self.returncode or 0

    async def create_process(*command: str, **kwargs: Any) -> FakeProcess:
        state["cwd"] = Path(kwargs["cwd"])
        return FakeProcess()

    monkeypatch.setattr(
        antigravity_module,
        "resolve_local_executable",
        lambda *args: Path("/opt/local/bin/agy"),
    )
    monkeypatch.setattr(antigravity_module.asyncio, "create_subprocess_exec", create_process)

    task = asyncio.create_task(AntigravityProvider().execute(request()))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert state["terminated"] is True
    assert state["cwd"].exists() is False


@pytest.mark.asyncio
async def test_missing_local_cli_and_sdk_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(antigravity_module, "resolve_local_executable", lambda *args: None)

    def missing(name: str) -> Any:
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(antigravity_module, "import_module", missing)

    with pytest.raises(ProviderUnavailableError, match=r"agent-core\[antigravity\]"):
        await AntigravityProvider().execute(request())


@pytest.mark.asyncio
async def test_unsafe_tool_is_rejected_before_runtime_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = AgentRequest[DemoOutput](
        prompt="test",
        response_model=DemoOutput,
        tool_policy=ToolPolicy(allowed_tools=("run_command",)),
    )
    monkeypatch.setattr(
        antigravity_module,
        "resolve_local_executable",
        lambda *args: (_ for _ in ()).throw(AssertionError("runtime discovery called")),
    )

    with pytest.raises(ProviderCapabilityError, match="run_command"):
        await AntigravityProvider().execute(item)
