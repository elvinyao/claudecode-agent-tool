"""Command-line entry point for Trivy JSON to HTML conversion."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from trivy_ai_report.models import AnalysisOutcome
from trivy_ai_report.providers import GEMINI_DEFAULT_MODEL, ProviderError, create_analyzer
from trivy_ai_report.renderer import OutputExistsError, write_html_report
from trivy_ai_report.rules import merge_recommendations
from trivy_ai_report.skills import SkillError, load_skill
from trivy_ai_report.trivy import TrivyReportError, load_trivy_report

EXIT_OK = 0
EXIT_INTERNAL_ERROR = 1
EXIT_INPUT_ERROR = 2
EXIT_DEGRADED = 3


def positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是数字") from exc
    if seconds <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return seconds


_PROVIDERS = ("codex", "claude", "gemini")


def _reported_model(provider: str, requested_model: str | None) -> str:
    if requested_model is not None:
        return requested_model
    return GEMINI_DEFAULT_MODEL if provider == "gemini" else "provider-default"


def build_parser(
    *,
    fixed_provider: str | None = None,
    prog: str | None = None,
) -> argparse.ArgumentParser:
    """Build the compatibility parser or one fixed-provider parser.

    A fixed-provider command intentionally has no ``--provider`` argument.  It
    is therefore impossible for a command named ``trivy-report-codex`` to start
    another provider because of a typo or copied flag.
    """

    if fixed_provider is not None and fixed_provider not in _PROVIDERS:
        raise ValueError(f"unsupported fixed provider: {fixed_provider}")
    provider_label = {
        "codex": "Codex",
        "claude": "Claude",
        "gemini": "Gemini",
    }.get(fixed_provider)
    parser = argparse.ArgumentParser(
        prog=prog or "trivy-ai-report",
        description=(
            f"使用 {provider_label} 为 Trivy native JSON v2 生成中文 HTML 整改报告。"
            if provider_label
            else "使用 Codex、Claude 或 Gemini 为 Trivy native JSON v2 生成中文 HTML 整改报告。"
        ),
    )
    if fixed_provider is None:
        parser.add_argument(
            "--provider",
            choices=_PROVIDERS,
            required=True,
            help="选择 Agent Provider（兼容入口必填）",
        )
    else:
        parser.set_defaults(provider=fixed_provider)
    parser.add_argument("--input", type=Path, required=True, help="Trivy native JSON v2 文件")
    parser.add_argument("--output", type=Path, required=True, help="输出 HTML 文件")
    parser.add_argument(
        "--model",
        help="可选的 provider 模型覆盖；Gemini 默认 gemini-3.6-flash",
    )
    parser.add_argument(
        "--skill",
        type=Path,
        metavar="PATH",
        help="可选整改 Skill 目录或 SKILL.md 文件",
    )
    parser.add_argument(
        "--enrich-web",
        action="store_true",
        help="Codex/Claude 联网核验所有漏洞；Gemini 当前会安全降级",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=positive_seconds,
        default=300.0,
        metavar="SECONDS",
        help="每批 Agent 请求超时（默认：300）",
    )
    parser.add_argument("--force", action="store_true", help="覆盖已有输出文件")
    return parser


def _check_output_path(path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        raise OutputExistsError(f"输出已存在：{path}；如需覆盖请使用 --force")
    if path.exists() and path.is_dir():
        raise OutputExistsError(f"输出路径是目录：{path}")


async def run(args: argparse.Namespace) -> int:
    """Execute one report run and return the documented process exit code."""

    try:
        _check_output_path(args.output, force=args.force)
        report = load_trivy_report(args.input)
    except (TrivyReportError, OutputExistsError, OSError) as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    try:
        skill = load_skill(args.skill) if args.skill is not None else None
    except (SkillError, OSError) as exc:
        print(f"Skill 输入错误：{exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    skill_name = skill.name if skill is not None else None

    if not report.findings:
        outcome = AnalysisOutcome(
            provider=args.provider,
            model=_reported_model(args.provider, args.model),
            skill_name=skill_name,
            enrich_web=args.enrich_web,
            recommendations=[],
        )
        try:
            write_html_report(report, outcome, args.output, force=args.force)
        except (OutputExistsError, OSError) as exc:
            print(f"输出错误：{exc}", file=sys.stderr)
            return EXIT_INPUT_ERROR
        print(f"报告已生成（未发现漏洞）：{args.output}")
        return EXIT_OK

    provider_failed = False
    failure_reason: str | None = None
    try:
        analyzer = create_analyzer(
            args.provider,
            model=args.model,
            enrich_web=args.enrich_web,
            batch_size=10 if args.enrich_web else 25,
            skill=skill,
        )
        outcome = await analyzer.analyze(
            report.findings,
            timeout_seconds=args.timeout_seconds,
        )
        outcome = outcome.model_copy(update={"skill_name": skill_name})
    except ProviderError as exc:
        provider_failed = True
        failure_reason = str(exc)
        outcome = AnalysisOutcome(
            provider=args.provider,
            model=_reported_model(args.provider, args.model),
            skill_name=skill_name,
            enrich_web=args.enrich_web,
            partial=True,
            warnings=[f"Agent 分析失败：{failure_reason}"],
        )

    merged, validation_warnings, validation_partial = merge_recommendations(
        report.findings,
        outcome.recommendations,
        failure_reason=failure_reason,
    )
    outcome = outcome.model_copy(
        update={
            "recommendations": merged,
            "warnings": [*outcome.warnings, *validation_warnings],
            "partial": outcome.partial or validation_partial or provider_failed,
        }
    )

    try:
        write_html_report(report, outcome, args.output, force=args.force)
    except (OutputExistsError, OSError) as exc:
        print(f"输出错误：{exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    if outcome.partial:
        print(f"报告已降级生成：{args.output}", file=sys.stderr)
        for warning in outcome.warnings:
            print(f"警告：{warning}", file=sys.stderr)
        return EXIT_DEGRADED

    print(f"报告已生成：{args.output}")
    return EXIT_OK


def _main_with_parser(
    parser: argparse.ArgumentParser,
    argv: Sequence[str] | None,
) -> int:
    args = parser.parse_args(argv)
    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        code = 130
    except Exception as exc:  # pragma: no cover - last-resort CLI boundary
        print(f"内部错误：{exc}", file=sys.stderr)
        code = EXIT_INTERNAL_ERROR

    if argv is None:
        raise SystemExit(code)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    """Compatibility entry point that retains the explicit ``--provider`` flag."""

    return _main_with_parser(build_parser(), argv)


def main_for_provider(
    provider: str,
    argv: Sequence[str] | None = None,
    *,
    prog: str | None = None,
) -> int:
    """Run a provider-specific CLI which does not expose ``--provider``."""

    return _main_with_parser(
        build_parser(fixed_provider=provider, prog=prog),
        argv,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
