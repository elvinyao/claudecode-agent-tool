"""Deterministic remediation guardrails around untrusted agent output."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

from trivy_ai_report.models import (
    Finding,
    Recommendation,
    RecommendationCategory,
    ResearchStatus,
    VersionSource,
)


class RecommendationValidationError(ValueError):
    """An agent recommendation conflicts with immutable Trivy facts."""


def category_hint(finding: Finding) -> RecommendationCategory:
    """Choose the only safe high-level remediation path for a finding."""

    if finding.target_class.lower() == "os-pkgs" and finding.os_eosl:
        return RecommendationCategory.BASE_IMAGE_OR_OS_UPGRADE
    if not finding.is_fixable:
        return RecommendationCategory.MITIGATION_OR_ACCEPTANCE
    if finding.target_class.lower() == "os-pkgs":
        return RecommendationCategory.OS_PACKAGE_UPGRADE
    if finding.package_name.startswith(("org.springframework.boot:", "org.springframework:")):
        return RecommendationCategory.SPRING_BOOT_OR_BOM_UPGRADE
    if finding.is_fixable:
        return RecommendationCategory.DEPENDENCY_UPGRADE
    return RecommendationCategory.MITIGATION_OR_ACCEPTANCE


def _fixed_versions(finding: Finding) -> list[str]:
    return [value.strip() for value in finding.fixed_version.split(",") if value.strip()]


def _numeric_prefix(value: str) -> tuple[int, ...]:
    match = re.match(r"^\s*v?(\d+(?:\.\d+)*)", value)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _preferred_fixed_version(finding: Finding) -> str | None:
    versions = _fixed_versions(finding)
    if not versions:
        return None
    installed = _numeric_prefix(finding.installed_version)
    if len(installed) >= 2:
        same_line = [
            candidate for candidate in versions if _numeric_prefix(candidate)[:2] == installed[:2]
        ]
        if same_line:
            return same_line[0]
    return versions[0]


def base_recommendation(finding: Finding, reason: str | None = None) -> Recommendation:
    """Build a conservative local fallback without inventing version facts."""

    category = category_hint(finding)
    preferred = _preferred_fixed_version(finding) if finding.is_fixable else None
    reason_suffix = f" Agent 分析未采用，需人工复核：{reason}" if reason else ""

    if category is RecommendationCategory.BASE_IMAGE_OR_OS_UPGRADE:
        return Recommendation(
            finding_id=finding.finding_id,
            category=category,
            title_zh=(
                f"迁移出已停止支持的 {finding.os_family or 'OS'} {finding.os_version or ''}"
            ).strip(),
            rationale_zh=(
                "Trivy 将该操作系统标记为 EOSL。单独升级软件包不能恢复发行版级安全支持；"
                f"应迁移到组织批准且仍受支持的基础镜像或 OS。{reason_suffix}"
            ),
            actions_zh=[
                "选择仍受厂商支持的基础镜像或 OS 版本，并在隔离分支中重建。",
                "核对运行时、原生依赖和系统库兼容性后，再分阶段发布。",
                "如必须短期缓解，可升级受影响软件包，但不要把单包升级视为 EOSL 的完整修复。",
            ],
            validation_zh=[
                "确认新扫描的 Metadata.OS.EOSL 不再为 true。",
                "对重建产物重新运行 Trivy，并执行启动、集成与回归测试。",
            ],
            recommended_version=None,
            version_source=VersionSource.NONE,
            confidence="high",
            research_status=ResearchStatus.NOT_REQUESTED,
        )

    if category is RecommendationCategory.OS_PACKAGE_UPGRADE:
        return Recommendation(
            finding_id=finding.finding_id,
            category=category,
            title_zh=f"升级 OS 软件包 {finding.package_name}",
            rationale_zh=(
                "使用发行版提供的安全包进行升级，保留发行版 backport 语义。"
                + (
                    f" Trivy FixedVersion 为 {preferred}。"
                    if preferred
                    else " Trivy 未给出可用修复版本。"
                )
                + reason_suffix
            ),
            actions_zh=[
                f"通过基础镜像的包管理器升级 {finding.package_name}，然后重建不可变镜像。",
                "不要直接用上游版本号替代发行版包版本；同时核对发行版安全公告。",
            ],
            validation_zh=["重新运行 Trivy，并确认 InstalledVersion 已进入修复范围。"],
            recommended_version=preferred,
            version_source=(VersionSource.TRIVY_FIXED_VERSION if preferred else VersionSource.NONE),
            confidence="high" if preferred else "medium",
            research_status=ResearchStatus.NOT_REQUESTED,
        )

    if category is RecommendationCategory.SPRING_BOOT_OR_BOM_UPGRADE:
        direct_boot = finding.package_name.startswith("org.springframework.boot:")
        spring_version = preferred if direct_boot else None
        return Recommendation(
            finding_id=finding.finding_id,
            category=category,
            title_zh="升级 Spring Boot parent / BOM",
            rationale_zh=(
                (
                    "该 finding 直接关联 Spring Boot 组件。"
                    if direct_boot
                    else "该 Spring 组件通常由 Spring Boot BOM 间接管理。"
                )
                + (
                    f" Trivy FixedVersion 为 {preferred}。"
                    if preferred
                    else " Trivy 未给出可用修复版本。"
                )
                + " Spring Framework 版本不能直接当作 Spring Boot 版本。"
                + reason_suffix
            ),
            actions_zh=[
                "在隔离分支中升级 Spring Boot parent、BOM 或 Gradle plugin；"
                "间接组件不要随意单独覆盖。",
                "运行 Maven dependency:tree 或 Gradle dependencies，"
                "确认实际解析出的 Spring 组件版本。",
            ],
            validation_zh=[
                "执行单元、集成和启动测试。",
                "重新运行 dependency tree 与 Trivy，确认旧组件及该 finding 已消失。",
            ],
            recommended_version=spring_version,
            version_source=(
                VersionSource.TRIVY_FIXED_VERSION if spring_version else VersionSource.NONE
            ),
            confidence="high" if direct_boot and preferred else "medium",
            research_status=ResearchStatus.NOT_REQUESTED,
        )

    if category is RecommendationCategory.DEPENDENCY_UPGRADE:
        return Recommendation(
            finding_id=finding.finding_id,
            category=category,
            title_zh=f"升级依赖 {finding.package_name}",
            rationale_zh=(
                f"Trivy 将状态标记为 fixed，并给出 FixedVersion {preferred}.{reason_suffix}"
            ),
            actions_zh=[
                "通过项目的依赖清单或锁文件升级到修复版本，重新解析依赖并构建产物。",
                "用依赖树确认没有通过其他路径继续引入旧版本。",
            ],
            validation_zh=["执行相关回归测试，并对最终产物重新运行 Trivy。"],
            recommended_version=preferred,
            version_source=VersionSource.TRIVY_FIXED_VERSION,
            confidence="high",
            research_status=ResearchStatus.NOT_REQUESTED,
        )

    return Recommendation(
        finding_id=finding.finding_id,
        category=RecommendationCategory.MITIGATION_OR_ACCEPTANCE,
        title_zh=f"缓解并人工复核 {finding.vulnerability_id}",
        rationale_zh=(
            "Trivy 当前没有同时满足 Status=fixed 且 FixedVersion 非空的可靠升级目标，"
            f"因此不能编造修复版本。{reason_suffix}"
        ),
        actions_zh=[
            "核对厂商公告、实际可达性和运行时暴露面，记录风险责任人与复核日期。",
            "评估禁用受影响功能、隔离、替换组件或升级到仍受支持产品线等缓解措施。",
        ],
        validation_zh=["实施缓解后执行安全回归并重新运行 Trivy；保留风险接受审批记录。"],
        recommended_version=None,
        version_source=VersionSource.NONE,
        confidence="medium",
        research_status=ResearchStatus.NOT_REQUESTED,
    )


def _canonical_trivy_version(finding: Finding, proposed: str | None) -> str | None:
    if proposed is None:
        return None
    matches = []
    for candidate in _fixed_versions(finding):
        pattern = rf"(?<![A-Za-z0-9._+\-]){re.escape(candidate)}(?![A-Za-z0-9._+\-])"
        if re.search(pattern, proposed):
            matches.append(candidate)
    if len(matches) != 1:
        raise RecommendationValidationError(
            f"recommended_version {proposed!r} 不能唯一映射到 Trivy FixedVersion"
        )
    return matches[0]


def validate_recommendation(finding: Finding, recommendation: Recommendation) -> Recommendation:
    """Cross-check one structured suggestion and normalize version provenance."""

    if recommendation.finding_id != finding.finding_id:
        raise RecommendationValidationError("finding_id 与 Trivy finding 不一致")
    expected_category = category_hint(finding)
    if recommendation.category is not expected_category:
        raise RecommendationValidationError(
            f"category 应为 {expected_category.value}，实际为 {recommendation.category.value}"
        )

    if recommendation.version_source is VersionSource.TRIVY_FIXED_VERSION:
        if not finding.is_fixable:
            raise RecommendationValidationError("Trivy 没有可靠的 fixed 状态/版本")
        if expected_category is RecommendationCategory.BASE_IMAGE_OR_OS_UPGRADE:
            raise RecommendationValidationError(
                "OS 软件包 FixedVersion 不能作为 EOSL 基础镜像的目标 OS 版本"
            )
        if (
            expected_category is RecommendationCategory.SPRING_BOOT_OR_BOM_UPGRADE
            and not finding.package_name.startswith("org.springframework.boot:")
        ):
            raise RecommendationValidationError(
                "Spring Framework FixedVersion 不能作为 Spring Boot/BOM 版本"
            )
        version = _canonical_trivy_version(finding, recommendation.recommended_version)
        if version is None:
            raise RecommendationValidationError("使用 Trivy FixedVersion 时必须给出版本")
        return recommendation.model_copy(update={"recommended_version": version})

    if recommendation.version_source is VersionSource.VENDOR_ADVISORY:
        if (
            not recommendation.recommended_version
            or recommendation.research_status is not ResearchStatus.ENRICHED
            or not recommendation.evidence
        ):
            raise RecommendationValidationError("厂商版本建议必须有联网核验状态、版本和 HTTPS 证据")
        return recommendation

    if recommendation.recommended_version is not None:
        raise RecommendationValidationError("没有版本来源时 recommended_version 必须为 null")
    if recommendation.version_source is VersionSource.INFERRED:
        raise RecommendationValidationError("不接受模型推断的整改版本")
    return recommendation


def merge_recommendations(
    findings: Sequence[Finding],
    recommendations: Sequence[Recommendation],
    *,
    failure_reason: str | None = None,
) -> tuple[list[Recommendation], list[str], bool]:
    """Validate agent output and fill every missing/unsafe item with a local fallback."""

    finding_by_id = {finding.finding_id: finding for finding in findings}
    counts = Counter(item.finding_id for item in recommendations)
    duplicate_ids = {finding_id for finding_id, count in counts.items() if count > 1}
    candidates = {
        item.finding_id: item for item in recommendations if item.finding_id not in duplicate_ids
    }
    warnings: list[str] = []
    partial = bool(failure_reason)

    for finding_id in sorted(duplicate_ids):
        warnings.append(f"Agent 对 finding {finding_id[:12]} 返回重复建议，已使用本地保守规则。")
        partial = True
    for finding_id in sorted(set(candidates) - set(finding_by_id)):
        warnings.append(f"Agent 返回未知 finding_id {finding_id[:12]}，已忽略。")
        partial = True

    merged: list[Recommendation] = []
    for finding in findings:
        candidate = candidates.get(finding.finding_id)
        if candidate is None or finding.finding_id in duplicate_ids:
            reason = failure_reason or "Agent 未返回唯一建议"
            merged.append(base_recommendation(finding, reason=reason))
            if not failure_reason and finding.finding_id not in duplicate_ids:
                warnings.append(
                    f"Agent 未返回 finding {finding.finding_id[:12]} 的建议，已使用本地保守规则。"
                )
            partial = True
            continue
        try:
            merged.append(validate_recommendation(finding, candidate))
        except RecommendationValidationError as exc:
            warnings.append(
                f"finding {finding.finding_id[:12]} 的 Agent 建议未通过校验：{exc}；"
                "已使用本地保守规则。"
            )
            merged.append(base_recommendation(finding, reason=str(exc)))
            partial = True

    return merged, warnings, partial


__all__ = [
    "RecommendationValidationError",
    "base_recommendation",
    "category_hint",
    "merge_recommendations",
    "validate_recommendation",
]
