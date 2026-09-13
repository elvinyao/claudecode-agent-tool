from __future__ import annotations

import asyncio
import json

import pytest

from agent_core.contracts import ProviderResult, RunStatus
from agent_core.ownership import FieldOwnership, OwnershipSurface
from agent_core.providers import (
    ProviderAdapter,
    ProviderCapabilities,
    ProviderRegistry,
    ProviderUnavailableError,
)
from agent_core.registry import PluginRegistry
from agent_core.runtime import AgentRuntime
from trivy_ai_report.domain import (
    TrivyAgentBatchOutcome,
    TrivyWorkflowInput,
    finalize_analysis,
    finalize_batch_outcomes,
    parse_input_bytes,
    plan_batches,
)
from trivy_ai_report.models import (
    Evidence,
    ProviderRecommendation,
    ProviderRecommendationBatch,
    Recommendation,
    RecommendationCategory,
    ResearchStatus,
    TrivyRunOptions,
    VersionSource,
)
from trivy_ai_report.plugin import (
    TrivyPlugin,
    bundled_skill_path,
    create_plugin,
    load_bundled_skill,
    plan_agent_requests,
)
from trivy_ai_report.trivy import TrivyInputError


def _report(count: int) -> bytes:
    vulnerabilities = [
        {
            "VulnerabilityID": f"CVE-2026-{index:04d}",
            "PkgName": f"pkg-{index}",
            "InstalledVersion": "1.0",
            "FixedVersion": "1.1",
            "Status": "fixed",
            "Severity": "HIGH",
        }
        for index in range(count)
    ]
    return json.dumps(
        {
            "SchemaVersion": 2,
            "ArtifactName": "example:latest",
            "ArtifactType": "container_image",
            "Results": [
                {
                    "Target": "example:latest",
                    "Class": "os-pkgs",
                    "Type": "alpine",
                    "Vulnerabilities": vulnerabilities,
                }
            ],
        }
    ).encode()


def _named_report(*, artifact: str, vulnerability: str, package: str) -> bytes:
    return json.dumps(
        {
            "SchemaVersion": 2,
            "ArtifactName": artifact,
            "ArtifactType": "container_image",
            "Results": [
                {
                    "Target": artifact,
                    "Class": "os-pkgs",
                    "Type": "alpine",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": vulnerability,
                            "PkgName": package,
                            "InstalledVersion": "1.0",
                            "FixedVersion": "1.1",
                            "Status": "fixed",
                            "Severity": "HIGH",
                        }
                    ],
                }
            ],
        }
    ).encode()


def _provider_recommendation(advice: Recommendation) -> ProviderRecommendation:
    """Populate the explicit wire contract from a domain fixture."""

    return ProviderRecommendation.model_validate(advice.model_dump(mode="python"))


def test_batches_are_stable_and_use_domain_defaults() -> None:
    offline = plan_batches(parse_input_bytes(_report(26), TrivyRunOptions()))
    online = plan_batches(parse_input_bytes(_report(26), TrivyRunOptions(enrich_web=True)))

    assert [item.batch_id for item in offline] == ["batch-0001", "batch-0002"]
    assert [len(item.findings) for item in offline] == [25, 1]
    assert [len(item.findings) for item in online] == [10, 10, 6]


def test_invalid_utf8_is_a_domain_input_error() -> None:
    with pytest.raises(TrivyInputError, match="UTF-8"):
        parse_input_bytes(b"\xff", TrivyRunOptions())


def test_offline_finalization_strips_agent_citations() -> None:
    parsed = parse_input_bytes(_report(1), TrivyRunOptions())
    finding = parsed.report.findings[0]
    advice = Recommendation(
        finding_id=finding.finding_id,
        category=RecommendationCategory.OS_PACKAGE_UPGRADE,
        title_zh="升级软件包",
        rationale_zh="使用发行版安全更新。",
        actions_zh=["升级并重建镜像。"],
        validation_zh=["重新运行 Trivy。"],
        recommended_version="1.1",
        version_source=VersionSource.TRIVY_FIXED_VERSION,
        research_status=ResearchStatus.ENRICHED,
        evidence=[Evidence(title="untrusted", url="https://example.com", claim_zh="claim")],
    )

    outcome = finalize_analysis(
        parsed,
        [advice],
        provider="codex",
        model="test",
        skill_name=None,
    )

    assert outcome.partial is False
    assert outcome.recommendations[0].research_status is ResearchStatus.NOT_REQUESTED
    assert outcome.recommendations[0].evidence == []


def test_plugin_plans_domain_prompt_and_strict_wire_schema() -> None:
    parsed = parse_input_bytes(_report(1), TrivyRunOptions(enrich_web=True))

    request = plan_agent_requests(parsed)[0]

    assert request.request_id == "batch-0001"
    assert request.response_model is ProviderRecommendationBatch
    assert request.tool_policy.web_access is True
    assert "Trivy" in request.system_prompt
    assert parsed.report.findings[0].finding_id in request.prompt
    assert request.metadata["finding_ids"] == (parsed.report.findings[0].finding_id,)


def test_provider_wire_schema_requires_every_object_property() -> None:
    schema = ProviderRecommendationBatch.model_json_schema()

    def assert_all_properties_required(value: object) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert set(value.get("required", ())) == set(value["properties"])
            for child in value.values():
                assert_all_properties_required(child)
        elif isinstance(value, list):
            for child in value:
                assert_all_properties_required(child)

    assert_all_properties_required(schema)
    recommended_version = schema["$defs"]["ProviderRecommendation"]["properties"][
        "recommended_version"
    ]
    assert {variant.get("type") for variant in recommended_version["anyOf"]} == {
        "string",
        "null",
    }


def test_provider_wire_model_rejects_omitted_explicit_fields() -> None:
    parsed = parse_input_bytes(_report(1), TrivyRunOptions())
    finding_id = parsed.report.findings[0].finding_id

    with pytest.raises(ValueError, match="recommended_version"):
        ProviderRecommendation.model_validate(
            {
                "finding_id": finding_id,
                "category": "os_package_upgrade",
                "title_zh": "升级软件包",
                "rationale_zh": "使用发行版安全更新。",
                "actions_zh": ["升级并重建镜像。"],
                "validation_zh": ["重新运行 Trivy。"],
            }
        )


def test_bundled_skill_is_wheel_local_and_valid() -> None:
    assert bundled_skill_path().is_file()
    assert load_bundled_skill().name == "trivy-remediation"


def test_trivy_factory_registers_with_strict_schemas_and_fresh_instances() -> None:
    registry = PluginRegistry()
    descriptor = registry.register("trivy", create_plugin)

    assert descriptor.options_schema["additionalProperties"] is False
    assert descriptor.output_schema["title"] == "RenderedTrivyReport"
    assert descriptor.artifact_content_schema is None
    assert descriptor.ownership is not None
    fields = {(field.surface, field.path): field.ownership for field in descriptor.ownership.fields}
    assert fields[(OwnershipSurface.INPUT, "/content")] is FieldOwnership.PROGRAM_FACT
    assert fields[(OwnershipSurface.OPTIONS, "/enrich_web")] is FieldOwnership.USER_CHOICE
    assert not any(
        field.surface is OwnershipSurface.ARTIFACT_CONTENT for field in descriptor.ownership.fields
    )
    assert registry.create_for_run("trivy") is not registry.create_for_run("trivy")


def test_failed_batches_become_deterministic_partial_fallbacks() -> None:
    parsed = parse_input_bytes(_report(2), TrivyRunOptions(batch_size=1))
    first, second = plan_agent_requests(parsed)
    finding = parsed.report.findings[0]
    advice = Recommendation(
        finding_id=finding.finding_id,
        category=RecommendationCategory.OS_PACKAGE_UPGRADE,
        title_zh="升级软件包",
        rationale_zh="使用发行版安全更新。",
        actions_zh=["升级并重建镜像。"],
        validation_zh=["重新运行 Trivy。"],
        recommended_version="1.1",
        version_source=VersionSource.TRIVY_FIXED_VERSION,
    )
    successful = TrivyAgentBatchOutcome(
        batch_id=first.request_id,
        result=ProviderResult(
            request_id=first.request_id,
            provider="codex",
            model="test-model",
            output=first.response_model(
                recommendations=[_provider_recommendation(advice)]
            ).to_domain(),
        ),
    )
    failed = TrivyAgentBatchOutcome(
        batch_id=second.request_id,
        error_code="provider_timeout",
        error_message="Provider 请求超时",
        partial=True,
    )

    outcome = finalize_batch_outcomes(
        parsed,
        (successful, failed),
        provider="codex",
        requested_model=None,
        skill_name=None,
    )

    assert outcome.partial is True
    assert outcome.model == "test-model"
    assert len(outcome.recommendations) == 2
    assert any("batch-0002" in warning for warning in outcome.warnings)


def test_provider_partial_flag_propagates_without_relying_on_warning_text() -> None:
    parsed = parse_input_bytes(_report(1), TrivyRunOptions())
    request = plan_agent_requests(parsed)[0]
    result = ProviderResult(
        request_id=request.request_id,
        provider="codex",
        model="test-model",
        output=request.response_model(recommendations=[]).to_domain(),
        partial=True,
    )

    outcome = finalize_batch_outcomes(
        parsed,
        (
            TrivyAgentBatchOutcome(
                batch_id=request.request_id,
                result=result,
                partial=True,
            ),
        ),
        provider="codex",
        requested_model=None,
        skill_name=None,
    )

    assert outcome.partial is True


@pytest.mark.asyncio
async def test_trivy_workflow_degrades_without_losing_the_report() -> None:
    class FailingProvider:
        name = "codex"
        model = "fake-codex"
        capabilities = ProviderCapabilities()

        async def execute(self, request, *, timeout_seconds=None):
            del request
            raise ProviderUnavailableError("not installed", provider="codex")

    class Runtime:
        provider: ProviderAdapter = FailingProvider()
        provider_name = "codex"
        model = None
        skill_name = None
        attempt_timeout_seconds: float | None = 1.0
        max_agent_concurrency = 1

    workflow = TrivyPlugin().create_workflow(Runtime())
    result = await workflow.execute(
        TrivyWorkflowInput(
            content=_report(1),
            options=TrivyRunOptions(use_bundled_skill=False),
        )
    )

    artifact = result.final_outputs[0]
    assert result.status.value == "degraded"
    assert artifact.partial is True
    assert b"<!doctype html>" in artifact.content.lower()


@pytest.mark.asyncio
async def test_trivy_workflow_uses_typed_provider_result_end_to_end() -> None:
    raw = _report(1)
    finding = parse_input_bytes(raw, TrivyRunOptions()).report.findings[0]
    advice = ProviderRecommendation(
        finding_id=finding.finding_id,
        category=RecommendationCategory.OS_PACKAGE_UPGRADE,
        title_zh="经过类型校验的升级建议",
        rationale_zh="使用发行版安全更新。",
        actions_zh=["升级并重建镜像。"],
        validation_zh=["重新运行 Trivy。"],
        recommended_version="1.1",
        version_source=VersionSource.TRIVY_FIXED_VERSION,
        confidence="high",
        research_status=ResearchStatus.NOT_REQUESTED,
        evidence=[],
    )

    class SuccessfulProvider:
        name = "codex"
        model = "fake-codex"
        capabilities = ProviderCapabilities()

        async def execute(self, request, *, timeout_seconds=None):
            return ProviderResult(
                request_id=request.request_id,
                provider="codex",
                model="fake-codex",
                output=request.response_model(recommendations=[advice]),
            )

    class Runtime:
        provider: ProviderAdapter = SuccessfulProvider()
        provider_name = "codex"
        model = None
        skill_name: str | None = "trivy-remediation"
        attempt_timeout_seconds: float | None = 1.0
        max_agent_concurrency = 1

    result = (
        await TrivyPlugin()
        .create_workflow(Runtime())
        .execute(
            TrivyWorkflowInput(
                content=raw,
                options=TrivyRunOptions(use_bundled_skill=False),
            )
        )
    )

    artifact = result.final_outputs[0]
    assert result.status.value == "succeeded"
    assert artifact.partial is False
    assert "经过类型校验的升级建议" in artifact.content.decode()


@pytest.mark.asyncio
async def test_trivy_workflow_rejects_cross_batch_finding_identity() -> None:
    raw = _report(2)
    parsed = parse_input_bytes(raw, TrivyRunOptions(batch_size=1))
    first_finding, second_finding = parsed.report.findings

    def advice_for(finding_id: str) -> ProviderRecommendation:
        return ProviderRecommendation(
            finding_id=finding_id,
            category=RecommendationCategory.OS_PACKAGE_UPGRADE,
            title_zh="跨批次建议",
            rationale_zh="不应被接受。",
            actions_zh=["拒绝该响应。"],
            validation_zh=["使用本地 fallback。"],
            recommended_version=None,
            version_source=VersionSource.NONE,
            confidence="low",
            research_status=ResearchStatus.NOT_REQUESTED,
            evidence=[],
        )

    class CrossBatchProvider:
        name = "codex"
        model = "fake-codex"
        capabilities = ProviderCapabilities()

        async def execute(self, request, *, timeout_seconds=None):
            wrong_id = (
                second_finding.finding_id
                if request.request_id == "batch-0001"
                else first_finding.finding_id
            )
            return ProviderResult(
                request_id=request.request_id,
                provider="codex",
                model="fake-codex",
                output=request.response_model(recommendations=[advice_for(wrong_id)]),
            )

    class Runtime:
        provider: ProviderAdapter = CrossBatchProvider()
        provider_name = "codex"
        model = None
        skill_name = None
        attempt_timeout_seconds: float | None = 1.0
        max_agent_concurrency = 1

    result = (
        await TrivyPlugin()
        .create_workflow(Runtime())
        .execute(
            TrivyWorkflowInput(
                content=raw,
                options=TrivyRunOptions(batch_size=1, use_bundled_skill=False),
            )
        )
    )

    artifact = result.final_outputs[0]
    assert result.status is RunStatus.DEGRADED
    assert artifact.partial is True
    assert "跨批次建议" not in artifact.content.decode()
    assert all("provider_response" in warning for warning in artifact.warnings)


@pytest.mark.asyncio
async def test_concurrent_runtime_runs_keep_trivy_artifacts_isolated() -> None:
    plugin_registry = PluginRegistry()
    plugin_registry.register("trivy", create_plugin)
    provider_registry = ProviderRegistry()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    active = 0
    max_active = 0
    calls = 0
    providers: list[object] = []

    class CoordinatedProvider:
        name = "fake"
        model = "fake-model"
        capabilities = ProviderCapabilities()

        async def execute(self, request, *, timeout_seconds=None):
            nonlocal active, max_active, calls
            del timeout_seconds
            calls += 1
            call_number = calls
            active += 1
            max_active = max(max_active, active)
            try:
                if call_number == 1:
                    first_started.set()
                    await release_first.wait()
                return ProviderResult(
                    request_id=request.request_id,
                    provider=self.name,
                    model=self.model,
                    output=request.response_model(recommendations=[]),
                )
            finally:
                active -= 1

    def provider_factory(*, model, skills):
        del model
        assert [skill.name for skill in skills] == ["trivy-remediation"]
        provider = CoordinatedProvider()
        providers.append(provider)
        return provider

    provider_registry.register("fake", provider_factory)
    runtime = AgentRuntime(
        plugin_registry,
        provider_registry=provider_registry,
        default_deadline_seconds=3,
        attempt_timeout_seconds=1,
    )
    first = asyncio.create_task(
        runtime.run(
            plugin_id="trivy",
            provider="fake",
            input_bytes=_named_report(
                artifact="isolated-a:latest",
                vulnerability="CVE-2026-1001",
                package="only-package-a",
            ),
            run_id="trivy-concurrent-a",
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second = asyncio.create_task(
        runtime.run(
            plugin_id="trivy",
            provider="fake",
            input_bytes=_named_report(
                artifact="isolated-b:latest",
                vulnerability="CVE-2026-2002",
                package="only-package-b",
            ),
            run_id="trivy-concurrent-b",
        )
    )
    await asyncio.sleep(0.05)

    # Provider capacity is shared across runs, rather than recreated per runtime call.
    assert max_active == 1
    release_first.set()
    first_result, second_result = await asyncio.gather(first, second)

    first_html = first_result.artifact.content.decode()
    second_html = second_result.artifact.content.decode()
    assert "isolated-a:latest" in first_html
    assert "only-package-a" in first_html
    assert "isolated-b:latest" not in first_html
    assert "only-package-b" not in first_html
    assert "isolated-b:latest" in second_html
    assert "only-package-b" in second_html
    assert "isolated-a:latest" not in second_html
    assert "only-package-a" not in second_html
    assert max_active == 1
    assert len(providers) == 2
    assert providers[0] is not providers[1]
