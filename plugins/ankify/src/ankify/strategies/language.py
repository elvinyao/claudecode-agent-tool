"""Versioned language-learning card-authoring policy."""

from __future__ import annotations

from ankify.models import AnkifyRunOptions, LanguageTaskType, TranslationDirection
from ankify.strategies.base import stable_tags

STRATEGY_VERSION = "language-v1"

_TASK_RULES: dict[LanguageTaskType, tuple[str, tuple[str, ...]]] = {
    LanguageTaskType.VOCABULARY: (
        "vocabulary",
        (
            "Separate meaning, usage, and example points instead of merging several words "
            "into one card.",
            "Keep the recall prompt short and preserve the supplied spelling and usage evidence.",
        ),
    ),
    LanguageTaskType.GRAMMAR: (
        "grammar",
        (
            "Separate grammatical connection, meaning, example, and common mistake into "
            "atomic cards.",
            "Do not use Cloze syntax in the Basic-only first version.",
        ),
    ),
    LanguageTaskType.TRANSLATION_PRACTICE: (
        "translation-practice",
        (
            "Each card asks for one short phrase or sentence in exactly one translation direction.",
            "Do not silently improve or add facts beyond the supplied source phrase.",
        ),
    ),
    LanguageTaskType.LISTENING_READING: (
        "listening-reading",
        (
            "Create atomic main-idea, detail, and paraphrase cards rather than one broad summary.",
            "Answers must remain grounded in the supplied passage.",
        ),
    ),
}


def language_policy(options: AnkifyRunOptions) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if options.language_task is None:
        raise ValueError("language strategy requires language_task")
    task_tag, task_rules = _TASK_RULES[options.language_task]
    exam_tags = (
        ()
        if not options.target_exam or options.target_exam.casefold() == "none"
        else (options.target_exam.casefold(),)
    )
    direction_tags: tuple[str, ...] = ()
    direction_rule = ""
    if options.language_task is LanguageTaskType.TRANSLATION_PRACTICE:
        if options.translation_direction is None:
            raise ValueError("translation practice requires direction")
        direction_tags = (options.translation_direction.value.replace("_", "-"),)
        direction_rule = {
            TranslationDirection.LEARNING_TO_NATIVE: (
                "Ask from the learning language and answer in the native language."
            ),
            TranslationDirection.NATIVE_TO_LEARNING: (
                "Ask from the native language and answer in the learning language."
            ),
            TranslationDirection.BIDIRECTIONAL: (
                "The complete batch must contain both learning-to-native and "
                "native-to-learning cards."
            ),
        }[options.translation_direction]
    tags = stable_tags(
        ("language", task_tag),
        exam_tags,
        direction_tags,
        options.user_tags,
    )
    rules = (
        f"Learning language: {options.learning_language}; "
        f"native language: {options.native_language}.",
        *task_rules,
        *((direction_rule,) if direction_rule else ()),
        "Generate Basic cards only and keep every card focused on one recall target.",
    )
    return tags, rules


__all__ = ["STRATEGY_VERSION", "language_policy"]
