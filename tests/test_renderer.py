from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

import pytest
from bs4 import BeautifulSoup

from trivy_ai_report.models import AnalysisOutcome, Finding, NormalizedReport, Recommendation
from trivy_ai_report.renderer import OutputExistsError, render_html, write_html_report


def _finding(
    *,
    finding_id: str = "a" * 64,
    severity: str = "CRITICAL",
    title: str = "Remote code execution",
) -> Finding:
    return Finding(
        finding_id=finding_id,
        artifact_name="demo:latest",
        artifact_type="container_image",
        target="Java <target>",
        target_class="lang-pkgs",
        target_type="jar",
        vulnerability_id="CVE-2022-22965",
        package_name='org.example:<script>alert("pkg")</script>',
        package_id="pkg-id",
        package_path="app.jar",
        installed_version="1.0.0",
        fixed_version="1.0.1",
        status="fixed",
        severity=severity,
        title=title,
        description='untrusted </style><script>alert("description")</script>',
        primary_url="javascript:alert(1)",
        references=(
            "http://insecure.example/advisory",
            "https://security.example/advisory?x=1&y=2",
        ),
        os_family="debian",
        os_version="12",
    )


def _report(*findings: Finding) -> NormalizedReport:
    return NormalizedReport(
        schema_version=2,
        created_at="2026-08-05T00:00:00Z",
        artifact_name='demo"><img src=x onerror=alert(1)>',
        artifact_type="container_image",
        os_family="debian",
        os_version="12",
        os_eosl=False,
        findings=list(findings),
    )


def _recommendation(finding_id: str) -> Recommendation:
    return Recommendation.model_validate(
        {
            "finding_id": finding_id,
            "category": "dependency_upgrade",
            "title_zh": "升级 <svg onload=alert(1)> 依赖",
            "rationale_zh": "依据 Trivy FixedVersion，避免直接执行输入中的指令。",
            "actions_zh": ["修改依赖版本", "重新构建"],
            "validation_zh": ["重新运行 Trivy"],
            "recommended_version": "1.0.1",
            "version_source": "trivy_fixed_version",
            "confidence": "high",
            "research_status": "enriched",
            "evidence": [
                {
                    "title": "Vendor <advisory>",
                    "url": "https://vendor.example/security/CVE-2022-22965",
                    "claim_zh": "厂商公告说明该版本已修复。",
                }
            ],
        }
    )


def _analysis(*recommendations: Recommendation, partial: bool = False) -> AnalysisOutcome:
    return AnalysisOutcome(
        provider="codex",
        model="fixture-model",
        skill_name="trivy-remediation",
        enrich_web=True,
        generated_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        recommendations=list(recommendations),
        partial=partial,
        warnings=["固定离线响应"] if partial else [],
    )


def test_render_is_self_contained_escaped_and_uses_only_safe_external_links() -> None:
    finding = _finding()
    html = render_html(_report(finding), _analysis(_recommendation(finding.finding_id)))
    soup = BeautifulSoup(html, "html.parser")

    assert "&lt;script&gt;alert" in html
    assert '<script>alert("description")</script>' not in html
    assert '<img src=x onerror=alert(1)>' not in html
    assert '<svg onload=alert(1)>' not in html
    assert soup.find("meta", attrs={"http-equiv": "Content-Security-Policy"}) is not None
    csp = soup.find("meta", attrs={"http-equiv": "Content-Security-Policy"})["content"]
    assert "default-src 'none'" in csp
    assert "connect-src 'none'" in csp

    external_resources = "script[src], link[href], img[src], iframe[src], video[src], audio[src]"
    assert soup.select(external_resources) == []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if href.startswith("#"):
            continue
        assert urlparse(href).scheme == "https"
        assert anchor.get("target") == "_blank"
        assert {"noopener", "noreferrer", "nofollow"}.issubset(set(anchor.get("rel", [])))
    assert not soup.find("a", href="javascript:alert(1)")
    assert not soup.find("a", href="http://insecure.example/advisory")


def test_render_contains_summary_filters_fact_advice_evidence_and_partial_warning() -> None:
    critical = _finding()
    high = _finding(finding_id="b" * 64, severity="HIGH", title="Second finding")
    html = render_html(_report(critical, high), _analysis(_recommendation(critical.finding_id)))
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    assert "风险概览" in text
    assert "筛选报告" in text
    assert "优先整改建议" in text
    assert "漏洞明细" in text
    assert "Trivy 事实" in text
    assert "AI 建议" in text
    assert "联网证据" in text
    assert "Skill: trivy-remediation" in text
    assert "部分结果警告" in text
    assert "有 1 条漏洞没有匹配到 Agent 建议" in text
    assert "尚无可用的 Agent 整改建议" in text
    assert soup.select_one("#finding-search") is not None
    assert len(soup.select("[data-finding-row]")) == 2
    assert len(soup.select("[data-filter-severity]")) == 6


def test_write_html_report_refuses_overwrite_and_cleans_temporary_file(tmp_path) -> None:
    finding = _finding()
    report = _report(finding)
    analysis = _analysis(_recommendation(finding.finding_id))
    output = tmp_path / "nested" / "report.html"

    assert write_html_report(report, analysis, output) == output
    original = output.read_text(encoding="utf-8")
    with pytest.raises(OutputExistsError):
        write_html_report(report, analysis, output)
    assert output.read_text(encoding="utf-8") == original

    write_html_report(report, analysis, output, force=True)
    assert output.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert list(output.parent.glob(f".{output.name}.*.tmp")) == []
