"""Stable JSON, Markdown, terminal, and explicit filesystem reporting."""

from __future__ import annotations

import json
from pathlib import Path

from ankify.eval.models import EvalCaseResult, EvalReport


def serialize_json_report(report: EvalReport) -> str:
    return (
        json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def _score(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def build_terminal_summary(report: EvalReport) -> str:
    lines = [
        f"Ankify eval {report.run_id}",
        f"Mode: {report.mode.value}",
        f"Passed: {'yes' if report.passed else 'no'}",
        f"Generation: {report.generation_provider or 'not configured'} / "
        f"{report.generation_model or 'n/a'}",
        f"Judge: {report.judge_provider or 'not configured'} / {report.judge_model or 'n/a'}",
        f"Overall average: {_score(report.overall_average)}",
        "",
    ]
    for result in report.results:
        lines.append(
            f"{result.status.value.upper()} {result.fixture_id.value} "
            f"score={_score(result.total_score)} cards={len(result.cards)} "
            f"issues={len(result.rule_result.issues)}"
        )
    return "\n".join(lines)


def _case_markdown(result: EvalCaseResult) -> list[str]:
    lines = [
        f"## {result.status.value.upper()} {result.fixture_id.value}",
        "",
        f"- Title: {result.title}",
        f"- Strategy: {result.strategy_profile.value} / {result.strategy_version}",
        f"- Runtime: {result.runtime_status.value if result.runtime_status else 'not run'}",
        f"- Score: {_score(result.total_score)}",
        f"- Cards: {len(result.cards)}",
        f"- Error: {result.error_message or 'none'}",
        "",
        "### Deterministic issues",
        "",
    ]
    if result.rule_result.issues:
        for issue in result.rule_result.issues:
            location = "" if issue.card_index is None else f" card={issue.card_index + 1}"
            lines.append(f"- {issue.severity.value} {issue.check.value}{location}: {issue.message}")
    else:
        lines.append("- none")

    lines.extend(["", "### Judge", ""])
    if result.judge_result is None:
        lines.append("- unavailable")
    else:
        judge = result.judge_result
        lines.extend(
            [
                f"- Overall: {_score(judge.overall_score)}",
                f"- Atomicity: {judge.scores.atomicity}",
                f"- Grounding: {judge.scores.grounding}",
                f"- Learner fit: {judge.scores.learner_fit}",
                f"- Usefulness: {judge.scores.usefulness}",
                f"- Domain fit: {judge.scores.domain_fit}",
                "",
                "Reasons:",
                *(f"- {reason}" for reason in judge.reasons),
                "",
                "Suggestions:",
                *(f"- {suggestion}" for suggestion in judge.suggestions),
            ]
        )

    lines.extend(["", "### Sample cards", ""])
    if not result.cards:
        lines.append("- none")
    for index, card in enumerate(result.cards[:3], start=1):
        lines.extend(
            [
                f"**Card {index}**",
                f"- Front: {card.front}",
                f"- Back: {card.back}",
                f"- Tags: {', '.join(card.tags)}",
                f"- Evidence: {' | '.join(card.evidence_quotes)}",
                "",
            ]
        )
    return lines


def build_markdown_report(report: EvalReport) -> str:
    lines = [
        "# Ankify Agent Eval Report",
        "",
        f"- Run: {report.run_id}",
        f"- Created: {report.created_at}",
        f"- Mode: {report.mode.value}",
        f"- Passed: {'yes' if report.passed else 'no'}",
        f"- Generation: {report.generation_provider or 'not configured'} / "
        f"{report.generation_model or 'n/a'}",
        f"- Judge: {report.judge_provider or 'not configured'} / {report.judge_model or 'n/a'}",
        f"- Self-judged: {'yes' if report.self_judged else 'no'}",
        f"- Fixture threshold: {report.fixture_score_threshold:.2f}",
        f"- Overall threshold: {report.overall_score_threshold:.2f}",
        f"- Overall average: {_score(report.overall_average)}",
        "",
    ]
    for result in report.results:
        lines.extend(_case_markdown(result))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_eval_reports(report: EvalReport, output_directory: Path) -> tuple[Path, Path]:
    """Write only to the caller-selected directory; generation/Judge never write files."""

    output_directory.mkdir(parents=True, exist_ok=True)
    json_path = output_directory / f"{report.run_id}.json"
    markdown_path = output_directory / f"{report.run_id}.md"
    json_path.write_text(serialize_json_report(report), encoding="utf-8")
    markdown_path.write_text(build_markdown_report(report), encoding="utf-8")
    return json_path, markdown_path


__all__ = [
    "build_markdown_report",
    "build_terminal_summary",
    "serialize_json_report",
    "write_eval_reports",
]
