"""Regenerate the checked-in HTML examples from fixed, offline analysis responses."""

from __future__ import annotations

from pathlib import Path

from trivy_ai_report.models import AnalysisOutcome
from trivy_ai_report.renderer import write_html_report
from trivy_ai_report.rules import merge_recommendations
from trivy_ai_report.trivy import load_trivy_report

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
OUTPUT = EXAMPLES / "output"

EXAMPLE_PAIRS = (
    (
        EXAMPLES / "trivy-spring-boot.json",
        EXAMPLES / "analysis-spring-boot.json",
        OUTPUT / "spring-boot-report.html",
    ),
    (
        EXAMPLES / "trivy-alpine-eosl.json",
        EXAMPLES / "analysis-alpine-eosl.json",
        OUTPUT / "alpine-eosl-report.html",
    ),
)


def main() -> int:
    for trivy_path, analysis_path, output_path in EXAMPLE_PAIRS:
        report = load_trivy_report(trivy_path)
        analysis = AnalysisOutcome.model_validate_json(analysis_path.read_text(encoding="utf-8"))
        report_ids = {finding.finding_id for finding in report.findings}
        analysis_ids = {item.finding_id for item in analysis.recommendations}
        if report_ids != analysis_ids:
            raise ValueError(
                f"fixed analysis IDs do not match {trivy_path.name}: "
                f"report={sorted(report_ids)}, analysis={sorted(analysis_ids)}"
            )
        merged, warnings, partial = merge_recommendations(
            report.findings,
            analysis.recommendations,
        )
        if partial:
            raise ValueError(
                f"fixed analysis failed local guardrails for {trivy_path.name}: {warnings}"
            )
        analysis = analysis.model_copy(
            update={
                "recommendations": merged,
                "warnings": [*analysis.warnings, *warnings],
            }
        )
        write_html_report(report, analysis, output_path, force=True)
        print(f"rendered {output_path.relative_to(ROOT)} (offline fixed response)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
