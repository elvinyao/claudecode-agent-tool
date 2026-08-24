"""Deterministic checks that an AI judge is never allowed to override."""

from __future__ import annotations

from collections.abc import Iterable

from ankify.eval.models import (
    EvalDomainCheck,
    EvalFixture,
    EvalIssueSeverity,
    EvalRuleIssue,
    EvalRuleResult,
)
from ankify.models import AnkifyCard, NoteType, ProvenanceType

SEMANTIC_DOMAIN_CHECKS = frozenset(
    {
        EvalDomainCheck.JUNIOR_MATH_STRATEGY,
        EvalDomainCheck.SOCIAL_CAUSALITY,
        EvalDomainCheck.TRANSLATION_DIRECTION,
    }
)


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _issue(
    issues: list[EvalRuleIssue],
    check: EvalDomainCheck,
    message: str,
    *,
    card_index: int | None = None,
    severity: EvalIssueSeverity = EvalIssueSeverity.FAILURE,
) -> None:
    issues.append(
        EvalRuleIssue(
            check=check,
            severity=severity,
            message=message,
            card_index=card_index,
        )
    )


def _enabled(fixture: EvalFixture, check: EvalDomainCheck) -> bool:
    return check in fixture.domain_checks


def check_generated_cards(
    fixture: EvalFixture,
    cards: Iterable[AnkifyCard],
) -> EvalRuleResult:
    """Check only mechanically provable properties; semantic checks go to Judge."""

    card_list = tuple(cards)
    issues: list[EvalRuleIssue] = []

    if _enabled(fixture, EvalDomainCheck.BASIC_SCHEMA):
        for index, card in enumerate(card_list):
            if card.note_type != NoteType.BASIC.value or not card.front or not card.back:
                _issue(
                    issues,
                    EvalDomainCheck.BASIC_SCHEMA,
                    "Card must be a non-empty Basic note.",
                    card_index=index,
                )
            if "{{c" in card.front.casefold() or "{{c" in card.back.casefold():
                _issue(
                    issues,
                    EvalDomainCheck.BASIC_SCHEMA,
                    "Eval v1 forbids Cloze syntax.",
                    card_index=index,
                )

    if _enabled(fixture, EvalDomainCheck.COUNT_RANGE) and not (
        fixture.expected_card_count.minimum
        <= len(card_list)
        <= fixture.expected_card_count.maximum
    ):
        _issue(
            issues,
            EvalDomainCheck.COUNT_RANGE,
            "Expected "
            f"{fixture.expected_card_count.minimum}-{fixture.expected_card_count.maximum} "
            f"cards, received {len(card_list)}.",
        )

    if _enabled(fixture, EvalDomainCheck.LENGTH):
        for index, card in enumerate(card_list):
            if len(card.front) > fixture.max_front_length:
                _issue(
                    issues,
                    EvalDomainCheck.LENGTH,
                    f"Front has {len(card.front)} characters; limit is "
                    f"{fixture.max_front_length}.",
                    card_index=index,
                )
            if len(card.back) > fixture.max_back_length:
                _issue(
                    issues,
                    EvalDomainCheck.LENGTH,
                    f"Back has {len(card.back)} characters; limit is "
                    f"{fixture.max_back_length}.",
                    card_index=index,
                )

    if _enabled(fixture, EvalDomainCheck.FRONT_BACK_DISTINCT):
        for index, card in enumerate(card_list):
            if _normalize(card.front) == _normalize(card.back):
                _issue(
                    issues,
                    EvalDomainCheck.FRONT_BACK_DISTINCT,
                    "Front and back are identical after normalization.",
                    card_index=index,
                )

    if _enabled(fixture, EvalDomainCheck.DUPLICATE_CARDS):
        seen_fronts: dict[str, int] = {}
        seen_note_ids: dict[str, int] = {}
        for index, card in enumerate(card_list):
            front_key = _normalize(card.front)
            duplicate_index = seen_fronts.get(front_key)
            if duplicate_index is not None:
                _issue(
                    issues,
                    EvalDomainCheck.DUPLICATE_CARDS,
                    f"Front duplicates card {duplicate_index + 1}.",
                    card_index=index,
                )
            else:
                seen_fronts[front_key] = index
            duplicate_id_index = seen_note_ids.get(card.note_id)
            if duplicate_id_index is not None:
                _issue(
                    issues,
                    EvalDomainCheck.DUPLICATE_CARDS,
                    f"note_id duplicates card {duplicate_id_index + 1}.",
                    card_index=index,
                )
            else:
                seen_note_ids[card.note_id] = index

    if _enabled(fixture, EvalDomainCheck.SOURCE_REF):
        for index, card in enumerate(card_list):
            if not card.source_block_ids or not card.evidence_quotes:
                _issue(
                    issues,
                    EvalDomainCheck.SOURCE_REF,
                    "Source-grounded eval cards require block IDs and evidence quotes.",
                    card_index=index,
                )

    if _enabled(fixture, EvalDomainCheck.EXPECTED_TAGS):
        all_tags = {_normalize(tag) for card in card_list for tag in card.tags}
        for tag in fixture.expected_tags:
            if _normalize(tag) not in all_tags:
                _issue(
                    issues,
                    EvalDomainCheck.EXPECTED_TAGS,
                    f"Expected tag {tag!r} was not found.",
                )

    if _enabled(fixture, EvalDomainCheck.SOURCE_GROUNDING):
        for index, card in enumerate(card_list):
            if card.provenance is not ProvenanceType.SOURCE:
                _issue(
                    issues,
                    EvalDomainCheck.SOURCE_GROUNDING,
                    "Fixture requires source provenance.",
                    card_index=index,
                )
                continue
            for quote in card.evidence_quotes:
                if not quote.strip() or quote.strip() not in fixture.source.text:
                    _issue(
                        issues,
                        EvalDomainCheck.SOURCE_GROUNDING,
                        "Evidence quote does not occur exactly in fixture source text.",
                        card_index=index,
                    )
                    break

    return EvalRuleResult(
        passed=not any(
            issue.severity is EvalIssueSeverity.FAILURE for issue in issues
        ),
        issues=tuple(issues),
    )


__all__ = ["SEMANTIC_DOMAIN_CHECKS", "check_generated_cards"]
