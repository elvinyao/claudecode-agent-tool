from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_core.contracts import AgentRequest, ProviderResult, RunStatus
from agent_core.providers import ProviderCapabilities
from agent_core.providers.base import BaseProvider
from agent_core.providers.codex_schema import validate_codex_output_schema
from ankify.eval.fixture_loader import load_eval_fixtures
from ankify.eval.judge import build_judge_prompt, judge_generated_cards
from ankify.eval.models import (
    EvalCaseResult,
    EvalCaseStatus,
    EvalJudgeResponse,
    EvalJudgeScores,
    EvalMode,
    EvalReport,
    EvalRuleResult,
)
from ankify.eval.reporter import (
    build_markdown_report,
    build_terminal_summary,
    serialize_json_report,
    write_eval_reports,
)
from ankify.models import AgentConfidence, AnkifyCard, ProvenanceType


def _card() -> AnkifyCard:
    return AnkifyCard(
        note_id="a" * 64,
        front="相談するとは？",
        back="人に意見を聞くこと。",
        tags=("language", "vocabulary", "jlpt-n4"),
        provenance=ProvenanceType.SOURCE,
        source_block_ids=("b" * 64,),
        evidence_quotes=("相談する",),
        learning_objective="相談するの意味",
        confidence=AgentConfidence.HIGH,
    )


class FakeJudgeProvider(BaseProvider):
    name = "fake-judge"
    capabilities = ProviderCapabilities(structured_output=True)

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.requests: list[AgentRequest[Any]] = []

    async def _execute(self, request: AgentRequest[Any]) -> ProviderResult[Any]:
        self.requests.append(request)
        output = EvalJudgeResponse(
            scores=EvalJudgeScores(
                atomicity=90,
                grounding=80,
                learner_fit=70,
                usefulness=80,
                domain_fit=80,
            ),
            reasons=["Focused."],
            suggestions=["Add usage context."],
        )
        return ProviderResult(
            request_id=request.request_id,
            provider=self.name,
            model=self.model,
            output=output,
        )


def test_judge_wire_schema_satisfies_codex_strict_output() -> None:
    validate_codex_output_schema(EvalJudgeResponse.model_json_schema())


def test_judge_prompt_marks_payload_untrusted_and_includes_semantic_rubric() -> None:
    fixture = load_eval_fixtures()[4]

    prompt = build_judge_prompt(fixture, (_card(),))

    assert "<BEGIN_UNTRUSTED_EVAL_PAYLOAD>" in prompt
    assert "junior_math_strategy" in prompt
    assert fixture.rubric_notes[0] in prompt
    assert fixture.source.text in prompt


@pytest.mark.asyncio
async def test_judge_uses_provider_adapter_and_program_computes_average() -> None:
    provider = FakeJudgeProvider(model="judge-model")
    fixture = load_eval_fixtures()[0]

    result = await judge_generated_cards(provider, fixture, (_card(),))

    assert result.overall_score == 80
    assert result.provider == "fake-judge"
    assert provider.requests[0].response_model is EvalJudgeResponse
    assert provider.requests[0].tool_policy.web_access is False


def _report() -> EvalReport:
    fixture = load_eval_fixtures()[0]
    return EvalReport(
        run_id="eval-2026-08-24T00-00-00-000Z",
        mode=EvalMode.LOCAL,
        created_at=datetime(2026, 8, 24, tzinfo=timezone.utc).isoformat(),
        generation_provider="fake-generate",
        generation_model="generate-model",
        judge_provider="fake-judge",
        judge_model="judge-model",
        self_judged=False,
        fixture_score_threshold=75,
        overall_score_threshold=80,
        overall_average=80,
        passed=True,
        results=(
            EvalCaseResult(
                fixture_id=fixture.id,
                title=fixture.title,
                strategy_profile=fixture.options.guide_profile,
                strategy_version=fixture.options.strategy_version or "missing",
                runtime_status=RunStatus.SUCCEEDED,
                cards=(_card(),),
                rule_result=EvalRuleResult(passed=True),
                judge_result=None,
                status=EvalCaseStatus.PASS,
                total_score=80,
                error_message=None,
            ),
        ),
    )


def test_reporters_are_stable_and_write_both_formats(tmp_path: Path) -> None:
    report = _report()

    first_json = serialize_json_report(report)
    second_json = serialize_json_report(report)
    markdown = build_markdown_report(report)
    terminal = build_terminal_summary(report)
    json_path, markdown_path = write_eval_reports(report, tmp_path)

    assert first_json == second_json
    assert json.loads(first_json)["overall_average"] == 80
    assert "# Ankify Agent Eval Report" in markdown
    assert "language-jlpt-vocabulary" in markdown
    assert "PASS language-jlpt-vocabulary" in terminal
    assert json_path.read_text(encoding="utf-8") == first_json
    assert markdown_path.read_text(encoding="utf-8") == markdown
