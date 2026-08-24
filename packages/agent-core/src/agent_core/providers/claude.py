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
from agent_core.providers.errors import ProviderUnavailableError
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

        structured_output: Any | None = None
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
                    if (
                        isinstance(message, sdk.ResultMessage)
                        and message.structured_output is not None
                    ):
                        structured_output = message.structured_output
            finally:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await asyncio.shield(close())

        output = validate_structured_output(
            structured_output,
            request.response_model,
            provider=self.name,
        )
        return ProviderResult(
            request_id=request.request_id,
            provider=self.name,
            model=self.model,
            output=output,
        )


__all__ = ["ClaudeProvider"]
