"""Lazy, provider-neutral adapters for Codex, Claude, and Google ADK."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, ClassVar, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import ValidationError

from trivy_ai_report.models import (
    AnalysisOutcome,
    Finding,
    Recommendation,
    RecommendationBatch,
    ResearchStatus,
)
from trivy_ai_report.prompts import SYSTEM_PROMPT, build_analysis_prompt
from trivy_ai_report.skills import SkillSpec, materialize_skill

RETRY_BACKOFF_SECONDS = 0.05
GEMINI_DEFAULT_MODEL = "gemini-3.6-flash"


class ProviderError(RuntimeError):
    """Base class for errors that should degrade to a fact-only report."""


class ProviderUnavailableError(ProviderError):
    """The selected optional SDK is not installed or cannot be imported."""


class ProviderExecutionError(ProviderError):
    """The provider failed before returning a usable structured result."""


class ProviderResponseError(ProviderError):
    """The provider returned missing, malformed, or unsafe structured output."""


class ProviderTimeoutError(ProviderError):
    """A provider batch exceeded the caller's timeout."""


class ProviderConfigurationError(ProviderError):
    """The provider name or local analyzer configuration is invalid."""


@runtime_checkable
class Analyzer(Protocol):
    """Provider-neutral asynchronous analyzer interface."""

    provider: str
    model: str
    enrich_web: bool
    skill: SkillSpec | None

    async def analyze(
        self,
        findings: Sequence[Finding],
        *,
        timeout_seconds: float | None = None,
    ) -> AnalysisOutcome: ...


class BaseAnalyzer(ABC):
    """Sequential batching, timeout handling, and output identity validation."""

    provider: ClassVar[str]

    def __init__(
        self,
        *,
        model: str | None = None,
        enrich_web: bool = False,
        batch_size: int = 10,
        skill: SkillSpec | None = None,
    ) -> None:
        if batch_size <= 0:
            raise ProviderConfigurationError("batch_size 必须大于 0")
        self._requested_model = model
        self.model = model or "provider-default"
        self.enrich_web = enrich_web
        self.batch_size = batch_size
        self.skill = skill

    async def analyze(
        self,
        findings: Sequence[Finding],
        *,
        timeout_seconds: float | None = None,
    ) -> AnalysisOutcome:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ProviderConfigurationError("timeout_seconds 必须大于 0")

        items = list(findings)
        if not items:
            return self._outcome([])

        batches = [
            items[index : index + self.batch_size]
            for index in range(0, len(items), self.batch_size)
        ]
        recommendations: list[Recommendation] = []
        warnings: list[str] = []
        failures: list[ProviderError] = []
        successful_batches = 0

        for index, batch in enumerate(batches, start=1):
            try:
                result = await self._run_batch_with_retry(
                    batch,
                    timeout_seconds=timeout_seconds,
                )
                recommendations.extend(self._validate_batch(result, batch))
                successful_batches += 1
            except ProviderUnavailableError:
                # Retrying later batches cannot install a missing SDK.
                raise
            except ProviderError as exc:
                failures.append(exc)
                warnings.append(f"{self.provider} 第 {index}/{len(batches)} 批失败：{exc}")
            except Exception as exc:  # pragma: no cover - defensive adapter boundary
                error = ProviderExecutionError(f"{self.provider} 调用失败：{exc}")
                failures.append(error)
                warnings.append(f"{self.provider} 第 {index}/{len(batches)} 批失败：{exc}")

        if successful_batches == 0 and failures:
            if len(failures) == 1:
                raise failures[0]
            raise ProviderExecutionError(
                f"{self.provider} 的 {len(failures)} 个批次全部失败；首个错误：{failures[0]}"
            ) from failures[0]

        return self._outcome(
            recommendations,
            partial=bool(failures),
            warnings=warnings,
        )

    async def _run_batch_with_retry(
        self,
        findings: Sequence[Finding],
        *,
        timeout_seconds: float | None,
    ) -> RecommendationBatch:
        """Retry one transient execution failure or timeout, never bad output."""

        for attempt in range(2):
            try:
                call = self._analyze_batch(findings)
                return (
                    await call
                    if timeout_seconds is None
                    else await asyncio.wait_for(call, timeout=timeout_seconds)
                )
            except asyncio.TimeoutError as exc:
                error: ProviderError = ProviderTimeoutError(
                    f"{self.provider} 单批请求超过 {timeout_seconds:g} 秒"
                )
                error.__cause__ = exc
            except (ProviderUnavailableError, ProviderResponseError):
                raise
            except ProviderExecutionError as exc:
                error = exc
            except ProviderError:
                raise
            except Exception as exc:  # pragma: no cover - defensive adapter boundary
                error = ProviderExecutionError(f"{self.provider} 调用失败：{exc}")

            if attempt == 0:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS)
                continue
            raise error

        raise AssertionError("unreachable")  # pragma: no cover

    def _outcome(
        self,
        recommendations: list[Recommendation],
        *,
        partial: bool = False,
        warnings: list[str] | None = None,
    ) -> AnalysisOutcome:
        return AnalysisOutcome(
            provider=self.provider,
            model=self.model,
            skill_name=self.skill.name if self.skill is not None else None,
            enrich_web=self.enrich_web,
            recommendations=recommendations,
            partial=partial,
            warnings=warnings or [],
        )

    def _validate_batch(
        self,
        result: RecommendationBatch,
        findings: Sequence[Finding],
    ) -> list[Recommendation]:
        expected = {finding.finding_id for finding in findings}
        ids = [recommendation.finding_id for recommendation in result.recommendations]
        duplicates = sorted(finding_id for finding_id, count in Counter(ids).items() if count > 1)
        unknown = sorted(set(ids) - expected)
        if duplicates:
            raise ProviderResponseError(f"响应包含重复 finding_id：{', '.join(duplicates)}")
        if unknown:
            raise ProviderResponseError(f"响应包含未知 finding_id：{', '.join(unknown)}")

        if self.enrich_web:
            return result.recommendations

        # Offline reports must never surface model-invented citations.
        return [
            recommendation.model_copy(
                update={"research_status": ResearchStatus.NOT_REQUESTED, "evidence": []}
            )
            for recommendation in result.recommendations
        ]

    @abstractmethod
    async def _analyze_batch(self, findings: Sequence[Finding]) -> RecommendationBatch:
        """Run exactly one provider request."""


def _load_optional_sdk(name: str, *, extra: str) -> Any:
    try:
        return import_module(name)
    except (ImportError, ModuleNotFoundError) as exc:
        raise ProviderUnavailableError(
            f"未安装或无法加载 {name}；请运行 `uv sync --extra {extra}` "
            f"或 `pip install '.[{extra}]'`"
        ) from exc


def _parse_json_response(raw: str, *, provider: str) -> RecommendationBatch:
    if not raw.strip():
        raise ProviderResponseError(f"{provider} 未返回最终结构化内容")
    try:
        return RecommendationBatch.model_validate_json(raw)
    except ValidationError as exc:
        raise ProviderResponseError(
            f"{provider} 返回内容不符合 RecommendationBatch Schema"
        ) from exc


class CodexAnalyzer(BaseAnalyzer):
    provider = "codex"

    async def _analyze_batch(self, findings: Sequence[Finding]) -> RecommendationBatch:
        sdk = _load_optional_sdk("openai_codex", extra="codex")
        try:
            AsyncCodex = sdk.AsyncCodex
            ApprovalMode = sdk.ApprovalMode
            CodexConfig = sdk.CodexConfig
            Sandbox = sdk.Sandbox
            if self.skill is not None:
                SkillInput = sdk.SkillInput
                TextInput = sdk.TextInput
        except AttributeError as exc:
            raise ProviderUnavailableError(
                "openai-codex 版本不兼容；需要包含 AsyncCodex、CodexConfig、"
                "ApprovalMode 和 Sandbox 的版本；使用 --skill 时还需要 SkillInput 和 TextInput"
            ) from exc

        schema = RecommendationBatch.model_json_schema()
        prompt = build_analysis_prompt(findings, enrich_web=self.enrich_web)
        web_mode = "live" if self.enrich_web else "disabled"

        try:
            with TemporaryDirectory(prefix="trivy-ai-report-codex-") as cwd:
                run_input: Any = prompt
                if self.skill is not None:
                    staged_skill = materialize_skill(self.skill, Path(cwd), "codex")
                    # This is an explicit native skill reference, equivalent to
                    # selecting the named skill rather than relying on discovery.
                    run_input = [
                        SkillInput(self.skill.name, str(staged_skill)),
                        TextInput(prompt),
                    ]
                config = CodexConfig(
                    cwd=cwd,
                    config_overrides=(f'web_search="{web_mode}"',),
                )
                async with AsyncCodex(config=config) as codex:
                    thread = await codex.thread_start(
                        approval_mode=ApprovalMode.deny_all,
                        developer_instructions=SYSTEM_PROMPT,
                        ephemeral=True,
                        model=self._requested_model,
                        sandbox=Sandbox.read_only,
                    )
                    result = await thread.run(
                        run_input,
                        approval_mode=ApprovalMode.deny_all,
                        output_schema=schema,
                        sandbox=Sandbox.read_only,
                    )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderExecutionError(f"Codex SDK 调用失败：{exc}") from exc

        return _parse_json_response(
            getattr(result, "final_response", None) or "",
            provider="Codex",
        )


class ClaudeAnalyzer(BaseAnalyzer):
    provider = "claude"

    async def _analyze_batch(self, findings: Sequence[Finding]) -> RecommendationBatch:
        sdk = _load_optional_sdk("claude_agent_sdk", extra="claude")
        try:
            ClaudeAgentOptions = sdk.ClaudeAgentOptions
            ResultMessage = sdk.ResultMessage
            query = sdk.query
        except AttributeError as exc:
            raise ProviderUnavailableError(
                "claude-agent-sdk 版本不兼容；需要 query、ClaudeAgentOptions 和 ResultMessage"
            ) from exc

        tools = ["Skill"] if self.skill is not None else []
        if self.enrich_web:
            tools.extend(["WebSearch", "WebFetch"])
        schema = RecommendationBatch.model_json_schema()
        prompt = build_analysis_prompt(findings, enrich_web=self.enrich_web)
        if self.skill is not None:
            # Agent Skills are exposed as slash commands by the Claude SDK.
            # Prefixing the request makes the selected skill explicit.
            prompt = f"/{self.skill.name}\n\n{prompt}"
        structured_output: Any | None = None

        try:
            with TemporaryDirectory(prefix="trivy-ai-report-claude-") as cwd:
                cwd_path = Path(cwd)
                if self.skill is not None:
                    materialize_skill(self.skill, cwd_path, "claude")
                config_dir = cwd_path / ".claude-config"
                config_dir.mkdir()
                options = ClaudeAgentOptions(
                    allowed_tools=tools,
                    cwd=cwd,
                    env={
                        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                        "CLAUDE_CONFIG_DIR": str(config_dir),
                    },
                    mcp_servers={},
                    model=self._requested_model,
                    output_format={"type": "json_schema", "schema": schema},
                    permission_mode="dontAsk",
                    setting_sources=["project"] if self.skill is not None else [],
                    skills=[self.skill.name] if self.skill is not None else [],
                    strict_mcp_config=True,
                    system_prompt=SYSTEM_PROMPT,
                    tools=tools,
                )
                async for message in query(prompt=prompt, options=options):
                    if isinstance(message, ResultMessage) and message.structured_output is not None:
                        structured_output = message.structured_output
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderExecutionError(f"Claude Agent SDK 调用失败：{exc}") from exc

        if structured_output is None:
            raise ProviderResponseError("Claude 未返回 structured_output")
        try:
            return RecommendationBatch.model_validate(structured_output)
        except ValidationError as exc:
            raise ProviderResponseError(
                "Claude structured_output 不符合 RecommendationBatch Schema"
            ) from exc


def _event_parts(event: Any) -> list[Any]:
    """Return ADK event parts without depending on a concrete SDK model type."""

    content = getattr(event, "content", None)
    return list(getattr(content, "parts", None) or [])


def _successful_skill_load(event: Any, skill_name: str) -> bool:
    """Recognize a successful ADK ``load_skill`` tool response."""

    for part in _event_parts(event):
        function_response = getattr(part, "function_response", None)
        if getattr(function_response, "name", None) != "load_skill":
            continue
        response = getattr(function_response, "response", None)
        if (
            isinstance(response, dict)
            and not response.get("error")
            and response.get("skill_name") == skill_name
        ):
            return True
    return False


class GeminiAnalyzer(BaseAnalyzer):
    """Gemini remediation agent implemented with the official Google ADK."""

    provider = "gemini"

    def __init__(
        self,
        *,
        model: str | None = None,
        enrich_web: bool = False,
        batch_size: int = 10,
        skill: SkillSpec | None = None,
    ) -> None:
        if enrich_web:
            raise ProviderConfigurationError(
                "Gemini 暂不启用 --enrich-web：Google Search grounding 的搜索建议必须随报告"
                "展示；当前离线 HTML 尚未安全承载该归因组件。请移除该参数，或改用 Codex/Claude。"
            )
        super().__init__(
            model=model or GEMINI_DEFAULT_MODEL,
            enrich_web=False,
            batch_size=batch_size,
            skill=skill,
        )

    async def _analyze_batch(self, findings: Sequence[Finding]) -> RecommendationBatch:
        try:
            agents_sdk = import_module("google.adk.agents")
            runners_sdk = import_module("google.adk.runners")
            sessions_sdk = import_module("google.adk.sessions")
            skills_sdk = import_module("google.adk.skills")
            skill_toolset_sdk = import_module("google.adk.tools.skill_toolset")
            genai_types = import_module("google.genai.types")
            Agent = agents_sdk.Agent
            Runner = runners_sdk.Runner
            InMemorySessionService = sessions_sdk.InMemorySessionService
            load_skill_from_dir = skills_sdk.load_skill_from_dir
            SkillToolset = skill_toolset_sdk.SkillToolset
            Content = genai_types.Content
            Part = genai_types.Part
        except (ImportError, ModuleNotFoundError, AttributeError) as exc:
            raise ProviderUnavailableError(
                "未安装或无法加载兼容的 Google ADK；请运行 `uv sync --extra gemini` "
                "或 `pip install '.[gemini]'`"
            ) from exc

        prompt = build_analysis_prompt(findings, enrich_web=False)
        instruction = SYSTEM_PROMPT
        tools: list[Any] = []
        staged_skill_name: str | None = None

        try:
            with TemporaryDirectory(prefix="trivy-ai-report-gemini-") as cwd:
                if self.skill is not None:
                    staged_skill_file = materialize_skill(self.skill, Path(cwd), "gemini")
                    native_skill = load_skill_from_dir(staged_skill_file.parent)
                    tools.append(
                        SkillToolset(
                            skills=[native_skill],
                            registry=None,
                            code_executor=None,
                            additional_tools=[],
                            tool_filter=["load_skill", "load_skill_resource"],
                        )
                    )
                    staged_skill_name = self.skill.name
                    instruction += (
                        "\n在分析输入之前，必须先调用 load_skill，且 skill_name 必须严格等于 "
                        f"{self.skill.name!r}。只能使用该 Skill 及其只读资源。"
                    )
                    prompt = (
                        "开始分析前，先调用 load_skill，参数 skill_name 必须严格等于 "
                        f"{self.skill.name!r}；成功加载后再处理下面的 Trivy 事实。\n\n{prompt}"
                    )

                agent = Agent(
                    name="trivy_remediation_agent",
                    model=self.model,
                    description="Generate structured Chinese remediation advice from Trivy facts.",
                    instruction=instruction,
                    tools=tools,
                    output_schema=RecommendationBatch,
                )
                session_service = InMemorySessionService()
                app_name = "trivy_ai_report"
                user_id = "trivy_report_user"
                session_id = uuid4().hex
                await session_service.create_session(
                    app_name=app_name,
                    user_id=user_id,
                    session_id=session_id,
                )
                runner = Runner(
                    agent=agent,
                    app_name=app_name,
                    session_service=session_service,
                )

                final_response = ""
                skill_loaded = staged_skill_name is None
                message = Content(role="user", parts=[Part(text=prompt)])
                async with runner:
                    async for event in runner.run_async(
                        user_id=user_id,
                        session_id=session_id,
                        new_message=message,
                    ):
                        if staged_skill_name is not None and _successful_skill_load(
                            event, staged_skill_name
                        ):
                            skill_loaded = True
                        if event.is_final_response():
                            final_response = "".join(
                                text
                                for part in _event_parts(event)
                                if isinstance((text := getattr(part, "text", None)), str)
                            )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderExecutionError(f"Google ADK 调用失败：{exc}") from exc

        if not skill_loaded:
            raise ProviderResponseError(
                f"Gemini 未成功调用指定 Skill：{staged_skill_name}"
            )
        return _parse_json_response(final_response, provider="Gemini")


def create_analyzer(
    provider: str,
    *,
    model: str | None = None,
    enrich_web: bool = False,
    batch_size: int = 10,
    skill: SkillSpec | None = None,
) -> Analyzer:
    """Create an analyzer without importing any optional provider SDK."""

    normalized = provider.strip().lower()
    analyzers: dict[str, type[BaseAnalyzer]] = {
        "codex": CodexAnalyzer,
        "claude": ClaudeAnalyzer,
        "gemini": GeminiAnalyzer,
    }
    try:
        analyzer_type = analyzers[normalized]
    except KeyError as exc:
        raise ProviderConfigurationError(
            f"未知 provider：{provider!r}；可选值为 codex、claude、gemini"
        ) from exc
    return analyzer_type(
        model=model,
        enrich_web=enrich_web,
        batch_size=batch_size,
        skill=skill,
    )


__all__ = [
    "Analyzer",
    "BaseAnalyzer",
    "ClaudeAnalyzer",
    "CodexAnalyzer",
    "GEMINI_DEFAULT_MODEL",
    "GeminiAnalyzer",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderExecutionError",
    "ProviderResponseError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "create_analyzer",
]
