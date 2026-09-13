"""Claude Agent SDK adapter for generic, deny-by-default requests."""

from __future__ import annotations

import asyncio
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_core.contracts import AgentRequest, ProviderResult
from agent_core.providers.base import BaseProvider, OutputT
from agent_core.providers.capabilities import ProviderCapabilities
from agent_core.providers.errors import (
    ProviderExecutionError,
    ProviderResponseError,
    ProviderUnavailableError,
    classify_provider_exception,
)
from agent_core.providers.local_runtime import resolve_local_executable
from agent_core.providers.structured import validate_structured_output
from agent_core.skills import CLAUDE_SKILL_LAYOUT, stage_skill


class ClaudeProvider(BaseProvider):
    """Execute requests with an explicit safe-tool allowlist and no MCP servers."""

    name = "claude"
    capabilities = ProviderCapabilities(
        structured_output=True,
        skills=True,
        web_access=True,
        read_only_workspace=True,
        # Skill and web tools are derived from dedicated policy fields below;
        # arbitrary tool names are denied even when the SDK happens to support them.
        allowed_tools=frozenset(),
    )

    @staticmethod
    def _load_sdk() -> Any:
        try:
            sdk = import_module("claude_agent_sdk")
            for attribute in ("ClaudeAgentOptions", "ResultMessage", "query"):
                getattr(sdk, attribute)
            return sdk
        except (ImportError, ModuleNotFoundError, AttributeError) as exc:
            raise ProviderUnavailableError(
                "claude-agent-sdk is not installed or has an incompatible API; "
                "install agent-core[claude]",
                provider="claude",
            ) from exc

    async def _execute(
        self,
        request: AgentRequest[OutputT],
    ) -> ProviderResult[OutputT]:
        sdk = self._load_sdk()
        tools = set(request.tool_policy.allowed_tools)
        if self.skills:
            tools.add("Skill")
        if request.tool_policy.web_access:
            tools.update(("WebSearch", "WebFetch"))
        active_tools = sorted(tools)
        prompt = request.prompt
        if self.skills:
            invocations = "\n".join(f"/{skill.name}" for skill in self.skills)
            prompt = f"{invocations}\n\n{prompt}"

        terminal_result: Any | None = None
        with TemporaryDirectory(prefix="agent-core-claude-") as cwd:
            cwd_path = Path(cwd)
            for skill in self.skills:
                stage_skill(skill, cwd_path, CLAUDE_SKILL_LAYOUT)
            option_values: dict[str, Any] = {
                "allowed_tools": active_tools,
                "cwd": cwd,
                "env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
                "mcp_servers": {},
                "model": self._requested_model,
                "output_format": {
                    "type": "json_schema",
                    "schema": request.response_model.model_json_schema(),
                },
                "permission_mode": "dontAsk",
                "skills": [skill.name for skill in self.skills],
                "strict_mcp_config": True,
                "system_prompt": request.system_prompt,
                "tools": active_tools,
            }
            # Omitting setting_sources lets Claude reuse the local user's
            # configured authentication/settings. The isolated cwd prevents
            # unrelated repository settings from entering the request.
            if self.skills:
                option_values["setting_sources"] = ["user", "project", "local"]
            local_claude = resolve_local_executable(self.name, ("claude",))
            if local_claude is not None:
                option_values["cli_path"] = str(local_claude)
            options = sdk.ClaudeAgentOptions(
                **option_values,
            )
            stream = sdk.query(prompt=prompt, options=options)
            try:
                async for message in stream:
                    if isinstance(message, sdk.ResultMessage):
                        terminal_result = message
            finally:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await asyncio.shield(close())

        if terminal_result is None:
            raise ProviderResponseError("Claude returned no terminal result", provider=self.name)
        _raise_for_result(terminal_result)
        output = validate_structured_output(
            terminal_result.structured_output,
            request.response_model,
            provider=self.name,
        )
        return ProviderResult(
            request_id=request.request_id,
            provider=self.name,
            model=self.model,
            output=output,
        )


class _ClaudeApiError(RuntimeError):
    """Expose only an HTTP status to the shared error classifier."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Claude API request failed (status {status_code})")


def _raise_for_result(result: Any) -> None:
    status = getattr(result, "api_error_status", None)
    if isinstance(status, int) and not isinstance(status, bool) and 400 <= status <= 599:
        raise classify_provider_exception(_ClaudeApiError(status), provider="claude")
    if result.subtype == "error_max_structured_output_retries":
        raise ProviderResponseError("Claude exhausted structured-output retries", provider="claude")
    if result.is_error or result.subtype != "success":
        raise ProviderExecutionError("Claude query ended unsuccessfully", provider="claude")
    if getattr(result, "terminal_reason", None) in {"aborted_streaming", "aborted_tools"}:
        raise ProviderExecutionError("Claude query was aborted", provider="claude")


__all__ = ["ClaudeProvider"]
