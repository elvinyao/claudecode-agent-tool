"""Google Antigravity adapter with SDK-enforced tool policies."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_core.contracts import AgentRequest, ProviderResult
from agent_core.providers.base import BaseProvider, OutputT
from agent_core.providers.capabilities import ProviderCapabilities
from agent_core.providers.errors import ProviderUnavailableError
from agent_core.providers.local_runtime import require_antigravity_sdk_runtime
from agent_core.providers.structured import validate_structured_output
from agent_core.skills import ANTIGRAVITY_SKILL_LAYOUT, stage_skill


class AntigravityProvider(BaseProvider):
    """Run structured requests exclusively through the policy-controlled SDK."""

    name = "antigravity"
    capabilities = ProviderCapabilities(
        structured_output=True,
        skills=True,
        web_access=True,
        read_only_workspace=True,
        allowed_tools=frozenset(),
    )

    @staticmethod
    def _load_sdk() -> tuple[Any, Any]:
        require_antigravity_sdk_runtime()
        try:
            sdk = import_module("google.antigravity")
            policy = import_module("google.antigravity.hooks.policy")
            for attribute in (
                "Agent",
                "BuiltinTools",
                "CapabilitiesConfig",
                "LocalAgentConfig",
            ):
                getattr(sdk, attribute)
            for attribute in ("allow", "deny_all"):
                getattr(policy, attribute)
            return sdk, policy
        except (ImportError, ModuleNotFoundError, AttributeError) as exc:
            raise ProviderUnavailableError(
                "google-antigravity is not installed or has an incompatible API; "
                "install agent-core[antigravity]",
                provider="antigravity",
            ) from exc

    async def _execute(
        self,
        request: AgentRequest[OutputT],
    ) -> ProviderResult[OutputT]:
        schema = request.response_model.model_json_schema()
        prompt = request.prompt
        if self.skills:
            invocations = "\n".join(f"/{skill.name}" for skill in self.skills)
            prompt = f"{invocations}\n\n{prompt}"

        with TemporaryDirectory(prefix="agent-core-antigravity-") as cwd:
            cwd_path = Path(cwd)
            staged_skill_dirs: list[str] = []
            for skill in self.skills:
                staged = stage_skill(skill, cwd_path, ANTIGRAVITY_SKILL_LAYOUT)
                staged_skill_dirs.append(str(staged.parent))

            raw = await self._execute_sdk(
                cwd_path,
                prompt=prompt,
                system_prompt=request.system_prompt,
                schema=schema,
                skills_paths=staged_skill_dirs,
                web_access=request.tool_policy.web_access,
            )

        output = validate_structured_output(
            raw,
            request.response_model,
            provider=self.name,
        )
        return ProviderResult(
            request_id=request.request_id,
            provider=self.name,
            model=self.model,
            output=output,
        )

    async def _execute_sdk(
        self,
        cwd: Path,
        *,
        prompt: str,
        system_prompt: str,
        schema: dict[str, Any],
        skills_paths: list[str],
        web_access: bool,
    ) -> Any:
        sdk, policy = self._load_sdk()
        enabled_tools = [sdk.BuiltinTools.FINISH]
        if web_access:
            enabled_tools.extend((sdk.BuiltinTools.SEARCH_WEB, sdk.BuiltinTools.READ_URL_CONTENT))
        capabilities = sdk.CapabilitiesConfig(
            enable_subagents=False,
            enabled_tools=enabled_tools,
        )
        policies = [policy.deny_all()]
        policies.extend(policy.allow(tool.value) for tool in enabled_tools)
        config = sdk.LocalAgentConfig(
            app_data_dir=str(cwd / ".antigravity-data"),
            capabilities=capabilities,
            mcp_servers=[],
            model=self._requested_model,
            policies=policies,
            response_schema=schema,
            save_dir=str(cwd / ".antigravity-state"),
            skills_paths=skills_paths,
            subagents=[],
            system_instructions=system_prompt,
            tools=[],
            workspaces=[str(cwd)],
        )
        async with sdk.Agent(config) as agent:
            response = await agent.chat(prompt)
            return await response.structured_output()


__all__ = ["AntigravityProvider"]
