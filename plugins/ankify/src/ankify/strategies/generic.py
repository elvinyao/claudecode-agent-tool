"""Versioned generic exam-prep and free-form policies."""

from __future__ import annotations

from ankify.models import AnkifyRunOptions
from ankify.strategies.base import stable_tags

EXAM_PREP_VERSION = "exam-prep-v1"
FREE_VERSION = "free-v1"


def exam_prep_policy(options: AnkifyRunOptions) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (
        stable_tags(("exam-prep",), options.user_tags),
        (
            "Prioritize exam-relevant definitions, distinctions, conditions, and common "
            "mistakes present in the source.",
            "Create Basic cards with one independently testable learning point per card.",
        ),
    )


def free_policy(options: AnkifyRunOptions) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (
        stable_tags(("ankify",), options.user_tags),
        (
            "Select useful recall targets from the supplied material without inventing "
            "unsupported facts.",
            "Create Basic cards with one independently testable learning point per card.",
        ),
    )


__all__ = ["EXAM_PREP_VERSION", "FREE_VERSION", "exam_prep_policy", "free_policy"]
