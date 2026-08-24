"""Codex SDK adapter for generic, read-only structured requests."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_core.contracts import AgentRequest, ProviderResult
from agent_core.providers.base import BaseProvider, OutputT
from agent_core.providers.capabilities import ProviderCapabilities
from agent_core.providers.codex_schema import validate_codex_output_schema
from agent_core.providers.errors import ProviderUnavailableError
from agent_core.providers.local_runtime import resolve_local_executable
from agent_core.providers.structured import validate_structured_output
from agent_core.skills import CODEX_SKILL_LAYOUT, stage_skill


class CodexProvider(BaseProvider):
    """Execute requests through ``openai-codex`` with no write authority."""

    name = "codex"
    capabilities = ProviderCapabilities(
        structured_output=True,
        skills=True,
        web_access=True,
        read_only_workspace=True,
        allowed_tools=frozenset(),
    )

    @staticmethod
    def _load_sdk() -> Any:
        try:
            sdk = import_module("openai_codex")
            for attribute in ("AsyncCodex", "ApprovalMode", "CodexConfig", "Sandbox"):
                getattr(sdk, attribute)
            return sdk
        except (ImportError, ModuleNotFoundError, AttributeError) as exc:
            raise ProviderUnavailableError(
                "openai-codex is not installed or has an incompatible API; "
                "install agent-core[codex]",
                provider="codex",
            ) from exc

    async def _execute(
        self,
        request: AgentRequest[OutputT],
    ) -> ProviderResult[OutputT]:
        schema = request.response_model.model_json_schema()
        validate_codex_output_schema(schema)

        sdk = self._load_sdk()
        if self.skills:
            try:
                SkillInput = sdk.SkillInput
                TextInput = sdk.TextInput
            except AttributeError as exc:
                raise ProviderUnavailableError(
                    "installed openai-codex does not support SkillInput and TextInput",
                    provider=self.name,
                ) from exc

        web_mode = "live" if request.tool_policy.web_access else "disabled"
        with TemporaryDirectory(prefix="agent-core-codex-") as cwd:
            run_input: Any = request.prompt
            if self.skills:
                skill_inputs = []
                for skill in self.skills:
                    staged = stage_skill(skill, Path(cwd), CODEX_SKILL_LAYOUT)
                    skill_inputs.append(SkillInput(skill.name, str(staged)))
                run_input = [*skill_inputs, TextInput(request.prompt)]

            config_options: dict[str, Any] = {
                "cwd": cwd,
                "config_overrides": (f'web_search="{web_mode}"',),
            }
            local_codex = resolve_local_executable(self.name, ("codex",))
            if local_codex is not None:
                config_options["codex_bin"] = str(local_codex)
            config = sdk.CodexConfig(
                **config_options,
            )
            async with sdk.AsyncCodex(config=config) as codex:
                thread = await codex.thread_start(
                    approval_mode=sdk.ApprovalMode.deny_all,
                    developer_instructions=request.system_prompt,
                    ephemeral=True,
                    model=self._requested_model,
                    sandbox=sdk.Sandbox.read_only,
                )
                result = await thread.run(
                    run_input,
                    approval_mode=sdk.ApprovalMode.deny_all,
                    output_schema=schema,
                    sandbox=sdk.Sandbox.read_only,
                )

        output = validate_structured_output(
            getattr(result, "final_response", None),
            request.response_model,
            provider=self.name,
        )
        resolved_model = getattr(result, "model", None) or self.model
        return ProviderResult(
            request_id=request.request_id,
            provider=self.name,
            model=resolved_model,
            output=output,
        )
__all__ = ["CodexProvider"]
