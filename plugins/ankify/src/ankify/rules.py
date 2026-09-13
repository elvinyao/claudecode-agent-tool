"""Deterministic acceptance rules around untrusted Ankify card candidates."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from ankify.models import (
    AgentConfidence,
    AgentReviewCode,
    AnkifyCard,
    GroundingPolicy,
    ParsedAnkifyRun,
    ProvenanceType,
    ProviderCardCandidate,
    QualityCode,
    QualityIssue,
    QualityOrigin,
    QualitySeverity,
    RejectedCandidate,
)

_CLOZE = re.compile(r"\{\{c\d+::", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    card: AnkifyCard | None
    rejection: RejectedCandidate | None


def normalize_card_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _safe_tag(value: str) -> bool:
    return bool(
        value
        and value == value.strip()
        and len(value) <= 64
        and not any(
            character.isspace() or character in {",", "\r", "\n", "\x00"} for character in value
        )
    )


def _issue(
    code: QualityCode,
    *,
    origin: QualityOrigin,
    severity: QualitySeverity,
    message: str,
) -> QualityIssue:
    return QualityIssue(code=code, origin=origin, severity=severity, message=message)


def _stable_note_id(parsed: ParsedAnkifyRun, candidate: ProviderCardCandidate) -> str:
    payload = {
        "strategy": parsed.strategy.version,
        "document_id": parsed.source.document_id,
        "note_type": candidate.note_type,
        "provenance": candidate.provenance.value,
        "source_block_ids": sorted(candidate.source_block_ids),
        "evidence_quotes": sorted(normalize_card_text(item) for item in candidate.evidence_quotes),
        "learning_objective": normalize_card_text(candidate.learning_objective),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def validate_candidate(
    parsed: ParsedAnkifyRun,
    candidate: ProviderCardCandidate,
    *,
    batch_id: str,
    candidate_index: int,
    allowed_block_ids: frozenset[str],
) -> CandidateDecision:
    """Accept one candidate only after hard structure and provenance checks."""

    issues: list[QualityIssue] = []
    front = candidate.front.strip()
    back = candidate.back.strip()
    if not front or not back or normalize_card_text(front) == normalize_card_text(back):
        issues.append(
            _issue(
                QualityCode.INVALID_STRUCTURE,
                origin=QualityOrigin.RULE,
                severity=QualitySeverity.ERROR,
                message="Basic card front and back must be non-empty and distinct.",
            )
        )
    if _CLOZE.search(front) or _CLOZE.search(back):
        issues.append(
            _issue(
                QualityCode.INVALID_STRUCTURE,
                origin=QualityOrigin.RULE,
                severity=QualitySeverity.ERROR,
                message="Ankify v1 accepts Basic cards only; Cloze syntax is forbidden.",
            )
        )
    if "\x00" in front or "\x00" in back:
        issues.append(
            _issue(
                QualityCode.INVALID_STRUCTURE,
                origin=QualityOrigin.RULE,
                severity=QualitySeverity.ERROR,
                message="Card text must not contain NUL characters.",
            )
        )
    if len(front) > parsed.options.max_front_length or len(back) > parsed.options.max_back_length:
        issues.append(
            _issue(
                QualityCode.TOO_LONG,
                origin=QualityOrigin.RULE,
                severity=QualitySeverity.WARNING,
                message=(
                    f"Card exceeds configured length limits "
                    f"({len(front)}/{parsed.options.max_front_length} front, "
                    f"{len(back)}/{parsed.options.max_back_length} back)."
                ),
            )
        )

    unknown_block_ids = set(candidate.source_block_ids) - allowed_block_ids
    selected_blocks = {
        block.block_id: block
        for block in parsed.source.blocks
        if block.block_id in candidate.source_block_ids
    }
    if unknown_block_ids:
        issues.append(
            _issue(
                QualityCode.INVALID_STRUCTURE,
                origin=QualityOrigin.RULE,
                severity=QualitySeverity.ERROR,
                message="Candidate references a source block outside its assigned batch.",
            )
        )

    strict_source = parsed.strategy.grounding_policy is GroundingPolicy.STRICT
    if strict_source or candidate.provenance is ProvenanceType.SOURCE:
        if candidate.provenance is not ProvenanceType.SOURCE:
            issues.append(
                _issue(
                    QualityCode.SOURCE_CHECK,
                    origin=QualityOrigin.RULE,
                    severity=QualitySeverity.ERROR,
                    message="This strategy requires source-grounded cards.",
                )
            )
        if not selected_blocks or not candidate.evidence_quotes:
            issues.append(
                _issue(
                    QualityCode.SOURCE_CHECK,
                    origin=QualityOrigin.RULE,
                    severity=QualitySeverity.ERROR,
                    message="Source-grounded cards require block IDs and evidence quotes.",
                )
            )
        else:
            for quote in candidate.evidence_quotes:
                if not quote.strip() or not any(
                    quote.strip() in block.text for block in selected_blocks.values()
                ):
                    issues.append(
                        _issue(
                            QualityCode.SOURCE_CHECK,
                            origin=QualityOrigin.RULE,
                            severity=QualitySeverity.ERROR,
                            message="Evidence quote does not exactly occur in a selected block.",
                        )
                    )
                    break
    elif candidate.provenance is ProvenanceType.MODEL_KNOWLEDGE:
        if candidate.source_block_ids or candidate.evidence_quotes:
            issues.append(
                _issue(
                    QualityCode.INVALID_STRUCTURE,
                    origin=QualityOrigin.RULE,
                    severity=QualitySeverity.ERROR,
                    message="Model-knowledge cards must not present source blocks as evidence.",
                )
            )
        issues.append(
            _issue(
                QualityCode.SOURCE_CHECK,
                origin=QualityOrigin.RULE,
                severity=QualitySeverity.WARNING,
                message="Model-knowledge card requires human source verification.",
            )
        )

    if any(not _safe_tag(tag) for tag in candidate.suggested_tags):
        issues.append(
            _issue(
                QualityCode.INVALID_STRUCTURE,
                origin=QualityOrigin.RULE,
                severity=QualitySeverity.ERROR,
                message="Candidate contains an unsafe suggested tag.",
            )
        )

    review_codes = list(dict.fromkeys(candidate.review_flags))
    if (
        candidate.confidence is AgentConfidence.LOW
        and AgentReviewCode.LOW_CONFIDENCE not in review_codes
    ):
        review_codes.append(AgentReviewCode.LOW_CONFIDENCE)
    for code in review_codes:
        issues.append(
            _issue(
                QualityCode(code.value),
                origin=QualityOrigin.AGENT_REVIEW,
                severity=QualitySeverity.WARNING,
                message=f"Agent review requested: {code.value}.",
            )
        )

    error_issues = [issue for issue in issues if issue.severity is QualitySeverity.ERROR]
    if error_issues:
        return CandidateDecision(
            card=None,
            rejection=RejectedCandidate(
                batch_id=batch_id,
                candidate_index=candidate_index,
                reason_codes=tuple(dict.fromkeys(issue.code for issue in error_issues)),
                message=" ".join(issue.message for issue in error_issues),
            ),
        )

    tags = tuple(dict.fromkeys((*parsed.strategy.default_tags, *candidate.suggested_tags)))
    return CandidateDecision(
        card=AnkifyCard(
            note_id=_stable_note_id(parsed, candidate),
            front=front,
            back=back,
            tags=tags,
            provenance=candidate.provenance,
            source_block_ids=tuple(candidate.source_block_ids),
            evidence_quotes=tuple(quote.strip() for quote in candidate.evidence_quotes),
            learning_objective=candidate.learning_objective.strip(),
            confidence=candidate.confidence,
            quality_issues=tuple(issues),
        ),
        rejection=None,
    )


__all__ = ["CandidateDecision", "normalize_card_text", "validate_candidate"]
