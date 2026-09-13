"""Strict fixture, checker, judge, and report contracts for Ankify evaluation."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_core.contracts import RunStatus, StrictFrozenModel
from ankify.models import AnkifyCard, AnkifyRunOptions, GuideProfile


class EvalMode(str, Enum):
    LOCAL = "local"
    STRICT = "strict"


class EvalFixtureId(str, Enum):
    LANGUAGE_JLPT_VOCABULARY = "language-jlpt-vocabulary"
    LANGUAGE_GRAMMAR = "language-grammar"
    LANGUAGE_TRANSLATION_BIDIRECTIONAL = "language-translation-bidirectional"
    LANGUAGE_TOEIC_READING = "language-toeic-reading"
    JUNIOR_SANSUU_MISTAKE = "junior-sansuu-mistake"
    JUNIOR_KOKUGO_VOCABULARY = "junior-kokugo-vocabulary"
    JUNIOR_SHAKAI_CAUSALITY = "junior-shakai-causality"


class EvalDomainCheck(str, Enum):
    BASIC_SCHEMA = "basic_schema"
    COUNT_RANGE = "count_range"
    LENGTH = "length"
    FRONT_BACK_DISTINCT = "front_back_distinct"
    DUPLICATE_CARDS = "duplicate_cards"
    SOURCE_REF = "source_ref"
    EXPECTED_TAGS = "expected_tags"
    SOURCE_GROUNDING = "source_grounding"
    JUNIOR_MATH_STRATEGY = "junior_math_strategy"
    SOCIAL_CAUSALITY = "social_causality"
    TRANSLATION_DIRECTION = "translation_direction"


class EvalIssueSeverity(str, Enum):
    WARNING = "warning"
    FAILURE = "failure"


class EvalCaseStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIPPED = "skipped"


class EvalSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=50, max_length=100_000)
    filename: str = Field(default="source.txt", min_length=1, max_length=255)

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("fixture filename must be a plain filename")
        return value


class EvalCardCount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum: int = Field(ge=1, le=100)
    maximum: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def validate_range(self) -> EvalCardCount:
        if self.maximum < self.minimum:
            raise ValueError("maximum card count must be at least minimum")
        return self


class EvalFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: EvalFixtureId
    title: str = Field(min_length=1, max_length=200)
    options: AnkifyRunOptions
    source: EvalSource
    expected_card_count: EvalCardCount
    expected_tags: tuple[str, ...] = Field(min_length=1)
    max_front_length: int = Field(ge=20, le=1_000)
    max_back_length: int = Field(ge=20, le=4_000)
    domain_checks: tuple[EvalDomainCheck, ...] = Field(min_length=1)
    rubric_notes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_fixed_strategy(self) -> EvalFixture:
        if self.options.strategy_version is None:
            raise ValueError("eval fixture must pin strategy_version")
        expected_profile = {
            "junior_exam": GuideProfile.JUNIOR_EXAM_STANDARD4,
            "language": GuideProfile.LANGUAGE_GENERAL,
            "exam_prep": GuideProfile.EXAM_PREP_GENERIC,
            "free": GuideProfile.FREE_GENERIC,
        }[self.options.study_purpose.value]
        if self.options.guide_profile is not expected_profile:
            raise ValueError("eval fixture must explicitly pin its matching guide_profile")
        if len(set(self.domain_checks)) != len(self.domain_checks):
            raise ValueError("eval fixture domain_checks must be unique")
        return self


class EvalRuleIssue(StrictFrozenModel):
    check: EvalDomainCheck
    severity: EvalIssueSeverity
    message: str = Field(min_length=1, max_length=500)
    card_index: int | None = Field(default=None, ge=0)


class EvalRuleResult(StrictFrozenModel):
    passed: bool
    issues: tuple[EvalRuleIssue, ...] = ()

    @model_validator(mode="after")
    def validate_passed(self) -> EvalRuleResult:
        expected = not any(issue.severity is EvalIssueSeverity.FAILURE for issue in self.issues)
        if self.passed is not expected:
            raise ValueError("passed must reflect the absence of hard rule failures")
        return self


class EvalJudgeScores(StrictFrozenModel):
    atomicity: int = Field(ge=0, le=100)
    grounding: int = Field(ge=0, le=100)
    learner_fit: int = Field(ge=0, le=100)
    usefulness: int = Field(ge=0, le=100)
    domain_fit: int = Field(ge=0, le=100)


class EvalJudgeResponse(StrictFrozenModel):
    """All-required wire schema returned by a Provider judge."""

    scores: EvalJudgeScores
    reasons: list[str] = Field(max_length=20)
    suggestions: list[str] = Field(max_length=20)


class EvalJudgeResult(StrictFrozenModel):
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    scores: EvalJudgeScores
    overall_score: float = Field(ge=0, le=100)
    reasons: tuple[str, ...]
    suggestions: tuple[str, ...]


class EvalCaseResult(StrictFrozenModel):
    fixture_id: EvalFixtureId
    title: str
    strategy_profile: GuideProfile
    strategy_version: str
    runtime_status: RunStatus | None
    cards: tuple[AnkifyCard, ...]
    rule_result: EvalRuleResult
    judge_result: EvalJudgeResult | None
    status: EvalCaseStatus
    total_score: float | None
    error_message: str | None


class EvalReport(StrictFrozenModel):
    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1, max_length=128)
    mode: EvalMode
    created_at: str
    generation_provider: str | None
    generation_model: str | None
    judge_provider: str | None
    judge_model: str | None
    self_judged: bool
    fixture_score_threshold: float = Field(ge=0, le=100)
    overall_score_threshold: float = Field(ge=0, le=100)
    overall_average: float | None = Field(default=None, ge=0, le=100)
    passed: bool
    results: tuple[EvalCaseResult, ...]


__all__ = [
    "EvalCardCount",
    "EvalCaseResult",
    "EvalCaseStatus",
    "EvalDomainCheck",
    "EvalFixture",
    "EvalFixtureId",
    "EvalIssueSeverity",
    "EvalJudgeResponse",
    "EvalJudgeResult",
    "EvalJudgeScores",
    "EvalMode",
    "EvalReport",
    "EvalRuleIssue",
    "EvalRuleResult",
    "EvalSource",
]
