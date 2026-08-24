"""Command-line composition root for explicit live Ankify evaluation runs."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from agent_core.providers import DEFAULT_PROVIDER_REGISTRY, ProviderRegistry
from agent_core.registry import PluginRegistry
from agent_core.runtime import AgentRuntime
from ankify.eval.fixture_loader import load_eval_fixtures
from ankify.eval.models import EvalFixtureId, EvalMode
from ankify.eval.reporter import build_terminal_summary, write_eval_reports
from ankify.eval.runner import run_eval_harness
from ankify.plugin import create_plugin


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ankify-eval",
        description="Run bundled Ankify fixtures through the real AgentRuntime path.",
    )
    parser.add_argument("--mode", choices=("local", "strict"), default="local")
    parser.add_argument("--generation-provider", choices=("codex", "claude"))
    parser.add_argument("--generation-model")
    parser.add_argument("--judge-provider", choices=("codex", "claude"))
    parser.add_argument("--judge-model")
    parser.add_argument(
        "--fixture",
        action="append",
        choices=tuple(item.value for item in EvalFixtureId),
        help="Run one fixture; repeat for several. Default: all seven.",
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=Path("eval/reports"),
    )
    return parser


async def _run(
    args: argparse.Namespace,
    *,
    provider_registry: ProviderRegistry,
) -> int:
    plugin_registry = PluginRegistry()
    plugin_registry.register("ankify", create_plugin)
    runtime = AgentRuntime(plugin_registry, provider_registry=provider_registry)
    judge = (
        provider_registry.create(args.judge_provider, model=args.judge_model)
        if args.judge_provider
        else None
    )
    fixtures = load_eval_fixtures()
    if args.fixture:
        selected = frozenset(args.fixture)
        fixtures = tuple(fixture for fixture in fixtures if fixture.id.value in selected)
    report = await run_eval_harness(
        runtime=runtime,
        mode=EvalMode(args.mode),
        generation_provider=args.generation_provider,
        generation_model=args.generation_model,
        judge_provider=judge,
        fixtures=fixtures,
    )
    json_path, markdown_path = write_eval_reports(report, args.output_directory)
    print(build_terminal_summary(report))
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    return 0 if report.passed else 1


def main(
    argv: Sequence[str] | None = None,
    *,
    provider_registry: ProviderRegistry = DEFAULT_PROVIDER_REGISTRY,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args, provider_registry=provider_registry))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        message = " ".join(str(exc).split()) or "no details"
        print(f"error: {exc.__class__.__name__}: {message}", file=sys.stderr)
        return 1


__all__ = ["build_parser", "main"]
