from __future__ import annotations

import json

import pytest

from trivy_ai_report.models import Recommendation, RecommendationCategory, VersionSource
from trivy_ai_report.rules import (
    RecommendationValidationError,
    base_recommendation,
    category_hint,
    merge_recommendations,
    validate_recommendation,
)
from trivy_ai_report.trivy import (
    TrivyInputError,
    UnsupportedSchemaVersionError,
    load_trivy_report,
    parse_trivy_report,
)


def _raw(*, schema: int = 2, eosl: bool = False) -> dict:
    return {
        "SchemaVersion": schema,
        "ArtifactName": "demo:1",
        "ArtifactType": "container_image",
        "Metadata": {"OS": {"Family": "alpine", "Name": "3.10", "EOSL": eosl}},
        "Results": [
            {
                "Target": "demo:1 (alpine 3.10)",
                "Class": "os-pkgs",
                "Type": "alpine",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2026-0001",
                        "PkgName": "apk-tools",
                        "InstalledVersion": "2.10.6-r0",
                        "FixedVersion": "2.10.7-r0",
                        "Status": "fixed",
                        "Severity": "critical",
                    }
                ],
            }
        ],
    }


def _agent_recommendation(finding_id: str, **updates) -> Recommendation:
    values = {
        "finding_id": finding_id,
        "category": "os_package_upgrade",
        "title_zh": "升级 apk-tools",
        "rationale_zh": "使用 Trivy FixedVersion。",
        "actions_zh": ["升级软件包"],
        "validation_zh": ["重新运行 Trivy"],
        "recommended_version": "2.10.7-r0（最低修复版本）",
        "version_source": "trivy_fixed_version",
        "confidence": "high",
        "research_status": "not_requested",
        "evidence": [],
    }
    values.update(updates)
    return Recommendation.model_validate(values)


def test_parse_v2_deduplicates_and_has_stable_identity() -> None:
    raw = _raw()
    raw["Results"][0]["Vulnerabilities"].append(dict(raw["Results"][0]["Vulnerabilities"][0]))
    first = parse_trivy_report(raw)
    second = parse_trivy_report(raw)
    assert len(first.findings) == 1
    assert first.findings[0].finding_id == second.findings[0].finding_id
    assert first.findings[0].severity == "CRITICAL"
    assert first.fixable_count == 1


def test_load_reports_precise_json_and_schema_errors(tmp_path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(TrivyInputError, match="不是有效 JSON"):
        load_trivy_report(invalid)
    with pytest.raises(UnsupportedSchemaVersionError):
        parse_trivy_report(_raw(schema=1))

    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps({"SchemaVersion": 2}), encoding="utf-8")
    with pytest.raises(TrivyInputError, match="ArtifactName"):
        load_trivy_report(missing)


def test_category_and_baseline_prioritize_eosl() -> None:
    finding = parse_trivy_report(_raw(eosl=True)).findings[0]
    assert category_hint(finding) is RecommendationCategory.BASE_IMAGE_OR_OS_UPGRADE
    fallback = base_recommendation(finding)
    assert fallback.category is RecommendationCategory.BASE_IMAGE_OR_OS_UPGRADE
    assert "EOSL" in fallback.rationale_zh
    assert fallback.recommended_version is None
    assert fallback.version_source is VersionSource.NONE


def test_validation_normalizes_fixed_version_and_rejects_wrong_category() -> None:
    finding = parse_trivy_report(_raw()).findings[0]
    valid = validate_recommendation(finding, _agent_recommendation(finding.finding_id))
    assert valid.recommended_version == "2.10.7-r0"

    ambiguous_substring = _agent_recommendation(
        finding.finding_id,
        recommended_version="12.10.7-r0",
    )
    with pytest.raises(RecommendationValidationError, match="不能唯一映射"):
        validate_recommendation(finding, ambiguous_substring)

    wrong = _agent_recommendation(
        finding.finding_id,
        category="dependency_upgrade",
    )
    with pytest.raises(RecommendationValidationError, match="category"):
        validate_recommendation(finding, wrong)


def test_no_fix_never_accepts_inferred_version() -> None:
    raw = _raw()
    vulnerability = raw["Results"][0]["Vulnerabilities"][0]
    vulnerability["FixedVersion"] = ""
    vulnerability["Status"] = "will_not_fix"
    finding = parse_trivy_report(raw).findings[0]
    recommendation = _agent_recommendation(
        finding.finding_id,
        category="mitigation_or_acceptance",
        recommended_version="9.9.9",
        version_source=VersionSource.INFERRED,
    )
    with pytest.raises(RecommendationValidationError):
        validate_recommendation(finding, recommendation)


def test_merge_falls_back_for_missing_duplicate_unknown_and_invalid() -> None:
    finding = parse_trivy_report(_raw()).findings[0]
    duplicate = _agent_recommendation(finding.finding_id)
    unknown = _agent_recommendation("f" * 64)
    merged, warnings, partial = merge_recommendations([finding], [duplicate, duplicate, unknown])
    assert partial
    assert len(merged) == 1
    assert merged[0].finding_id == finding.finding_id
    assert any("重复" in warning for warning in warnings)
    assert any("未知" in warning for warning in warnings)


def test_spring_boot_prefers_same_maintenance_line() -> None:
    raw = _raw()
    result = raw["Results"][0]
    result["Class"] = "lang-pkgs"
    result["Type"] = "jar"
    vulnerability = result["Vulnerabilities"][0]
    vulnerability.update(
        {
            "PkgName": "org.springframework.boot:spring-boot",
            "InstalledVersion": "2.6.3",
            "FixedVersion": "2.5.12, 2.6.6",
        }
    )
    finding = parse_trivy_report(raw).findings[0]
    fallback = base_recommendation(finding)
    assert fallback.category is RecommendationCategory.SPRING_BOOT_OR_BOM_UPGRADE
    assert fallback.recommended_version == "2.6.6"


def test_indirect_spring_component_version_is_not_presented_as_boot_version() -> None:
    raw = _raw()
    result = raw["Results"][0]
    result["Class"] = "lang-pkgs"
    result["Type"] = "jar"
    vulnerability = result["Vulnerabilities"][0]
    vulnerability.update(
        {
            "PkgName": "org.springframework:spring-webmvc",
            "InstalledVersion": "5.3.17",
            "FixedVersion": "5.3.18",
        }
    )
    finding = parse_trivy_report(raw).findings[0]
    fallback = base_recommendation(finding)
    assert fallback.category is RecommendationCategory.SPRING_BOOT_OR_BOM_UPGRADE
    assert fallback.recommended_version is None
    proposal = _agent_recommendation(
        finding.finding_id,
        category="spring_boot_or_bom_upgrade",
        recommended_version="5.3.18",
    )
    with pytest.raises(RecommendationValidationError, match="不能作为 Spring Boot"):
        validate_recommendation(finding, proposal)
