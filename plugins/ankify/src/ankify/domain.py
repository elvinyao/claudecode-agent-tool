"""Pure batch planning and candidate merge operations for the Ankify workflow."""

from __future__ import annotations

import math

from ankify.models import (
    AnkifyAgentBatchOutcome,
    AnkifyBatch,
    AnkifyCard,
    AnkifyRenderContext,
    ParsedAnkifyRun,
    QualityCode,
    RejectedCandidate,
)
from ankify.rules import normalize_card_text, validate_candidate


def plan_batches(parsed: ParsedAnkifyRun) -> tuple[AnkifyBatch, ...]:
    """Partition all blocks into stable batches and distribute the requested count."""

    blocks = parsed.source.blocks
    raw_batch_count = math.ceil(len(blocks) / parsed.options.batch_size)
    batch_count = min(raw_batch_count, parsed.options.requested_card_count)
    base_blocks, extra_blocks = divmod(len(blocks), batch_count)
    base_cards, extra_cards = divmod(parsed.options.requested_card_count, batch_count)
    batches: list[AnkifyBatch] = []
    offset = 0
    for index in range(batch_count):
        block_count = base_blocks + (1 if index < extra_blocks else 0)
        target_count = base_cards + (1 if index < extra_cards else 0)
        batch_blocks = blocks[offset : offset + block_count]
        offset += block_count
        batches.append(
            AnkifyBatch(
                batch_id=f"batch-{index + 1:04d}",
                target_card_count=target_count,
                blocks=batch_blocks,
            )
        )
    return tuple(batches)


def merge_batch_outcomes(
    parsed: ParsedAnkifyRun,
    outcomes: tuple[AnkifyAgentBatchOutcome, ...],
) -> AnkifyRenderContext:
    """Validate every candidate, reject unsafe work, and preserve partial success."""

    planned = {batch.batch_id: batch for batch in plan_batches(parsed)}
    accepted: list[tuple[AnkifyCard, str, int]] = []
    rejections: list[RejectedCandidate] = []
    warnings: list[str] = list(parsed.warnings)
    partial = bool(parsed.warnings)

    for outcome in outcomes:
        warnings.extend(outcome.warnings)
        batch = planned.get(outcome.batch_id)
        if batch is None:
            raise TypeError(f"unexpected Ankify batch outcome: {outcome.batch_id}")
        if outcome.result is None:
            partial = True
            warnings.append(
                f"{outcome.batch_id} generation failed ({outcome.error_code}); "
                "no local answers were invented."
            )
            continue
        warnings.extend(outcome.result.warnings)
        partial = partial or outcome.partial or outcome.result.partial
        allowed_ids = frozenset(block.block_id for block in batch.blocks)
        for candidate_index, candidate in enumerate(outcome.result.output.cards):
            decision = validate_candidate(
                parsed,
                candidate,
                batch_id=outcome.batch_id,
                candidate_index=candidate_index,
                allowed_block_ids=allowed_ids,
            )
            if decision.rejection is not None:
                partial = True
                rejections.append(decision.rejection)
                continue
            if decision.card is None:  # defensive; CandidateDecision requires one branch
                raise TypeError("candidate decision produced neither card nor rejection")
            if decision.card.quality_issues:
                partial = True
            accepted.append((decision.card, outcome.batch_id, candidate_index))

    unique_cards = []
    seen_note_ids: set[str] = set()
    seen_fronts: set[str] = set()
    for raw_card, batch_id, candidate_index in accepted:
        front_key = normalize_card_text(raw_card.front)
        if raw_card.note_id in seen_note_ids or front_key in seen_fronts:
            partial = True
            rejections.append(
                RejectedCandidate(
                    batch_id=batch_id,
                    candidate_index=candidate_index,
                    reason_codes=(QualityCode.DUPLICATE,),
                    message="Candidate duplicates an earlier validated card.",
                )
            )
            continue
        seen_note_ids.add(raw_card.note_id)
        seen_fronts.add(front_key)
        unique_cards.append(raw_card)

    maximum = parsed.options.requested_card_count
    if len(unique_cards) > maximum:
        partial = True
        for offset, _card in enumerate(unique_cards[maximum:]):
            rejections.append(
                RejectedCandidate(
                    batch_id="batch-0000",
                    candidate_index=maximum + offset,
                    reason_codes=(QualityCode.LIMIT_EXCEEDED,),
                    message="Candidate exceeds the requested card count.",
                )
            )
        unique_cards = unique_cards[:maximum]

    if len(unique_cards) < maximum:
        partial = True
        warnings.append(
            f"Generated {len(unique_cards)} validated cards; requested target was {maximum}."
        )
    if not unique_cards:
        partial = True
        warnings.append("No card candidate passed deterministic validation.")

    return AnkifyRenderContext(
        parsed=parsed,
        cards=tuple(unique_cards),
        rejections=tuple(rejections),
        warnings=tuple(dict.fromkeys(warnings)),
        partial=partial,
    )


__all__ = ["merge_batch_outcomes", "plan_batches"]
