from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_core.providers.codex_schema import validate_codex_output_schema
from ankify.models import (
    AnkifyRunOptions,
    GuideProfile,
    JuniorExamStage,
    JuniorExamSubject,
    LanguageTaskType,
    NoteType,
    ProviderCardBatch,
    SourceMode,
    StudyPurpose,
    TranslationDirection,
)
from ankify.plugin import AnkifyPlugin, bundled_skill_path, load_bundled_skill
from ankify.strategies import StrategyConfigurationError, resolve_strategy


def test_junior_exam_strategy_is_basic_japanese_and_versioned() -> None:
    options = AnkifyRunOptions(
        study_purpose=StudyPurpose.JUNIOR_EXAM,
        exam_subject=JuniorExamSubject.SANSUU,
        exam_stage=JuniorExamStage.GRADE5,
        source_mode=SourceMode.MISTAKES_EXPLANATIONS,
        deck_name="小5算数",
        user_tags=["custom"],
    )

    strategy = resolve_strategy(options)

    assert strategy.profile is GuideProfile.JUNIOR_EXAM_STANDARD4
    assert strategy.version == "junior-exam-v1"
    assert strategy.language == "ja"
    assert strategy.allowed_note_types == (NoteType.BASIC,)
    assert strategy.default_tags == (
        "chugaku-juken",
        "sansuu",
        "grade5",
        "mistake-review",
        "custom",
    )
    assert any("なぜその式" in rule for rule in strategy.prompt_rules)


def test_language_strategy_records_task_exam_and_bidirectional_tags() -> None:
    options = AnkifyRunOptions(
        study_purpose=StudyPurpose.LANGUAGE,
        learning_language="en",
        native_language="zh",
        target_exam="toeic",
        language_task=LanguageTaskType.TRANSLATION_PRACTICE,
        translation_direction=TranslationDirection.BIDIRECTIONAL,
    )

    strategy = resolve_strategy(options)

    assert strategy.profile is GuideProfile.LANGUAGE_GENERAL
    assert strategy.version == "language-v1"
    assert strategy.language == "en+zh"
    assert strategy.default_tags == (
        "language",
        "translation-practice",
        "toeic",
        "bidirectional",
    )


def test_options_reject_profile_mismatch_and_incomplete_guided_selections() -> None:
    with pytest.raises(ValidationError, match="guide_profile does not match"):
        AnkifyRunOptions(
            study_purpose=StudyPurpose.JUNIOR_EXAM,
            guide_profile=GuideProfile.FREE_GENERIC,
            exam_subject=JuniorExamSubject.RIKA,
            exam_stage=JuniorExamStage.GRADE6,
        )

    with pytest.raises(ValidationError, match="requires exam_subject"):
        AnkifyRunOptions(study_purpose=StudyPurpose.JUNIOR_EXAM)

    with pytest.raises(ValidationError, match="requires learning_language"):
        AnkifyRunOptions(study_purpose=StudyPurpose.LANGUAGE)


def test_strategy_version_pin_fails_closed() -> None:
    options = AnkifyRunOptions(strategy_version="free-v2")

    with pytest.raises(StrategyConfigurationError, match="expected 'free-v1'"):
        resolve_strategy(options)


def test_options_parse_allowlisted_json_enum_values_under_strict_runtime_validation() -> None:
    options = AnkifyRunOptions.model_validate(
        {
            "study_purpose": "junior_exam",
            "guide_profile": "junior_exam.standard4",
            "source_mode": "mistakes_explanations",
            "exam_subject": "sansuu",
            "exam_stage": "grade5",
        },
        strict=True,
    )

    assert options.study_purpose is StudyPurpose.JUNIOR_EXAM
    assert options.guide_profile is GuideProfile.JUNIOR_EXAM_STANDARD4
    assert options.source_mode is SourceMode.MISTAKES_EXPLANATIONS


def test_provider_wire_schema_satisfies_codex_strict_objects() -> None:
    validate_codex_output_schema(ProviderCardBatch.model_json_schema())


def test_plugin_manifest_and_bundled_skill_are_stable() -> None:
    plugin = AnkifyPlugin()

    assert plugin.manifest.plugin_id == "ankify"
    assert plugin.manifest.version == "0.1.0"
    assert plugin.manifest.options_model is AnkifyRunOptions
    assert bundled_skill_path().is_file()
    assert load_bundled_skill().name == "ankify-authoring"
