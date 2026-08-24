"""Strict public, provider-wire, and internal contracts for Ankify."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_core.contracts import ArtifactInput, ArtifactOutput, ProviderResult, StrictFrozenModel


class StudyPurpose(str, Enum):
    JUNIOR_EXAM = "junior_exam"
    LANGUAGE = "language"
    EXAM_PREP = "exam_prep"
    FREE = "free"


class GuideProfile(str, Enum):
    JUNIOR_EXAM_STANDARD4 = "junior_exam.standard4"
    LANGUAGE_GENERAL = "language.general"
    EXAM_PREP_GENERIC = "exam_prep.generic"
    FREE_GENERIC = "free.generic"


class SourceMode(str, Enum):
    MATERIALS_NOTES = "materials_notes"
    MISTAKES_EXPLANATIONS = "mistakes_explanations"
    TOPIC_SCOPE = "topic_scope"


class JuniorExamSubject(str, Enum):
    KOKUGO = "kokugo"
    SANSUU = "sansuu"
    RIKA = "rika"
    SHAKAI = "shakai"


class JuniorExamStage(str, Enum):
    GRADE4 = "grade4"
    GRADE5 = "grade5"
    GRADE6 = "grade6"
    FINAL_PUSH = "final_push"


class LanguageTaskType(str, Enum):
    VOCABULARY = "vocabulary"
    GRAMMAR = "grammar"
    TRANSLATION_PRACTICE = "translation_practice"
    LISTENING_READING = "listening_reading"


class TranslationDirection(str, Enum):
    LEARNING_TO_NATIVE = "learning_to_native"
    NATIVE_TO_LEARNING = "native_to_learning"
    BIDIRECTIONAL = "bidirectional"


class NoteType(str, Enum):
    BASIC = "basic"


class ProvenanceType(str, Enum):
    SOURCE = "source"
    MODEL_KNOWLEDGE = "model_knowledge"


class GroundingPolicy(str, Enum):
    STRICT = "strict"
    MODEL_KNOWLEDGE_ALLOWED = "model_knowledge_allowed"


class QualityOrigin(str, Enum):
    RULE = "rule"
    AGENT_REVIEW = "agent_review"


class QualitySeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


class QualityCode(str, Enum):
    TOO_LONG = "too_long"
    SOURCE_CHECK = "source_check"
    DUPLICATE = "duplicate"
    INVALID_STRUCTURE = "invalid_structure"
    LIMIT_EXCEEDED = "limit_exceeded"
    MULTI_POINT = "multi_point"
    STRATEGY_MISMATCH = "strategy_mismatch"
    SOLUTION_GAP = "solution_gap"
    LOW_CONFIDENCE = "low_confidence"


class AgentReviewCode(str, Enum):
    MULTI_POINT = "multi_point"
    STRATEGY_MISMATCH = "strategy_mismatch"
    SOLUTION_GAP = "solution_gap"
    LOW_CONFIDENCE = "low_confidence"


class AgentConfidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AnkifyRunOptions(BaseModel):
    """Strict per-run options resolved into one versioned strategy profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    study_purpose: StudyPurpose = StudyPurpose.FREE
    guide_profile: GuideProfile | None = None
    strategy_version: str | None = Field(default=None, min_length=1, max_length=64)
    source_mode: SourceMode = SourceMode.MATERIALS_NOTES
    deck_name: str = Field(default="Ankify", min_length=1, max_length=200)
    requested_card_count: int = Field(default=10, ge=1, le=100)
    batch_size: int = Field(default=8, ge=1, le=50)
    language: str = Field(default="auto", min_length=1, max_length=32)
    user_tags: list[str] = Field(default_factory=list, max_length=50)
    use_bundled_skill: bool = True

    exam_subject: JuniorExamSubject | None = None
    exam_stage: JuniorExamStage | None = None

    learning_language: str | None = Field(default=None, max_length=32)
    native_language: str | None = Field(default=None, max_length=32)
    target_exam: str | None = Field(default=None, max_length=64)
    language_task: LanguageTaskType | None = None
    translation_direction: TranslationDirection | None = None

    max_front_length: int = Field(default=160, ge=20, le=1_000)
    max_back_length: int = Field(default=360, ge=20, le=4_000)

    @field_validator("study_purpose", mode="before")
    @classmethod
    def parse_study_purpose(cls, value: object) -> object:
        return StudyPurpose(value) if isinstance(value, str) else value

    @field_validator("guide_profile", mode="before")
    @classmethod
    def parse_guide_profile(cls, value: object) -> object:
        return GuideProfile(value) if isinstance(value, str) else value

    @field_validator("source_mode", mode="before")
    @classmethod
    def parse_source_mode(cls, value: object) -> object:
        return SourceMode(value) if isinstance(value, str) else value

    @field_validator("exam_subject", mode="before")
    @classmethod
    def parse_exam_subject(cls, value: object) -> object:
        return JuniorExamSubject(value) if isinstance(value, str) else value

    @field_validator("exam_stage", mode="before")
    @classmethod
    def parse_exam_stage(cls, value: object) -> object:
        return JuniorExamStage(value) if isinstance(value, str) else value

    @field_validator("language_task", mode="before")
    @classmethod
    def parse_language_task(cls, value: object) -> object:
        return LanguageTaskType(value) if isinstance(value, str) else value

    @field_validator("translation_direction", mode="before")
    @classmethod
    def parse_translation_direction(cls, value: object) -> object:
        return TranslationDirection(value) if isinstance(value, str) else value

    @field_validator(
        "deck_name",
        "language",
        "learning_language",
        "native_language",
        "target_exam",
        "strategy_version",
    )
    @classmethod
    def validate_trimmed_text(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("Ankify option strings must be trimmed")
        return value

    @field_validator("user_tags")
    @classmethod
    def validate_user_tags(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("user_tags must be unique")
        for tag in value:
            if (
                not tag
                or tag != tag.strip()
                or len(tag) > 64
                or any(character.isspace() or character in {",", "\r", "\n"} for character in tag)
            ):
                raise ValueError("user_tags must be non-empty safe Anki tag values")
        return value

    @model_validator(mode="after")
    def validate_profile_selections(self) -> AnkifyRunOptions:
        profile = self.guide_profile
        expected = {
            StudyPurpose.JUNIOR_EXAM: GuideProfile.JUNIOR_EXAM_STANDARD4,
            StudyPurpose.LANGUAGE: GuideProfile.LANGUAGE_GENERAL,
            StudyPurpose.EXAM_PREP: GuideProfile.EXAM_PREP_GENERIC,
            StudyPurpose.FREE: GuideProfile.FREE_GENERIC,
        }[self.study_purpose]
        if profile is not None and profile is not expected:
            raise ValueError("guide_profile does not match study_purpose")

        if self.study_purpose is StudyPurpose.JUNIOR_EXAM:
            if self.exam_subject is None or self.exam_stage is None:
                raise ValueError("junior_exam requires exam_subject and exam_stage")
            if self.language not in {"auto", "ja"}:
                raise ValueError("junior_exam language is fixed to Japanese")
        elif self.exam_subject is not None or self.exam_stage is not None:
            raise ValueError("exam_subject and exam_stage are junior_exam-only options")

        if self.study_purpose is StudyPurpose.LANGUAGE:
            if not self.learning_language or not self.native_language:
                raise ValueError("language profile requires learning_language and native_language")
            if self.language_task is None:
                raise ValueError("language profile requires language_task")
            if (
                self.language_task is LanguageTaskType.TRANSLATION_PRACTICE
                and self.translation_direction is None
            ):
                raise ValueError("translation practice requires translation_direction")
        elif any(
            value is not None
            for value in (
                self.learning_language,
                self.native_language,
                self.target_exam,
                self.language_task,
                self.translation_direction,
            )
        ):
            raise ValueError("language selections are only valid for the language profile")
        return self


class AnkifyWorkflowInput(ArtifactInput[AnkifyRunOptions]):
    options: AnkifyRunOptions = Field(default_factory=AnkifyRunOptions)
    filename: str = "source.txt"


class SourceBlock(StrictFrozenModel):
    block_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    ordinal: int = Field(ge=0)
    heading_path: tuple[str, ...] = ()
    text: str = Field(min_length=1, max_length=8_000)


class NormalizedSource(StrictFrozenModel):
    document_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    name: str = Field(min_length=1, max_length=255)
    blocks: tuple[SourceBlock, ...] = Field(min_length=1)


class ResolvedStrategy(StrictFrozenModel):
    profile: GuideProfile
    version: str = Field(min_length=1, max_length=64)
    language: str = Field(min_length=1, max_length=32)
    allowed_note_types: tuple[NoteType, ...] = (NoteType.BASIC,)
    default_tags: tuple[str, ...]
    prompt_rules: tuple[str, ...]
    grounding_policy: GroundingPolicy


class ProviderCardCandidate(StrictFrozenModel):
    """All-required structured-output model returned by an Agent provider."""

    note_type: Literal["basic"]
    front: str = Field(min_length=1, max_length=4_000)
    back: str = Field(min_length=1, max_length=8_000)
    source_block_ids: list[str] = Field(max_length=32)
    evidence_quotes: list[str] = Field(max_length=32)
    suggested_tags: list[str] = Field(max_length=32)
    learning_objective: str = Field(min_length=1, max_length=500)
    review_flags: list[AgentReviewCode] = Field(max_length=8)
    confidence: AgentConfidence
    provenance: ProvenanceType


class ProviderCardBatch(StrictFrozenModel):
    cards: list[ProviderCardCandidate]


class QualityIssue(StrictFrozenModel):
    code: QualityCode
    origin: QualityOrigin
    severity: QualitySeverity
    message: str = Field(min_length=1, max_length=500)


class AnkifyCard(StrictFrozenModel):
    note_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    note_type: Literal["basic"] = "basic"
    front: str
    back: str
    tags: tuple[str, ...]
    provenance: ProvenanceType
    source_block_ids: tuple[str, ...]
    evidence_quotes: tuple[str, ...]
    learning_objective: str
    confidence: AgentConfidence
    quality_issues: tuple[QualityIssue, ...] = ()


class RejectedCandidate(StrictFrozenModel):
    batch_id: str
    candidate_index: int = Field(ge=0)
    reason_codes: tuple[QualityCode, ...]
    message: str = Field(min_length=1, max_length=500)


class AnkifyResultDocument(StrictFrozenModel):
    schema_version: Literal[1] = 1
    plugin: Literal["ankify"] = "ankify"
    plugin_version: str
    strategy_profile: GuideProfile
    strategy_version: str
    deck_name: str
    document_id: str
    source_name: str
    status: Literal["complete", "degraded"]
    partial: bool
    cards: tuple[AnkifyCard, ...]
    rejections: tuple[RejectedCandidate, ...]
    warnings: tuple[str, ...]


class AnkifyArtifact(ArtifactOutput):
    media_type: str = "application/json; charset=utf-8"
    filename: str = "ankify-cards.json"
    partial: bool = False
    warnings: tuple[str, ...] = ()


class AnkifyBatch(StrictFrozenModel):
    batch_id: str = Field(pattern=r"^batch-[0-9]{4}$")
    target_card_count: int = Field(ge=1, le=100)
    blocks: tuple[SourceBlock, ...] = Field(min_length=1)


class ParsedAnkifyRun(StrictFrozenModel):
    source: NormalizedSource
    options: AnkifyRunOptions
    strategy: ResolvedStrategy
    warnings: tuple[str, ...] = ()


class AnkifyAgentBatchOutcome(StrictFrozenModel):
    batch_id: str = Field(pattern=r"^batch-[0-9]{4}$")
    result: ProviderResult[ProviderCardBatch] | None = None
    error_code: str | None = Field(default=None, max_length=100)
    error_message: str | None = Field(default=None, max_length=500)
    partial: bool = False
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_one_outcome(self) -> AnkifyAgentBatchOutcome:
        if (self.result is None) == (self.error_code is None):
            raise ValueError("exactly one of result or error_code is required")
        if self.result is not None and self.result.request_id != self.batch_id:
            raise ValueError("Provider result request_id must match batch_id")
        if self.error_code is not None and not self.error_message:
            raise ValueError("failed batch requires a safe error_message")
        return self


class AnkifyRenderContext(StrictFrozenModel):
    parsed: ParsedAnkifyRun
    cards: tuple[AnkifyCard, ...]
    rejections: tuple[RejectedCandidate, ...]
    warnings: tuple[str, ...]
    partial: bool


__all__ = [
    "AgentConfidence",
    "AgentReviewCode",
    "AnkifyAgentBatchOutcome",
    "AnkifyArtifact",
    "AnkifyBatch",
    "AnkifyCard",
    "AnkifyRenderContext",
    "AnkifyResultDocument",
    "AnkifyRunOptions",
    "AnkifyWorkflowInput",
    "GroundingPolicy",
    "GuideProfile",
    "JuniorExamStage",
    "JuniorExamSubject",
    "LanguageTaskType",
    "NormalizedSource",
    "NoteType",
    "ParsedAnkifyRun",
    "ProvenanceType",
    "ProviderCardBatch",
    "ProviderCardCandidate",
    "QualityCode",
    "QualityIssue",
    "QualityOrigin",
    "QualitySeverity",
    "RejectedCandidate",
    "ResolvedStrategy",
    "SourceBlock",
    "SourceMode",
    "StudyPurpose",
    "TranslationDirection",
]
