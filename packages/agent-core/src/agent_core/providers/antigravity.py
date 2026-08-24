"""Google Antigravity adapter with local-CLI-first execution."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_core.contracts import AgentRequest, ProviderResult
from agent_core.providers.base import BaseProvider, OutputT
from agent_core.providers.capabilities import ProviderCapabilities
from agent_core.providers.errors import (
    ProviderAuthenticationError,
    ProviderExecutionError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from agent_core.providers.local_runtime import resolve_local_executable
from agent_core.providers.structured import validate_structured_output
from agent_core.skills import ANTIGRAVITY_SKILL_LAYOUT, stage_skill


class AntigravityProvider(BaseProvider):
    """Run structured requests through local ``agy`` or the Python SDK."""

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
                "install agent-core[antigravity] or install the agy CLI",
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

            local_cli = resolve_local_executable(
                self.name,
                ("agy",),
            )
            if local_cli is not None:
                raw = await self._execute_local_cli(
                    local_cli,
                    cwd_path,
                    prompt=prompt,
                    system_prompt=request.system_prompt,
                    schema=schema,
                    web_access=request.tool_policy.web_access,
                )
            else:
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

    async def _execute_local_cli(
        self,
        executable: Path,
        cwd: Path,
        *,
        prompt: str,
        system_prompt: str,
        schema: dict[str, Any],
        web_access: bool,
    ) -> Any:
        web_rule = (
            "Web search and URL reading are allowed when needed."
            if web_access
            else "Do not access the web or any URL."
        )
        (cwd / "AGENTS.md").write_text(
            "# Agent Core System Instructions\n\n"
            "Do not run commands, write files, invoke subagents, or use MCP tools. "
            f"{web_rule}\n\n{system_prompt}\n",
            encoding="utf-8",
        )
        schema_path = cwd / "response-schema.json"
        schema_path.write_text(
            json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        command = [
            str(executable),
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--json-schema",
            str(schema_path),
            "--sandbox",
        ]
        if self._requested_model is not None:
            command.extend(("--model", self._requested_model))
        stream_input = (
            json.dumps(
                {"event": "user", "message": {"content": prompt}},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await process.communicate(input=stream_input)
        except asyncio.CancelledError:
            await _terminate_process(process)
            raise

        if process.returncode != 0:
            error_text = stderr.decode("utf-8", errors="replace").casefold()
            if any(
                token in error_text
                for token in ("auth", "credential", "login", "sign in")
            ):
                raise ProviderAuthenticationError(
                    "Antigravity CLI authentication failed",
                    provider=self.name,
                )
            raise ProviderExecutionError(
                f"Antigravity CLI exited unsuccessfully (status {process.returncode})",
                provider=self.name,
            )

        result_envelope = _last_cli_result(stdout)
        if result_envelope.get("status") != "SUCCESS":
            error_text = " ".join(
                (
                    str(result_envelope.get("error", "")),
                    stderr.decode("utf-8", errors="replace"),
                )
            ).casefold()
            if any(
                token in error_text
                for token in ("auth", "credential", "login", "sign in")
            ):
                raise ProviderAuthenticationError(
                    "Antigravity CLI authentication failed",
                    provider=self.name,
                )
            raise ProviderExecutionError(
                "Antigravity CLI returned an unsuccessful result",
                provider=self.name,
            )
        return result_envelope.get("structured_output")

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
            enabled_tools.extend(
                (sdk.BuiltinTools.SEARCH_WEB, sdk.BuiltinTools.READ_URL_CONTENT)
            )
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


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()


def _last_cli_result(stdout: bytes) -> dict[str, Any]:
    result: dict[str, Any] | None = None
    try:
        for line in stdout.splitlines():
            event = json.loads(line)
            if isinstance(event, dict) and event.get("event") == "result":
                candidate = event.get("result")
                if isinstance(candidate, dict):
                    result = candidate
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderResponseError(
            "Antigravity CLI returned invalid stream JSON",
            provider="antigravity",
        ) from exc
    if result is None:
        raise ProviderResponseError(
            "Antigravity CLI returned no terminal result event",
            provider="antigravity",
        )
    return result


__all__ = ["AntigravityProvider"]
