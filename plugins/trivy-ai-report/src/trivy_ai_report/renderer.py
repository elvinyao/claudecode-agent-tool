"""Render a normalized Trivy report as a self-contained Chinese HTML document."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from .models import AnalysisOutcome, Finding, NormalizedReport, Recommendation

_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}

_CATEGORY_LABELS = {
    "os_package_upgrade": "升级 OS 软件包",
    "base_image_or_os_upgrade": "升级基础镜像 / OS",
    "spring_boot_or_bom_upgrade": "升级 Spring Boot / BOM",
    "dependency_upgrade": "升级应用依赖",
    "mitigation_or_acceptance": "缓解或风险接受",
    "manual_review": "人工复核",
}

_RESEARCH_LABELS = {
    "not_requested": "未启用联网核验",
    "enriched": "已联网核验",
    "not_found": "联网未找到证据",
    "failed": "联网核验失败",
}

_VERSION_SOURCE_LABELS = {
    "trivy_fixed_version": "Trivy FixedVersion",
    "vendor_advisory": "厂商公告",
    "inferred": "模型推断",
    "none": "未指定",
}


class OutputExistsError(ValueError):
    """Raised when rendering would overwrite an existing report without consent."""


def _https_url(value: str) -> str | None:
    """Return a safe external URL, or ``None`` for anything other than HTTPS."""

    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username:
        return None
    return parsed.geturl()


def _external_links(finding: Finding) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for label, candidate in (
        ("Trivy 主链接", finding.primary_url),
        *(("参考资料", reference) for reference in finding.references),
    ):
        url = _https_url(candidate)
        if url is not None and url not in seen:
            links.append({"label": label, "url": url})
            seen.add(url)
    return links


def _recommendation_view(recommendation: Recommendation | None) -> dict[str, Any]:
    if recommendation is None:
        return {
            "present": False,
            "category": "manual_review",
            "category_label": _CATEGORY_LABELS["manual_review"],
            "title": "尚无可用的 Agent 整改建议",
            "rationale": "本条漏洞未返回可验证的结构化建议，请由安全与应用负责人共同复核。",
            "actions": ["核对 Trivy 的 FixedVersion、厂商公告与实际依赖路径后再制定变更。"],
            "validation": ["完成变更后重新运行 Trivy，并确认该 finding 不再出现。"],
            "recommended_version": None,
            "version_source": "none",
            "version_source_label": _VERSION_SOURCE_LABELS["none"],
            "confidence": "low",
            "research_status": "not_requested",
            "research_label": _RESEARCH_LABELS["not_requested"],
            "evidence": [],
        }

    category = recommendation.category.value
    research_status = recommendation.research_status.value
    version_source = recommendation.version_source.value
    evidence = []
    for item in recommendation.evidence:
        url = _https_url(item.url)
        if url is not None:
            evidence.append({"title": item.title, "url": url, "claim": item.claim_zh})
    return {
        "present": True,
        "category": category,
        "category_label": _CATEGORY_LABELS.get(category, category),
        "title": recommendation.title_zh,
        "rationale": recommendation.rationale_zh,
        "actions": recommendation.actions_zh,
        "validation": recommendation.validation_zh,
        "recommended_version": recommendation.recommended_version,
        "version_source": version_source,
        "version_source_label": _VERSION_SOURCE_LABELS.get(version_source, version_source),
        "confidence": recommendation.confidence,
        "research_status": research_status,
        "research_label": _RESEARCH_LABELS.get(research_status, research_status),
        "evidence": evidence,
    }


def _finding_view(finding: Finding, recommendation: Recommendation | None) -> dict[str, Any]:
    rec = _recommendation_view(recommendation)
    severity = finding.severity.upper()
    search_text = " ".join(
        (
            finding.vulnerability_id,
            finding.package_name,
            finding.installed_version,
            finding.fixed_version,
            finding.target,
            finding.title,
            rec["title"],
            rec["rationale"],
        )
    ).lower()
    return {
        "id": finding.finding_id,
        "priority": finding.priority,
        "severity": severity,
        "vulnerability_id": finding.vulnerability_id,
        "package_name": finding.package_name,
        "package_id": finding.package_id,
        "package_path": finding.package_path,
        "installed_version": finding.installed_version,
        "fixed_version": finding.fixed_version,
        "status": finding.status,
        "target": finding.target,
        "target_class": finding.target_class,
        "target_type": finding.target_type,
        "title": finding.title,
        "description": finding.description,
        "is_fixable": finding.is_fixable,
        "links": _external_links(finding),
        "recommendation": rec,
        "search_text": search_text,
    }


def _build_context(report: NormalizedReport, analysis: AnalysisOutcome) -> dict[str, Any]:
    recommendations = {item.finding_id: item for item in analysis.recommendations}
    findings = [
        _finding_view(item, recommendations.get(item.finding_id)) for item in report.findings
    ]
    findings.sort(
        key=lambda item: (
            _SEVERITY_ORDER.get(item["severity"], _SEVERITY_ORDER["UNKNOWN"]),
            item["package_name"].lower(),
            item["vulnerability_id"],
        )
    )

    missing_count = sum(not item["recommendation"]["present"] for item in findings)
    warnings = list(analysis.warnings)
    if analysis.partial and not warnings:
        warnings.append("Agent 分析未完整完成；报告中的部分建议可能缺失。")
    if missing_count:
        warnings.append(f"有 {missing_count} 条漏洞没有匹配到 Agent 建议，已标记为人工复核。")

    return {
        "report": report,
        "analysis": analysis,
        "findings": findings,
        "severity_counts": report.severity_counts,
        "total_count": len(findings),
        "fixable_count": report.fixable_count,
        "missing_count": missing_count,
        "is_partial": analysis.partial or missing_count > 0,
        "warnings": warnings,
        "generated_at": analysis.generated_at.isoformat(),
    }


def _environment() -> Environment:
    template_dir = Path(__file__).resolve().parent / "templates"
    return Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(enabled_extensions=("html", "j2"), default_for_string=True),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(report: NormalizedReport, analysis: AnalysisOutcome) -> str:
    """Return a complete, offline-capable HTML report.

    Trivy fields and agent-authored text are always passed through Jinja autoescaping.
    Only validated HTTPS URLs are included as clickable external links.
    """

    template = _environment().get_template("report.html.j2")
    return template.render(**_build_context(report, analysis))


def write_html_report(
    report: NormalizedReport,
    analysis: AnalysisOutcome,
    output: str | Path,
    *,
    force: bool = False,
) -> Path:
    """Render and write a UTF-8 report, refusing accidental overwrite by default."""

    destination = Path(output)
    if destination.exists() and not force:
        raise OutputExistsError(f"output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    html = render_html(report, analysis)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(html)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return destination


__all__ = ["OutputExistsError", "render_html", "write_html_report"]
