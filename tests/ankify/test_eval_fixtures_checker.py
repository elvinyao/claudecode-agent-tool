from __future__ import annotations

from ankify.eval.checker import SEMANTIC_DOMAIN_CHECKS, check_generated_cards
from ankify.eval.fixture_loader import (
    EVAL_FIXTURE_IDS,
    fixture_manifest_json,
    load_eval_fixtures,
)
from ankify.eval.models import EvalDomainCheck, EvalFixture
from ankify.models import AgentConfidence, AnkifyCard, ProvenanceType


def _cards(fixture: EvalFixture, count: int) -> tuple[AnkifyCard, ...]:
    evidence = fixture.source.text[:20]
    return tuple(
        AnkifyCard(
            note_id=f"{index + 1:064x}",
            front=f"Question {index + 1}?",
            back=f"Answer {index + 1}.",
            tags=fixture.expected_tags,
            provenance=ProvenanceType.SOURCE,
            source_block_ids=("a" * 64,),
            evidence_quotes=(evidence,),
            learning_objective=f"objective {index + 1}",
            confidence=AgentConfidence.HIGH,
        )
        for index in range(count)
    )


def test_loads_seven_versioned_fixtures_in_stable_order() -> None:
    fixtures = load_eval_fixtures()

    assert tuple(fixture.id for fixture in fixtures) == EVAL_FIXTURE_IDS
    assert len(fixtures) == 7
    assert all(len(fixture.source.text) >= 50 for fixture in fixtures)
    assert all(fixture.options.strategy_version for fixture in fixtures)
    assert all(fixture.options.guide_profile is not None for fixture in fixtures)


def test_fixture_manifest_excludes_full_source_text() -> None:
    fixtures = load_eval_fixtures()
    manifest = fixture_manifest_json()

    assert fixtures[0].id.value in manifest
    assert fixtures[0].source.text not in manifest


def test_checker_accepts_mechanically_valid_cards() -> None:
    fixture = load_eval_fixtures()[0]

    result = check_generated_cards(
        fixture,
        _cards(fixture, fixture.expected_card_count.minimum),
    )

    assert result.passed is True
    assert result.issues == ()


def test_checker_rejects_count_duplicates_and_fabricated_evidence() -> None:
    fixture = load_eval_fixtures()[0]
    first = _cards(fixture, 1)[0]
    duplicate = first.model_copy(
        update={
            "note_id": "f" * 64,
            "evidence_quotes": ("fabricated evidence",),
        }
    )

    result = check_generated_cards(fixture, (first, duplicate))

    assert result.passed is False
    checks = {issue.check for issue in result.issues}
    assert EvalDomainCheck.COUNT_RANGE in checks
    assert EvalDomainCheck.DUPLICATE_CARDS in checks
    assert EvalDomainCheck.SOURCE_GROUNDING in checks


def test_semantic_domain_checks_are_not_keyword_heuristics() -> None:
    fixture = next(
        item
        for item in load_eval_fixtures()
        if EvalDomainCheck.JUNIOR_MATH_STRATEGY in item.domain_checks
    )
    cards = _cards(fixture, fixture.expected_card_count.minimum)

    result = check_generated_cards(fixture, cards)

    assert EvalDomainCheck.JUNIOR_MATH_STRATEGY in SEMANTIC_DOMAIN_CHECKS
    assert result.passed is True
    assert all(issue.check not in SEMANTIC_DOMAIN_CHECKS for issue in result.issues)
