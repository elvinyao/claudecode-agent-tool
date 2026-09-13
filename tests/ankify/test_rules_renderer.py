from __future__ import annotations

import json
from typing import Any

from agent_core.contracts import ProviderResult
from ankify.domain import merge_batch_outcomes, plan_batches
from ankify.models import (
    AgentConfidence,
    AgentReviewCode,
    AnkifyAgentBatchOutcome,
    AnkifyRunOptions,
    AnkifyWorkflowInput,
    ProvenanceType,
    ProviderCardBatch,
    ProviderCardCandidate,
    QualityCode,
    QualityOrigin,
    SourceMode,
)
from ankify.renderer import render_artifact
from ankify.rules import validate_candidate
from ankify.source import parse_workflow_input


def _parsed(*, source_mode: SourceMode = SourceMode.MATERIALS_NOTES, **options):
    return parse_workflow_input(
        AnkifyWorkflowInput(
            content="全体を1=5/5と見て、5/5-3/5=2/5。残りを見落とした。".encode(),
            filename="math.txt",
            options=AnkifyRunOptions(source_mode=source_mode, **options),
        )
    )


def _candidate(parsed, **overrides):
    block = parsed.source.blocks[0]
    values: dict[str, Any] = {
        "note_type": "basic",
        "front": "残りを求めるとき、全体はどう表す？",
        "back": "全体を1=5/5と表す。",
        "source_block_ids": [block.block_id],
        "evidence_quotes": ["全体を1=5/5と見て"],
        "suggested_tags": ["fraction"],
        "learning_objective": "全体を同分母の分数で表す",
        "review_flags": [],
        "confidence": AgentConfidence.HIGH,
        "provenance": ProvenanceType.SOURCE,
    }
    values.update(overrides)
    return ProviderCardCandidate(**values)


def test_exact_source_evidence_accepts_card_and_stable_note_id() -> None:
    parsed = _parsed()
    block_id = parsed.source.blocks[0].block_id
    candidate = _candidate(parsed)

    first = validate_candidate(
        parsed,
        candidate,
        batch_id="batch-0001",
        candidate_index=0,
        allowed_block_ids=frozenset({block_id}),
    )
    second = validate_candidate(
        parsed,
        candidate,
        batch_id="batch-0001",
        candidate_index=0,
        allowed_block_ids=frozenset({block_id}),
    )

    assert first.rejection is None
    assert first.card is not None
    assert second.card is not None
    assert first.card.note_id == second.card.note_id
    assert first.card.tags == ("ankify", "fraction")


def test_missing_or_fabricated_evidence_rejects_strict_card() -> None:
    parsed = _parsed()
    block_id = parsed.source.blocks[0].block_id
    decision = validate_candidate(
        parsed,
        _candidate(parsed, evidence_quotes=["徳川家康が江戸幕府を開いた"]),
        batch_id="batch-0001",
        candidate_index=0,
        allowed_block_ids=frozenset({block_id}),
    )

    assert decision.card is None
    assert decision.rejection is not None
    assert QualityCode.SOURCE_CHECK in decision.rejection.reason_codes


def test_topic_scope_model_knowledge_is_kept_but_requires_review() -> None:
    parsed = _parsed(source_mode=SourceMode.TOPIC_SCOPE)
    candidate = _candidate(
        parsed,
        source_block_ids=[],
        evidence_quotes=[],
        provenance=ProvenanceType.MODEL_KNOWLEDGE,
        confidence=AgentConfidence.LOW,
        review_flags=[AgentReviewCode.STRATEGY_MISMATCH],
    )

    decision = validate_candidate(
        parsed,
        candidate,
        batch_id="batch-0001",
        candidate_index=0,
        allowed_block_ids=frozenset({parsed.source.blocks[0].block_id}),
    )

    assert decision.card is not None
    issues = decision.card.quality_issues
    assert {issue.code for issue in issues} == {
        QualityCode.SOURCE_CHECK,
        QualityCode.STRATEGY_MISMATCH,
        QualityCode.LOW_CONFIDENCE,
    }
    assert {issue.origin for issue in issues} == {
        QualityOrigin.RULE,
        QualityOrigin.AGENT_REVIEW,
    }


def test_merge_rejects_duplicate_and_renderer_is_canonical() -> None:
    parsed = _parsed(requested_card_count=2)
    candidate = _candidate(parsed)
    result = ProviderResult(
        request_id="batch-0001",
        provider="fake",
        model="fake-model",
        output=ProviderCardBatch(cards=[candidate, candidate]),
    )
    context = merge_batch_outcomes(
        parsed,
        (AnkifyAgentBatchOutcome(batch_id="batch-0001", result=result),),
    )

    first = render_artifact(context)
    second = render_artifact(context)
    document = json.loads(first.content)

    assert first.content == second.content
    assert first.partial is True
    assert len(document["cards"]) == 1
    assert document["status"] == "degraded"
    assert document["rejections"][0]["reason_codes"] == ["duplicate"]


def test_failed_batch_produces_no_invented_answer_and_degraded_artifact() -> None:
    parsed = _parsed()
    context = merge_batch_outcomes(
        parsed,
        (
            AnkifyAgentBatchOutcome(
                batch_id=plan_batches(parsed)[0].batch_id,
                error_code="provider_authentication",
                error_message="provider did not authenticate",
                partial=True,
            ),
        ),
    )
    document = json.loads(render_artifact(context).content)

    assert document["cards"] == []
    assert document["partial"] is True
    assert "no local answers were invented" in " ".join(document["warnings"])
