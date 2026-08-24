from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_core.contracts import AgentRequest, ProviderResult
from agent_core.providers import ProviderCapabilities, ProviderRegistry
from agent_core.providers.base import BaseProvider
from agent_core.registry import PluginRegistry
from agent_core.runtime import AgentRuntime
from ankify.eval.cli import main as eval_main
from ankify.eval.fixture_loader import load_eval_fixtures
from ankify.eval.models import (
    EvalCaseStatus,
    EvalJudgeResponse,
    EvalJudgeScores,
    EvalMode,
)
from ankify.eval.runner import run_eval_harness
from ankify.models import (
    AgentConfidence,
    ProvenanceType,
    ProviderCardBatch,
    ProviderCardCandidate,
)
from ankify.plugin import create_plugin


class FakeEvalProvider(BaseProvider):
    name = "fake-eval"
    capabilities = ProviderCapabilities(structured_output=True)

    def __init__(
        self,
        *,
        card_count: int | None = None,
        judge_score: int = 90,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.card_count = card_count
        self.judge_score = judge_score
        self.requests: list[AgentRequest[Any]] = []

    async def _execute(self, request: AgentRequest[Any]) -> ProviderResult[Any]:
        self.requests.append(request)
        if request.response_model is EvalJudgeResponse:
            output: Any = EvalJudgeResponse(
                scores=EvalJudgeScores(
                    atomicity=self.judge_score,
                    grounding=self.judge_score,
                    learner_fit=self.judge_score,
                    usefulness=self.judge_score,
                    domain_fit=self.judge_score,
                ),
                reasons=["Focused and grounded."],
                suggestions=[],
            )
        else:
            count = self.card_count
            if count is None:
                count = int(request.metadata["target_card_count"])
            block_id = request.metadata["source_block_ids"][0]
            output = ProviderCardBatch(
                cards=[
                    ProviderCardCandidate(
                        note_type="basic",
                        front=f"語彙 {index + 1} の意味は？",
                        back=f"短い答え {index + 1}。",
                        source_block_ids=[block_id],
                        evidence_quotes=["語彙メモ"],
                        suggested_tags=[],
                        learning_objective=f"語彙目標 {index + 1}",
                        review_flags=[],
                        confidence=AgentConfidence.HIGH,
                        provenance=ProvenanceType.SOURCE,
                    )
                    for index in range(count)
                ]
            )
        return ProviderResult(
            request_id=request.request_id,
            provider=self.name,
            model=self.model,
            output=output,
        )


def _runtime(*, card_count: int | None = None) -> tuple[AgentRuntime, list[FakeEvalProvider]]:
    plugins = PluginRegistry()
    plugins.register("ankify", create_plugin)
    providers = ProviderRegistry()
    created: list[FakeEvalProvider] = []

    def factory(*, model=None, skills=()):
        provider = FakeEvalProvider(
            model=model,
            skills=skills,
            card_count=card_count,
        )
        created.append(provider)
        return provider

    providers.register("fake-eval", factory)
    return AgentRuntime(plugins, provider_registry=providers), created


def _clock() -> datetime:
    return datetime(2026, 8, 24, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_runner_uses_full_runtime_and_provider_judge_path() -> None:
    runtime, generated = _runtime()
    judge = FakeEvalProvider(model="shared-model")
    fixture = load_eval_fixtures()[0]

    report = await run_eval_harness(
        runtime=runtime,
        mode=EvalMode.LOCAL,
        generation_provider="fake-eval",
        generation_model="shared-model",
        judge_provider=judge,
        fixtures=(fixture,),
        now=_clock,
    )

    assert report.run_id == "eval-2026-08-24T00-00-00-000Z"
    assert report.passed is True
    assert report.self_judged is True
    assert report.results[0].status is EvalCaseStatus.PASS
    assert report.results[0].total_score == 90
    assert len(generated) == 1
    assert generated[0].requests[0].metadata["strategy_version"] == "language-v1"
    assert judge.requests[0].response_model is EvalJudgeResponse


@pytest.mark.asyncio
async def test_hard_failure_cannot_be_overridden_by_high_judge_score() -> None:
    runtime, _generated = _runtime(card_count=1)
    judge = FakeEvalProvider(judge_score=100)
    fixture = load_eval_fixtures()[0]

    report = await run_eval_harness(
        runtime=runtime,
        mode=EvalMode.LOCAL,
        generation_provider="fake-eval",
        judge_provider=judge,
        fixtures=(fixture,),
        now=_clock,
    )

    assert report.passed is False
    assert report.results[0].status is EvalCaseStatus.FAIL
    assert report.results[0].rule_result.passed is False
    assert report.results[0].judge_result is None
    assert judge.requests == []


@pytest.mark.asyncio
async def test_strict_mode_rejects_low_judge_score() -> None:
    runtime, _generated = _runtime()
    judge = FakeEvalProvider(judge_score=60)

    report = await run_eval_harness(
        runtime=runtime,
        mode=EvalMode.STRICT,
        generation_provider="fake-eval",
        judge_provider=judge,
        fixtures=(load_eval_fixtures()[0],),
        now=_clock,
    )

    assert report.passed is False
    assert report.overall_average == 60
    assert report.results[0].status is EvalCaseStatus.FAIL


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_passed"),
    [
        (EvalMode.LOCAL, EvalCaseStatus.SKIPPED, True),
        (EvalMode.STRICT, EvalCaseStatus.FAIL, False),
    ],
)
async def test_missing_generation_provider_has_mode_specific_behavior(
    mode: EvalMode,
    expected_status: EvalCaseStatus,
    expected_passed: bool,
) -> None:
    runtime, _generated = _runtime()

    report = await run_eval_harness(
        runtime=runtime,
        mode=mode,
        generation_provider=None,
        fixtures=(load_eval_fixtures()[0],),
        now=_clock,
    )

    assert report.passed is expected_passed
    assert report.results[0].status is expected_status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_passed"),
    [
        (EvalMode.LOCAL, EvalCaseStatus.WARN, True),
        (EvalMode.STRICT, EvalCaseStatus.FAIL, False),
    ],
)
async def test_missing_judge_provider_has_mode_specific_behavior(
    mode: EvalMode,
    expected_status: EvalCaseStatus,
    expected_passed: bool,
) -> None:
    runtime, _generated = _runtime()

    report = await run_eval_harness(
        runtime=runtime,
        mode=mode,
        generation_provider="fake-eval",
        judge_provider=None,
        fixtures=(load_eval_fixtures()[0],),
        now=_clock,
    )

    assert report.passed is expected_passed
    assert report.results[0].status is expected_status


def test_cli_local_mode_without_provider_writes_skipped_report(tmp_path: Path) -> None:
    code = eval_main(
        ["--output-directory", str(tmp_path), "--fixture", "language-jlpt-vocabulary"],
        provider_registry=ProviderRegistry(),
    )

    assert code == 0
    assert len(tuple(tmp_path.glob("*.json"))) == 1
    assert len(tuple(tmp_path.glob("*.md"))) == 1
