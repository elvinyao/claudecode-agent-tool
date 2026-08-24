"""Fail-closed resolution of versioned Ankify strategy profiles."""

from __future__ import annotations

from ankify.models import (
    AnkifyRunOptions,
    GroundingPolicy,
    GuideProfile,
    NoteType,
    ResolvedStrategy,
    SourceMode,
    StudyPurpose,
)
from ankify.strategies.generic import EXAM_PREP_VERSION as EXAM_PREP_VERSION
from ankify.strategies.generic import FREE_VERSION as FREE_VERSION
from ankify.strategies.generic import exam_prep_policy, free_policy
from ankify.strategies.junior_exam import STRATEGY_VERSION as JUNIOR_EXAM_VERSION
from ankify.strategies.junior_exam import junior_exam_policy
from ankify.strategies.language import STRATEGY_VERSION as LANGUAGE_VERSION
from ankify.strategies.language import language_policy


class StrategyConfigurationError(ValueError):
    """Run options do not select one supported, internally consistent strategy."""


_PROFILE_FOR_PURPOSE = {
    StudyPurpose.JUNIOR_EXAM: GuideProfile.JUNIOR_EXAM_STANDARD4,
    StudyPurpose.LANGUAGE: GuideProfile.LANGUAGE_GENERAL,
    StudyPurpose.EXAM_PREP: GuideProfile.EXAM_PREP_GENERIC,
    StudyPurpose.FREE: GuideProfile.FREE_GENERIC,
}


def resolve_strategy(options: AnkifyRunOptions) -> ResolvedStrategy:
    profile = options.guide_profile or _PROFILE_FOR_PURPOSE[options.study_purpose]
    try:
        if profile is GuideProfile.JUNIOR_EXAM_STANDARD4:
            version = JUNIOR_EXAM_VERSION
            tags, rules = junior_exam_policy(options)
            language = "ja"
        elif profile is GuideProfile.LANGUAGE_GENERAL:
            version = LANGUAGE_VERSION
            tags, rules = language_policy(options)
            language = (
                options.language
                if options.language != "auto"
                else f"{options.learning_language}+{options.native_language}"
            )
        elif profile is GuideProfile.EXAM_PREP_GENERIC:
            version = EXAM_PREP_VERSION
            tags, rules = exam_prep_policy(options)
            language = options.language
        else:
            version = FREE_VERSION
            tags, rules = free_policy(options)
            language = options.language
    except (KeyError, TypeError, ValueError) as exc:
        raise StrategyConfigurationError(str(exc)) from exc

    if options.strategy_version is not None and options.strategy_version != version:
        raise StrategyConfigurationError(
            f"strategy version {options.strategy_version!r} is not supported for "
            f"{profile.value!r}; "
            f"expected {version!r}"
        )
    grounding = (
        GroundingPolicy.MODEL_KNOWLEDGE_ALLOWED
        if options.source_mode is SourceMode.TOPIC_SCOPE
        else GroundingPolicy.STRICT
    )
    return ResolvedStrategy(
        profile=profile,
        version=version,
        language=language,
        allowed_note_types=(NoteType.BASIC,),
        default_tags=tags,
        prompt_rules=rules,
        grounding_policy=grounding,
    )


__all__ = ["StrategyConfigurationError", "resolve_strategy"]
