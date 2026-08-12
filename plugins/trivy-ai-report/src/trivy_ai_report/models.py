"""Validated internal contracts shared by parsing, providers, and rendering."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TrivyRunOptions(BaseModel):
    """Strict per-run options exposed by the Trivy domain plugin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enrich_web: bool = False
    batch_size: int | None = Field(default=None, ge=1, le=100)
    use_bundled_skill: bool = True


class RecommendationCategory(str, Enum):
    OS_PACKAGE_UPGRADE = "os_package_upgrade"
    BASE_IMAGE_OR_OS_UPGRADE = "base_image_or_os_upgrade"
    SPRING_BOOT_OR_BOM_UPGRADE = "spring_boot_or_bom_upgrade"
    DEPENDENCY_UPGRADE = "dependency_upgrade"
    MITIGATION_OR_ACCEPTANCE = "mitigation_or_acceptance"
    MANUAL_REVIEW = "manual_review"


class VersionSource(str, Enum):
    TRIVY_FIXED_VERSION = "trivy_fixed_version"
    VENDOR_ADVISORY = "vendor_advisory"
    INFERRED = "inferred"
    NONE = "none"


class ResearchStatus(str, Enum):
    NOT_REQUESTED = "not_requested"
    ENRICHED = "enriched"
    NOT_FOUND = "not_found"
    FAILED = "failed"


class Evidence(BaseModel):
    """A web claim supplied by an agent. It is never treated as a Trivy fact."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=1, max_length=2_048)
    claim_zh: str = Field(min_length=1, max_length=1_000)

    @field_validator("url")
    @classmethod
    def require_https(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("evidence URL must use https")
        return value


class Finding(BaseModel):
    """An immutable vulnerability fact normalized from Trivy JSON v2."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_name: str
    artifact_type: str
    target: str
    target_class: str
    target_type: str
    vulnerability_id: str
    package_name: str
    package_id: str = ""
    package_path: str = ""
    installed_version: str
    fixed_version: str = ""
    status: str = ""
    severity: str
    title: str = ""
    description: str = ""
    primary_url: str = ""
    references: tuple[str, ...] = ()
    os_family: str = ""
    os_version: str = ""
    os_eosl: bool = False

    @property
    def is_fixable(self) -> bool:
        return bool(self.fixed_version.strip()) and self.status.lower() == "fixed"

    @property
    def priority(self) -> str:
        return {
            "CRITICAL": "P0",
            "HIGH": "P1",
            "MEDIUM": "P2",
            "LOW": "P3",
            "UNKNOWN": "P3",
        }.get(self.severity.upper(), "P3")


class NormalizedReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    created_at: str = ""
    artifact_name: str
    artifact_type: str
    os_family: str = ""
    os_version: str = ""
    os_eosl: bool = False
    findings: list[Finding]

    @property
    def severity_counts(self) -> dict[str, int]:
        counts = Counter(f.severity.upper() for f in self.findings)
        return {
            severity: counts.get(severity, 0)
            for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
        }

    @property
    def fixable_count(self) -> int:
        return sum(finding.is_fixable for finding in self.findings)


class Recommendation(BaseModel):
    """Agent-authored advice linked to one immutable Finding."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    category: RecommendationCategory
    title_zh: str = Field(min_length=1, max_length=300)
    rationale_zh: str = Field(min_length=1, max_length=2_000)
    actions_zh: list[str] = Field(min_length=1, max_length=8)
    validation_zh: list[str] = Field(min_length=1, max_length=6)
    recommended_version: str | None = Field(default=None, max_length=300)
    version_source: VersionSource = VersionSource.NONE
    confidence: Literal["low", "medium", "high"] = "medium"
    research_status: ResearchStatus = ResearchStatus.NOT_REQUESTED
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


class RecommendationBatch(BaseModel):
    """Domain recommendations after provider output has crossed the wire boundary."""

    model_config = ConfigDict(extra="forbid")

    recommendations: list[Recommendation]


class ProviderRecommendation(BaseModel):
    """Strict structured-output contract returned by an Agent provider.

    Every field is intentionally required by the wire schema. Nullable values use
    an explicit JSON ``null`` rather than relying on an omitted field and a domain
    default.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    category: RecommendationCategory
    title_zh: str = Field(min_length=1, max_length=300)
    rationale_zh: str = Field(min_length=1, max_length=2_000)
    actions_zh: list[str] = Field(min_length=1, max_length=8)
    validation_zh: list[str] = Field(min_length=1, max_length=6)
    recommended_version: str | None = Field(max_length=300)
    version_source: VersionSource
    confidence: Literal["low", "medium", "high"]
    research_status: ResearchStatus
    evidence: list[Evidence] = Field(max_length=10)

    def to_domain(self) -> Recommendation:
        """Revalidate provider data against the stable domain model."""

        return Recommendation.model_validate(self.model_dump(mode="python"))


class ProviderRecommendationBatch(BaseModel):
    """Top-level wire schema used only for provider structured output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recommendations: list[ProviderRecommendation]

    def to_domain(self) -> RecommendationBatch:
        """Explicitly cross the provider-to-domain trust boundary."""

        return RecommendationBatch(
            recommendations=[item.to_domain() for item in self.recommendations]
        )


class AnalysisOutcome(BaseModel):
    """Merged provider result used by the renderer."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str = "provider-default"
    skill_name: str | None = Field(default=None, max_length=128)
    enrich_web: bool = False
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    recommendations: list[Recommendation] = Field(default_factory=list)
    partial: bool = False
    warnings: list[str] = Field(default_factory=list)
