from __future__ import annotations

import asyncio
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

import agent_core.providers.claude as claude_module
import agent_core.providers.codex as codex_module
from agent_core.contracts import AgentRequest, ToolPolicy
from agent_core.providers import (
    ClaudeProvider,
    CodexProvider,
    ProviderAuthenticationError,
    ProviderCapabilityError,
    ProviderExecutionError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTransportError,
    ProviderUnavailableError,
)
from agent_core.providers.local_runtime import resolve_local_executable
from agent_core.skills import load_skill


class DemoOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str


def request(*, web: bool = False) -> AgentRequest[DemoOutput]:
    return AgentRequest[DemoOutput](
        request_id="adapter-test",
        system_prompt="System instructions from the plugin.",
        prompt="User prompt from the plugin.",
        response_model=DemoOutput,
        tool_policy=ToolPolicy(web_access=web),
    )


@pytest.fixture(autouse=True)
def no_local_provider_executables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_module, "resolve_local_executable", lambda *args: None)
    monkeypatch.setattr(claude_module, "resolve_local_executable", lambda *args: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_override", [False, True])
async def test_codex_forwards_generic_contract_and_enforces_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    explicit_override: bool,
) -> None:
    state: dict[str, Any] = {}

    class FakeConfig:
        def __init__(self, **kwargs: Any) -> None:
            state["config"] = kwargs

    class FakeThread:
        async def run(self, run_input: Any, **kwargs: Any) -> Any:
            state["run"] = (run_input, kwargs)
            return SimpleNamespace(final_response='{"answer":"codex"}', model="codex-model")

    class FakeCodex:
        def __init__(self, config: Any) -> None:
            state["client_config"] = config

        async def __aenter__(self) -> FakeCodex:
            state["cwd_exists_during_call"] = Path(state["config"]["cwd"]).is_dir()
            return self

        async def __aexit__(self, *args: Any) -> None:
            state["closed"] = True

        async def thread_start(self, **kwargs: Any) -> FakeThread:
            state["thread"] = kwargs
            return FakeThread()

    sdk = SimpleNamespace(
        AsyncCodex=FakeCodex,
        ApprovalMode=SimpleNamespace(deny_all="deny-all"),
        CodexConfig=FakeConfig,
        Sandbox=SimpleNamespace(read_only="read-only"),
    )
    monkeypatch.setattr(codex_module, "import_module", lambda name: sdk)
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nexit 99\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("AGENT_CORE_CODEX_BIN", raising=False)
    if explicit_override:
        monkeypatch.setenv("AGENT_CORE_CODEX_BIN", str(executable))
    monkeypatch.setattr(codex_module, "resolve_local_executable", resolve_local_executable)

    result = await CodexProvider(model="codex-model").execute(request(web=True))

    assert result.output == DemoOutput(answer="codex")
    assert result.request_id == "adapter-test"
    assert result.model == "codex-model"
    if explicit_override:
        assert state["config"]["codex_bin"] == str(executable.resolve())
    else:
        assert "codex_bin" not in state["config"]
    assert state["config"]["config_overrides"] == ('web_search="live"',)
    assert state["thread"]["developer_instructions"].startswith("System instructions")
    assert state["thread"]["approval_mode"] == "deny-all"
    assert state["thread"]["sandbox"] == "read-only"
    assert state["run"][0] == "User prompt from the plugin."
    assert state["run"][1]["approval_mode"] == "deny-all"
    assert state["run"][1]["sandbox"] == "read-only"
    assert state["run"][1]["output_schema"]["properties"]["answer"]
    assert state["cwd_exists_during_call"] is True
    assert state["closed"] is True
    assert Path(state["config"]["cwd"]).exists() is False


@pytest.mark.asyncio
async def test_codex_rejects_missing_or_incompatible_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> Any:
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(codex_module, "import_module", missing)
    with pytest.raises(ProviderUnavailableError, match=r"agent-core\[codex\]"):
        await CodexProvider().execute(request())


@pytest.mark.asyncio
async def test_codex_rejects_malformed_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeThread:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(final_response='{"wrong":true}')

    class FakeCodex:
        def __init__(self, config: Any) -> None:
            pass

        async def __aenter__(self) -> FakeCodex:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def thread_start(self, **kwargs: Any) -> FakeThread:
            return FakeThread()

    sdk = SimpleNamespace(
        AsyncCodex=FakeCodex,
        ApprovalMode=SimpleNamespace(deny_all="deny-all"),
        CodexConfig=lambda **kwargs: kwargs,
        Sandbox=SimpleNamespace(read_only="read-only"),
    )
    monkeypatch.setattr(codex_module, "import_module", lambda name: sdk)

    with pytest.raises(ProviderResponseError, match="DemoOutput"):
        await CodexProvider().execute(request())


@pytest.mark.asyncio
async def test_codex_cancellation_closes_sdk_and_removes_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {"started": asyncio.Event()}

    class FakeThread:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            state["started"].set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    class FakeCodex:
        def __init__(self, config: Any) -> None:
            pass

        async def __aenter__(self) -> FakeCodex:
            return self

        async def __aexit__(self, *args: Any) -> None:
            state["closed"] = True

        async def thread_start(self, **kwargs: Any) -> FakeThread:
            return FakeThread()

    def config(**kwargs: Any) -> Any:
        state["cwd"] = Path(kwargs["cwd"])
        return kwargs

    sdk = SimpleNamespace(
        AsyncCodex=FakeCodex,
        ApprovalMode=SimpleNamespace(deny_all="deny-all"),
        CodexConfig=config,
        Sandbox=SimpleNamespace(read_only="read-only"),
    )
    monkeypatch.setattr(codex_module, "import_module", lambda name: sdk)
    task = asyncio.create_task(CodexProvider().execute(request()))
    await state["started"].wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert state["closed"] is True
    assert state["cwd"].exists() is False


@pytest.mark.asyncio
async def test_claude_exposes_only_explicit_safe_tools_and_closes_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {}

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            state["options"] = kwargs

    class FakeResult:
        is_error = False
        subtype = "success"

        def __init__(self) -> None:
            self.structured_output = {"answer": "claude"}

    class FakeStream:
        def __init__(self) -> None:
            self.sent = False

        def __aiter__(self) -> FakeStream:
            return self

        async def __anext__(self) -> FakeResult:
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return FakeResult()

        async def aclose(self) -> None:
            state["stream_closed"] = True

    def query(*, prompt: str, options: Any) -> FakeStream:
        state["prompt"] = prompt
        state["passed_options"] = options
        return FakeStream()

    sdk = SimpleNamespace(
        ClaudeAgentOptions=FakeOptions,
        ResultMessage=FakeResult,
        query=query,
    )
    monkeypatch.setattr(claude_module, "import_module", lambda name: sdk)
    monkeypatch.setattr(
        claude_module,
        "resolve_local_executable",
        lambda *args: Path("/opt/local/bin/claude"),
    )

    result = await ClaudeProvider(model="claude-model").execute(request(web=True))

    assert result.output == DemoOutput(answer="claude")
    assert result.model == "claude-model"
    assert state["prompt"] == "User prompt from the plugin."
    options = state["options"]
    assert options["system_prompt"].startswith("System instructions")
    assert options["tools"] == ["WebFetch", "WebSearch"]
    assert options["allowed_tools"] == ["WebFetch", "WebSearch"]
    assert options["permission_mode"] == "dontAsk"
    assert options["mcp_servers"] == {}
    assert options["strict_mcp_config"] is True
    assert options["cli_path"] == "/opt/local/bin/claude"
    assert "setting_sources" not in options
    assert options["env"] == {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    assert options["output_format"]["schema"]["properties"]["answer"]
    assert state["stream_closed"] is True
    assert Path(options["cwd"]).exists() is False


@pytest.mark.asyncio
async def test_claude_denies_unsafe_tool_before_sdk_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = AgentRequest[DemoOutput](
        prompt="test",
        response_model=DemoOutput,
        tool_policy=ToolPolicy(allowed_tools=("Bash",)),
    )

    def should_not_import(name: str) -> Any:
        raise AssertionError(name)

    monkeypatch.setattr(claude_module, "import_module", should_not_import)
    with pytest.raises(ProviderCapabilityError, match="Bash"):
        await ClaudeProvider().execute(item)


def create_skill(root: Path) -> Path:
    skill_dir = root / "adapter-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: adapter-skill\n"
        "description: Add provider-specific guidance.\n"
        "---\n\n"
        "# Guidance\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.mark.asyncio
async def test_codex_stages_and_explicitly_references_selected_skill(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state: dict[str, Any] = {}

    class FakeSkillInput:
        def __init__(self, name: str, path: str) -> None:
            self.name = name
            self.path = path

    class FakeTextInput:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeThread:
        async def run(self, run_input: Any, **kwargs: Any) -> Any:
            state["run_input"] = run_input
            staged = Path(run_input[0].path)
            state["staged_exists"] = staged.is_file()
            return SimpleNamespace(final_response='{"answer":"skilled"}')

    class FakeCodex:
        def __init__(self, config: Any) -> None:
            pass

        async def __aenter__(self) -> FakeCodex:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def thread_start(self, **kwargs: Any) -> FakeThread:
            return FakeThread()

    sdk = SimpleNamespace(
        AsyncCodex=FakeCodex,
        ApprovalMode=SimpleNamespace(deny_all="deny-all"),
        CodexConfig=lambda **kwargs: kwargs,
        Sandbox=SimpleNamespace(read_only="read-only"),
        SkillInput=FakeSkillInput,
        TextInput=FakeTextInput,
    )
    monkeypatch.setattr(codex_module, "import_module", lambda name: sdk)
    skill = load_skill(create_skill(tmp_path))

    result = await CodexProvider(skills=(skill,)).execute(request())

    skill_input, text_input = state["run_input"]
    assert result.output.answer == "skilled"
    assert skill_input.name == "adapter-skill"
    assert "/.agents/skills/adapter-skill/SKILL.md" in skill_input.path
    assert text_input.text == "User prompt from the plugin."
    assert state["staged_exists"] is True
    assert Path(skill_input.path).exists() is False


@pytest.mark.asyncio
async def test_claude_stages_and_enables_only_selected_skill(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state: dict[str, Any] = {}

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.values = kwargs
            state["options"] = kwargs

    class FakeResult:
        is_error = False
        subtype = "success"
        structured_output = {"answer": "skilled"}

    class FakeStream:
        def __init__(self) -> None:
            self.sent = False

        def __aiter__(self) -> FakeStream:
            return self

        async def __anext__(self) -> FakeResult:
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return FakeResult()

        async def aclose(self) -> None:
            pass

    def query(*, prompt: str, options: FakeOptions) -> FakeStream:
        state["prompt"] = prompt
        staged = Path(options.values["cwd"]) / ".claude/skills/adapter-skill/SKILL.md"
        state["staged"] = staged
        state["staged_exists"] = staged.is_file()
        return FakeStream()

    sdk = SimpleNamespace(
        ClaudeAgentOptions=FakeOptions,
        ResultMessage=FakeResult,
        query=query,
    )
    monkeypatch.setattr(claude_module, "import_module", lambda name: sdk)
    skill = load_skill(create_skill(tmp_path))

    result = await ClaudeProvider(skills=(skill,)).execute(request())

    assert result.output.answer == "skilled"
    assert state["prompt"].startswith("/adapter-skill\n\n")
    assert state["options"]["tools"] == ["Skill"]
    assert state["options"]["allowed_tools"] == ["Skill"]
    assert state["options"]["skills"] == ["adapter-skill"]
    assert state["options"]["setting_sources"] == ["user", "project", "local"]
    assert state["staged_exists"] is True
    assert state["staged"].exists() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subtype", "is_error", "status", "reason", "expected_error", "retryable"),
    [
        ("success", True, 401, None, ProviderAuthenticationError, False),
        ("success", True, 403, None, ProviderPermissionError, False),
        ("success", True, 429, None, ProviderRateLimitError, True),
        ("success", True, 408, None, ProviderTransportError, True),
        ("success", True, 500, None, ProviderTransportError, True),
        ("success", True, 529, None, ProviderTransportError, True),
        ("success", True, 400, None, ProviderExecutionError, False),
        ("success", True, None, None, ProviderExecutionError, False),
        ("error_during_execution", True, None, None, ProviderExecutionError, False),
        ("error_max_turns", True, None, None, ProviderExecutionError, False),
        ("error_max_budget_usd", True, None, None, ProviderExecutionError, False),
        ("error_max_structured_output_retries", True, None, None, ProviderResponseError, False),
        ("success", False, None, "aborted_streaming", ProviderExecutionError, False),
        ("success", False, None, "aborted_tools", ProviderExecutionError, False),
    ],
)
async def test_claude_terminal_errors_preserve_classification_and_close_stream(
    monkeypatch, subtype, is_error, status, reason, expected_error, retryable
):
    sdk = import_module("claude_agent_sdk")
    state = {"closed": False}
    failure = sdk.ResultMessage(
        subtype=subtype,
        is_error=is_error,
        api_error_status=status,
        terminal_reason=reason,
        duration_ms=0,
        duration_api_ms=0,
        num_turns=1,
        session_id="test-session",
        structured_output={"answer": "must not be accepted"},
        result="PRIVATE PROVIDER RESPONSE",
        errors=["PRIVATE PROVIDER RESPONSE"],
    )

    async def query(**_kwargs):
        try:
            yield failure
        finally:
            state["closed"] = True

    monkeypatch.setattr(sdk, "query", query)
    with pytest.raises(expected_error) as error:
        await ClaudeProvider().execute(request())
    assert error.value.retryable is retryable
    assert "PRIVATE PROVIDER RESPONSE" not in str(error.value)
    assert "PRIVATE PROVIDER RESPONSE" not in error.value.public_message
    assert state["closed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_result", [False, True])
async def test_claude_requires_terminal_success_with_structured_output(monkeypatch, with_result):
    sdk = import_module("claude_agent_sdk")

    async def query(**_kwargs):
        if with_result:
            yield sdk.ResultMessage(
                subtype="success",
                is_error=False,
                duration_ms=0,
                duration_api_ms=0,
                num_turns=1,
                session_id="test-session",
                structured_output={"answer": "ok"},
            )

    monkeypatch.setattr(sdk, "query", query)
    if with_result:
        assert (await ClaudeProvider().execute(request())).output.answer == "ok"
    else:
        with pytest.raises(ProviderResponseError, match="no terminal result"):
            await ClaudeProvider().execute(request())
