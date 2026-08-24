from __future__ import annotations

import json
from typing import Any

import pytest

from agent_core.contracts import AgentRequest, ProviderResult
from agent_core.providers import ProviderCapabilities, ProviderRegistry
from agent_core.providers.base import BaseProvider
from agent_core.providers.errors import ProviderAuthenticationError
from agent_core.registry import PluginRegistry
from agent_core.runtime import AgentRuntime
from ankify.models import (
    AgentConfidence,
    ProvenanceType,
    ProviderCardBatch,
    ProviderCardCandidate,
)
from ankify.plugin import create_plugin

SOURCE_TEXT = "全体を1=5/5と見て、5/5-3/5=2/5。"


class FakeCardProvider(BaseProvider):
    name = "fake-cards"
    capabilities = ProviderCapabilities(structured_output=True)

    def __init__(self, *, invalid_evidence: bool = False, fail: bool = False, **kwargs: Any):
        super().__init__(**kwargs)
        self.invalid_evidence = invalid_evidence
        self.fail = fail
        self.requests: list[AgentRequest[Any]] = []

    async def _execute(self, request: AgentRequest[Any]) -> ProviderResult[Any]:
        self.requests.append(request)
        if self.fail:
            raise ProviderAuthenticationError("fake authentication failure", provider=self.name)
        block_id = request.metadata["source_block_ids"][0]
        output = ProviderCardBatch(
            cards=[
                ProviderCardCandidate(
                    note_type="basic",
                    front="残りを求めるとき、全体はどう表す？",
                    back="全体を1=5/5と表す。",
                    source_block_ids=[block_id],
                    evidence_quotes=[
                        "source does not contain this"
                        if self.invalid_evidence
                        else "全体を1=5/5と見て"
                    ],
                    suggested_tags=["fraction"],
                    learning_objective="全体を分数で表す",
                    review_flags=[],
                    confidence=AgentConfidence.HIGH,
                    provenance=ProvenanceType.SOURCE,
                )
            ]
        )
        return ProviderResult(
            request_id=request.request_id,
            provider=self.name,
            model=self.model,
            output=output,
        )


def _runtime(*, invalid_evidence: bool = False, fail: bool = False):
    plugins = PluginRegistry()
    plugins.register("ankify", create_plugin)
    providers = ProviderRegistry()
    created: list[FakeCardProvider] = []

    def factory(*, model=None, skills=()):
        provider = FakeCardProvider(
            model=model,
            skills=skills,
            invalid_evidence=invalid_evidence,
            fail=fail,
        )
        created.append(provider)
        return provider

    providers.register("fake-cards", factory)
    return AgentRuntime(plugins, provider_registry=providers), created


@pytest.mark.asyncio
async def test_runtime_executes_full_ankify_path_and_returns_complete_artifact() -> None:
    runtime, created = _runtime()

    result = await runtime.run(
        plugin_id="ankify",
        provider="fake-cards",
        input_bytes=SOURCE_TEXT.encode(),
        input_filename="math.txt",
        options={
            "deck_name": "算数",
            "requested_card_count": 1,
            "use_bundled_skill": False,
        },
    )
    document = json.loads(result.artifact.content)

    assert result.status.value == "succeeded"
    assert result.partial is False
    assert document["status"] == "complete"
    assert len(document["cards"]) == 1
    assert document["cards"][0]["tags"] == ["ankify", "fraction"]
    assert created[0].requests[0].tool_policy.web_access is False


@pytest.mark.asyncio
async def test_runtime_rejects_bad_evidence_and_marks_artifact_degraded() -> None:
    runtime, _created = _runtime(invalid_evidence=True)

    result = await runtime.run(
        plugin_id="ankify",
        provider="fake-cards",
        input_bytes=SOURCE_TEXT.encode(),
        input_filename="math.txt",
        options={"requested_card_count": 1, "use_bundled_skill": False},
    )
    document = json.loads(result.artifact.content)

    assert result.status.value == "degraded"
    assert result.partial is True
    assert document["cards"] == []
    assert document["rejections"][0]["reason_codes"] == ["source_check"]


@pytest.mark.asyncio
async def test_runtime_provider_failure_uses_no_answer_fallback() -> None:
    runtime, created = _runtime(fail=True)

    result = await runtime.run(
        plugin_id="ankify",
        provider="fake-cards",
        input_bytes=SOURCE_TEXT.encode(),
        input_filename="math.txt",
        options={"requested_card_count": 1, "use_bundled_skill": False},
    )
    document = json.loads(result.artifact.content)

    assert result.status.value == "degraded"
    assert document["cards"] == []
    assert "no local answers were invented" in " ".join(document["warnings"])
    assert len(created[0].requests) == 1
