from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agent_core.providers.base as providers
from agent_core.providers import (
    BaseAnalyzer,
    ClaudeAnalyzer,
    CodexAnalyzer,
    GeminiAnalyzer,
    ProviderConfigurationError,
    ProviderExecutionError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    create_analyzer,
)
from agent_core.skills import load_skill
from trivy_ai_report.models import (
    Evidence,
    Finding,
    Recommendation,
    RecommendationBatch,
    RecommendationCategory,
    ResearchStatus,
)
from trivy_ai_report.prompts import build_analysis_prompt, finding_payload


def finding(index: int, **updates: Any) -> Finding:
    values: dict[str, Any] = {
        "finding_id": f"{index:064x}",
        "artifact_name": "demo:1",
        "artifact_type": "container_image",
        "target": "app.jar",
        "target_class": "lang-pkgs",
        "target_type": "jar",
        "vulnerability_id": f"CVE-2026-{index:04d}",
        "package_name": "org.springframework.boot:spring-boot",
        "installed_version": "3.1.0",
        "fixed_version": "3.1.1",
        "status": "fixed",
        "severity": "HIGH",
        "title": "test vulnerability",
        "description": "SECRET DESCRIPTION",
        "primary_url": "https://example.invalid/secret",
        "references": ("https://example.invalid/reference",),
    }
    values.update(updates)
    return Finding(**values)


def recommendation(finding_id: str, *, with_evidence: bool = False) -> Recommendation:
    return Recommendation(
        finding_id=finding_id,
        category=RecommendationCategory.SPRING_BOOT_OR_BOM_UPGRADE,
        title_zh="升级 Spring Boot",
        rationale_zh="Trivy 提供了修复版本。",
        actions_zh=["更新 parent 或 BOM"],
        validation_zh=["重新运行 Trivy"],
        recommended_version="3.1.1",
        research_status=ResearchStatus.ENRICHED if with_evidence else ResearchStatus.NOT_REQUESTED,
        evidence=(
            [Evidence(title="Vendor", url="https://vendor.example/advisory", claim_zh="已修复")]
            if with_evidence
            else []
        ),
    )


def create_test_skill(tmp_path: Path) -> Path:
    skill_dir = tmp_path / "trivy-test"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: trivy-test\n"
        "description: Provide read-only Trivy remediation rules.\n"
        "---\n\n"
        "# Rules\n\nDo not execute fixes.\n",
        encoding="utf-8",
    )
    references = skill_dir / "references"
    references.mkdir()
    (references / "policy.md").write_text("Prefer supported releases.\n", encoding="utf-8")
    return skill_dir


class FakeAnalyzer(BaseAnalyzer):
    provider = "fake"

    def __init__(self, *, failures: set[int] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.calls: list[list[Finding]] = []
        self.failures = failures or set()
        self.attempts: dict[int, int] = {}

    async def _analyze_batch(self, findings: list[Finding]) -> RecommendationBatch:
        batch = findings[0].finding_id
        attempt = self.attempts.get(int(batch, 16), 0) + 1
        self.attempts[int(batch, 16)] = attempt
        self.calls.append(list(findings))
        if int(batch, 16) in self.failures and attempt == 1:
            raise ProviderExecutionError("transient")
        return RecommendationBatch(
            recommendations=[
                recommendation(item.finding_id, with_evidence=True) for item in findings
            ]
        )


def test_prompt_uses_bounded_minimal_untrusted_facts() -> None:
    item = finding(1, title="x" * 700)
    payload = finding_payload(item)
    prompt = build_analysis_prompt([item], enrich_web=False)

    assert len(payload["title"]) == 501
    assert "description" not in payload
    assert "primary_url" not in payload
    assert "references" not in payload
    assert "SECRET DESCRIPTION" not in prompt
    assert "target_class=os-pkgs" in prompt
    assert "research_status=not_requested" in prompt


@pytest.mark.asyncio
async def test_base_analyzer_batches_retries_and_strips_offline_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers, "RETRY_BACKOFF_SECONDS", 0)
    analyzer = FakeAnalyzer(batch_size=2, failures={2})
    outcome = await analyzer.analyze([finding(i) for i in range(5)], timeout_seconds=1)

    assert [len(call) for call in analyzer.calls] == [2, 2, 2, 1]
    assert [int(call[0].finding_id, 16) for call in analyzer.calls] == [0, 2, 2, 4]
    assert len(outcome.recommendations) == 5
    assert not outcome.partial
    assert all(not item.evidence for item in outcome.recommendations)
    assert all(
        item.research_status is ResearchStatus.NOT_REQUESTED
        for item in outcome.recommendations
    )


@pytest.mark.asyncio
async def test_timeout_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers, "RETRY_BACKOFF_SECONDS", 0)

    class SlowAnalyzer(BaseAnalyzer):
        provider = "slow"

        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def _analyze_batch(self, findings: list[Finding]) -> RecommendationBatch:
            self.calls += 1
            await providers.asyncio.sleep(0.05)
            return RecommendationBatch(recommendations=[])

    analyzer = SlowAnalyzer()
    with pytest.raises(ProviderTimeoutError):
        await analyzer.analyze([finding(1)], timeout_seconds=0.001)
    assert analyzer.calls == 2


@pytest.mark.asyncio
async def test_bad_identity_is_not_retried() -> None:
    class BadAnalyzer(BaseAnalyzer):
        provider = "bad"

        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def _analyze_batch(self, findings: list[Finding]) -> RecommendationBatch:
            self.calls += 1
            bad = recommendation(f"{999:064x}")
            return RecommendationBatch(recommendations=[bad])

    analyzer = BadAnalyzer()
    with pytest.raises(ProviderResponseError, match="未知 finding_id"):
        await analyzer.analyze([finding(1)])
    assert analyzer.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("enrich_web, web_mode", [(False, "disabled"), (True, "live")])
async def test_codex_adapter_is_ephemeral_read_only_and_structured(
    monkeypatch: pytest.MonkeyPatch,
    enrich_web: bool,
    web_mode: str,
) -> None:
    state: dict[str, Any] = {}

    class FakeConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)
            state["config"] = kwargs

    class FakeThread:
        async def run(self, prompt: str, **kwargs: Any) -> Any:
            state["run"] = (prompt, kwargs)
            raw = RecommendationBatch(
                recommendations=[recommendation(finding(1).finding_id)]
            ).model_dump_json()
            return SimpleNamespace(final_response=raw)

    class FakeCodex:
        def __init__(self, config: Any) -> None:
            state["client_config"] = config

        async def __aenter__(self) -> FakeCodex:
            assert Path(state["config"]["cwd"]).is_dir()
            assert not list(Path(state["config"]["cwd"]).iterdir())
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def thread_start(self, **kwargs: Any) -> FakeThread:
            state["thread"] = kwargs
            return FakeThread()

    sdk = SimpleNamespace(
        AsyncCodex=FakeCodex,
        ApprovalMode=SimpleNamespace(deny_all="deny-all"),
        CodexConfig=FakeConfig,
        Sandbox=SimpleNamespace(read_only="read-only"),
    )
    monkeypatch.setattr(providers, "import_module", lambda name: sdk)

    outcome = await CodexAnalyzer(model="model-x", enrich_web=enrich_web).analyze([finding(1)])
    assert len(outcome.recommendations) == 1
    assert state["config"]["config_overrides"] == (f'web_search="{web_mode}"',)
    assert state["thread"]["ephemeral"] is True
    assert state["thread"]["approval_mode"] == "deny-all"
    assert state["thread"]["sandbox"] == "read-only"
    assert state["run"][1]["output_schema"]["type"] == "object"


@pytest.mark.asyncio
async def test_codex_adapter_uses_explicit_native_skill_input(
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

    class FakeConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)
            state["config"] = kwargs

    class FakeThread:
        async def run(self, run_input: Any, **kwargs: Any) -> Any:
            state["run_input"] = run_input
            skill_input = run_input[0]
            staged_path = Path(skill_input.path)
            state["staged_skill_exists"] = staged_path.is_file()
            state["staged_reference"] = (
                staged_path.parent / "references" / "policy.md"
            ).read_text(encoding="utf-8")
            raw = RecommendationBatch(
                recommendations=[recommendation(finding(1).finding_id)]
            ).model_dump_json()
            return SimpleNamespace(final_response=raw)

    class FakeCodex:
        def __init__(self, config: Any) -> None:
            self.config = config

        async def __aenter__(self) -> FakeCodex:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def thread_start(self, **kwargs: Any) -> FakeThread:
            return FakeThread()

    sdk = SimpleNamespace(
        AsyncCodex=FakeCodex,
        ApprovalMode=SimpleNamespace(deny_all="deny-all"),
        CodexConfig=FakeConfig,
        Sandbox=SimpleNamespace(read_only="read-only"),
        SkillInput=FakeSkillInput,
        TextInput=FakeTextInput,
    )
    monkeypatch.setattr(providers, "import_module", lambda name: sdk)

    selected_skill = load_skill(create_test_skill(tmp_path))
    outcome = await CodexAnalyzer(skill=selected_skill).analyze([finding(1)])

    run_input = state["run_input"]
    assert isinstance(run_input[0], FakeSkillInput)
    assert run_input[0].name == "trivy-test"
    assert "/.agents/skills/trivy-test/SKILL.md" in run_input[0].path
    assert isinstance(run_input[1], FakeTextInput)
    assert "BEGIN_UNTRUSTED_TRIVY_FACTS" in run_input[1].text
    assert state["staged_skill_exists"] is True
    assert state["staged_reference"] == "Prefer supported releases.\n"
    assert outcome.skill_name == "trivy-test"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enrich_web, expected_tools",
    [(False, []), (True, ["WebSearch", "WebFetch"])],
)
async def test_claude_adapter_isolated_tools_and_structured(
    monkeypatch: pytest.MonkeyPatch,
    enrich_web: bool,
    expected_tools: list[str],
) -> None:
    state: dict[str, Any] = {}

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            state["options"] = kwargs

    class FakeResult:
        def __init__(self, structured_output: Any) -> None:
            self.structured_output = structured_output

    async def fake_query(*, prompt: str, options: Any):
        state["prompt"] = prompt
        state["passed_options"] = options
        data = RecommendationBatch(
            recommendations=[recommendation(finding(1).finding_id)]
        ).model_dump(mode="json")
        yield FakeResult(data)

    sdk = SimpleNamespace(
        ClaudeAgentOptions=FakeOptions,
        ResultMessage=FakeResult,
        query=fake_query,
    )
    monkeypatch.setattr(providers, "import_module", lambda name: sdk)

    outcome = await ClaudeAnalyzer(enrich_web=enrich_web).analyze([finding(1)])
    assert len(outcome.recommendations) == 1
    options = state["options"]
    assert options["tools"] == expected_tools
    assert options["allowed_tools"] == expected_tools
    assert options["permission_mode"] == "dontAsk"
    assert options["setting_sources"] == []
    assert options["skills"] == []
    assert options["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert options["strict_mcp_config"] is True
    assert options["mcp_servers"] == {}
    assert Path(options["cwd"]).exists() is False


@pytest.mark.asyncio
async def test_claude_adapter_explicitly_invokes_only_selected_skill(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state: dict[str, Any] = {}

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            state["options"] = kwargs

    class FakeResult:
        def __init__(self, structured_output: Any) -> None:
            self.structured_output = structured_output

    async def fake_query(*, prompt: str, options: FakeOptions):
        state["prompt"] = prompt
        cwd = Path(options.kwargs["cwd"])
        staged = cwd / ".claude" / "skills" / "trivy-test"
        state["staged_skill_exists"] = (staged / "SKILL.md").is_file()
        state["staged_reference"] = (staged / "references" / "policy.md").read_text(
            encoding="utf-8"
        )
        state["config_dir_exists"] = Path(
            options.kwargs["env"]["CLAUDE_CONFIG_DIR"]
        ).is_dir()
        data = RecommendationBatch(
            recommendations=[recommendation(finding(1).finding_id)]
        ).model_dump(mode="json")
        yield FakeResult(data)

    sdk = SimpleNamespace(
        ClaudeAgentOptions=FakeOptions,
        ResultMessage=FakeResult,
        query=fake_query,
    )
    monkeypatch.setattr(providers, "import_module", lambda name: sdk)

    selected_skill = load_skill(create_test_skill(tmp_path))
    outcome = await ClaudeAnalyzer(skill=selected_skill).analyze([finding(1)])

    options = state["options"]
    assert options["tools"] == ["Skill"]
    assert options["allowed_tools"] == ["Skill"]
    assert options["setting_sources"] == ["project"]
    assert options["skills"] == ["trivy-test"]
    assert state["prompt"].startswith("/trivy-test\n\n")
    assert state["staged_skill_exists"] is True
    assert state["staged_reference"] == "Prefer supported releases.\n"
    assert state["config_dir_exists"] is True
    assert outcome.skill_name == "trivy-test"


def install_fake_gemini_adk(
    monkeypatch: pytest.MonkeyPatch,
    state: dict[str, Any],
    *,
    emit_skill_response: bool = True,
    skill_response_name: str = "trivy-test",
    emit_final_response: bool = True,
    raw_response: str | None = None,
) -> None:
    """Install a small module-shaped ADK fake at the lazy import boundary."""

    state["imports"] = []
    state["emit_skill_response"] = emit_skill_response
    state["emit_final_response"] = emit_final_response
    state["raw_response"] = raw_response or RecommendationBatch(
        recommendations=[recommendation(finding(1).finding_id)]
    ).model_dump_json()

    class FakePart:
        def __init__(
            self,
            text: str | None = None,
            function_response: Any | None = None,
            **kwargs: Any,
        ) -> None:
            self.text = text
            self.function_response = function_response
            self.__dict__.update(kwargs)

        @classmethod
        def from_text(cls, *, text: str) -> FakePart:
            return cls(text=text)

    class FakeContent:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class FakeRunConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)
            state["run_config"] = kwargs

    class FakeAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            state["agent"] = kwargs

    class FakeSessionService:
        def __init__(self) -> None:
            state["session_service_created"] = True

        async def create_session(self, *args: Any, **kwargs: Any) -> Any:
            state["create_session"] = (args, kwargs)
            session_id = kwargs.get("session_id", "gemini-test-session")
            return SimpleNamespace(id=session_id)

    class FakeSkillToolset:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            skills = kwargs.get("skills", args[0] if args else None)
            state["skill_toolset"] = {"skills": skills, **kwargs}

        async def close(self) -> None:
            state["skill_toolset_closed"] = True

    class FakeFunctionResponse:
        def __init__(self, name: str, response: dict[str, Any]) -> None:
            self.name = name
            self.response = response

    class FakeEvent:
        def __init__(
            self,
            *,
            final: bool = False,
            text: str | None = None,
            function_response: FakeFunctionResponse | None = None,
        ) -> None:
            self._final = final
            self.content = FakeContent(
                parts=[FakePart(text=text, function_response=function_response)]
            )
            self._function_responses = (
                [function_response] if function_response is not None else []
            )

        def is_final_response(self) -> bool:
            return self._final

        def get_function_responses(self) -> list[FakeFunctionResponse]:
            return self._function_responses

        def get_function_calls(self) -> list[Any]:
            return []

    class FakeRunner:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            state["runner"] = (args, kwargs)
            self.session_service = kwargs.get("session_service") or FakeSessionService()

        async def __aenter__(self) -> FakeRunner:
            state["runner_entered"] = True
            return self

        async def __aexit__(self, *args: Any) -> None:
            await self.close()

        async def close(self) -> None:
            state["runner_closed"] = state.get("runner_closed", 0) + 1

        async def run_async(self, *args: Any, **kwargs: Any):
            state["run"] = (args, kwargs)
            if state.get("loaded_skill") is not None and state["emit_skill_response"]:
                response = FakeFunctionResponse(
                    "load_skill",
                    {
                        "skill_name": skill_response_name,
                        "instructions": "Do not execute fixes.",
                    },
                )
                yield FakeEvent(function_response=response)
            if state["emit_final_response"]:
                yield FakeEvent(final=True, text=state["raw_response"])

    loaded_skill = SimpleNamespace(name="trivy-test")

    def fake_load_skill_from_dir(*args: Any, **kwargs: Any) -> Any:
        selected = Path(args[0] if args else kwargs["path"])
        state["loaded_skill"] = loaded_skill
        state["loaded_skill_dir"] = selected
        state["staged_skill_exists"] = (selected / "SKILL.md").is_file()
        state["staged_reference"] = (selected / "references" / "policy.md").read_text(
            encoding="utf-8"
        )
        state["staged_script_exists"] = (selected / "scripts" / "unsafe.py").is_file()
        return loaded_skill

    combined = SimpleNamespace(
        Agent=FakeAgent,
        LlmAgent=FakeAgent,
        Runner=FakeRunner,
        InMemorySessionService=FakeSessionService,
        SkillToolset=FakeSkillToolset,
        load_skill_from_dir=fake_load_skill_from_dir,
        Content=FakeContent,
        Part=FakePart,
        RunConfig=FakeRunConfig,
    )
    modules = {
        "google.adk": combined,
        "google.adk.agents": combined,
        "google.adk.runners": combined,
        "google.adk.sessions": combined,
        "google.adk.skills": combined,
        "google.adk.tools": combined,
        "google.adk.tools.skill_toolset": combined,
        "google.genai.types": combined,
    }

    def fake_import(name: str) -> Any:
        state["imports"].append(name)
        try:
            return modules[name]
        except KeyError as exc:
            raise ModuleNotFoundError(name) from exc

    monkeypatch.setattr(providers, "import_module", fake_import)


@pytest.mark.asyncio
async def test_gemini_adapter_uses_ephemeral_adk_session_and_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {}
    install_fake_gemini_adk(monkeypatch, state)

    outcome = await GeminiAnalyzer().analyze([finding(1)])

    assert outcome.provider == "gemini"
    assert outcome.model == "gemini-3.6-flash"
    assert len(outcome.recommendations) == 1
    agent = state["agent"]
    assert agent["model"] == "gemini-3.6-flash"
    assert agent["output_schema"] is RecommendationBatch
    assert agent["tools"] == []
    assert state["session_service_created"] is True
    assert state["create_session"]
    run_kwargs = state["run"][1]
    prompt = run_kwargs["new_message"].parts[0].text
    assert "BEGIN_UNTRUSTED_TRIVY_FACTS" in prompt
    assert state["runner_entered"] is True
    assert state["runner_closed"] >= 1


@pytest.mark.asyncio
async def test_gemini_adapter_stages_and_explicitly_loads_only_selected_skill(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state: dict[str, Any] = {}
    skill_dir = create_test_skill(tmp_path)
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "unsafe.py").write_text("raise SystemExit('must not run')\n", encoding="utf-8")
    install_fake_gemini_adk(monkeypatch, state)

    selected_skill = load_skill(skill_dir)
    outcome = await GeminiAnalyzer(skill=selected_skill).analyze([finding(1)])

    assert state["loaded_skill_dir"].parts[-2:] == ("skills", "trivy-test")
    assert state["staged_skill_exists"] is True
    assert state["staged_reference"] == "Prefer supported releases.\n"
    assert state["staged_script_exists"] is True
    toolset = state["skill_toolset"]
    assert toolset["skills"] == [state["loaded_skill"]]
    assert toolset["tool_filter"] == ["load_skill", "load_skill_resource"]
    assert toolset.get("code_executor") is None
    assert toolset.get("environment") is None
    assert toolset.get("additional_tools") in (None, [])
    assert "run_skill_script" not in toolset["tool_filter"]
    assert state["agent"]["tools"]
    instruction = state["agent"]["instruction"]
    assert "load_skill" in instruction
    assert "trivy-test" in instruction
    assert outcome.skill_name == "trivy-test"


@pytest.mark.asyncio
async def test_gemini_rejects_result_when_exact_skill_was_not_loaded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state: dict[str, Any] = {}
    skill_dir = create_test_skill(tmp_path)
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "unsafe.py").write_text("pass\n", encoding="utf-8")
    install_fake_gemini_adk(
        monkeypatch,
        state,
        skill_response_name="different-skill",
    )

    selected_skill = load_skill(skill_dir)
    with pytest.raises(ProviderResponseError, match="load_skill|Skill"):
        await GeminiAnalyzer(skill=selected_skill).analyze([finding(1)])


@pytest.mark.asyncio
async def test_gemini_requires_a_final_structured_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {}
    install_fake_gemini_adk(monkeypatch, state, emit_final_response=False)

    with pytest.raises(ProviderResponseError, match="最终|结构化"):
        await GeminiAnalyzer().analyze([finding(1)])


def test_gemini_web_enrichment_is_rejected_before_sdk_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def should_not_import(name: str) -> Any:  # pragma: no cover - assertion helper
        raise AssertionError(f"SDK import should not occur: {name}")

    monkeypatch.setattr(providers, "import_module", should_not_import)
    with pytest.raises(ProviderConfigurationError, match="Gemini|联网|enrich"):
        GeminiAnalyzer(enrich_web=True)


def test_factory_creates_gemini_and_lists_it_in_unknown_provider_error() -> None:
    analyzer = create_analyzer(" GEMINI ")

    assert isinstance(analyzer, GeminiAnalyzer)
    assert analyzer.model == "gemini-3.6-flash"
    with pytest.raises(ProviderConfigurationError, match="gemini"):
        create_analyzer("unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "extra"),
    [("codex", "codex"), ("gemini", "gemini")],
)
async def test_optional_sdk_import_is_lazy_and_clear(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    extra: str,
) -> None:
    analyzer = create_analyzer(provider)

    def missing(name: str) -> Any:
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(providers, "import_module", missing)
    with pytest.raises(ProviderUnavailableError, match=rf"uv sync --extra {extra}"):
        await analyzer.analyze([finding(1)])
