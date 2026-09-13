"""Orchestrate fixed fixtures through AgentRuntime, hard checks, and Judge."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone

from agent_core.contracts import RunStatus
from agent_core.providers import ProviderAdapter
from agent_core.runtime import AgentRuntime
from ankify.eval.checker import check_generated_cards
from ankify.eval.fixture_loader import load_eval_fixtures
from ankify.eval.judge import judge_generated_cards
from ankify.eval.models import (
    EvalCaseResult,
    EvalCaseStatus,
    EvalFixture,
    EvalMode,
    EvalReport,
    EvalRuleResult,
)
from ankify.models import AnkifyResultDocument
from ankify.strategies import resolve_strategy

DEFAULT_FIXTURE_SCORE_THRESHOLD = 75.0
DEFAULT_OVERALL_SCORE_THRESHOLD = 80.0


def _run_id(now: datetime) -> str:
    timestamp = now.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    return "eval-" + timestamp.replace(":", "-").replace(".", "-").replace("+00-00", "Z")


def _safe_error(exc: Exception) -> str:
    message = " ".join(str(exc).split()) or "no details"
    return f"{exc.__class__.__name__}: {message}"[:500]


def _empty_case(
    fixture: EvalFixture,
    *,
    status: EvalCaseStatus,
    error_message: str,
) -> EvalCaseResult:
    return EvalCaseResult(
        fixture_id=fixture.id,
        title=fixture.title,
        strategy_profile=resolve_strategy(fixture.options).profile,
        strategy_version=fixture.options.strategy_version or "unversioned",
        runtime_status=None,
        cards=(),
        rule_result=EvalRuleResult(passed=True),
        judge_result=None,
        status=status,
        total_score=None,
        error_message=error_message,
    )


async def run_eval_harness(
    *,
    runtime: AgentRuntime,
    mode: EvalMode,
    generation_provider: str | None,
    generation_model: str | None = None,
    judge_provider: ProviderAdapter | None = None,
    fixtures: Sequence[EvalFixture] | None = None,
    fixture_score_threshold: float = DEFAULT_FIXTURE_SCORE_THRESHOLD,
    overall_score_threshold: float = DEFAULT_OVERALL_SCORE_THRESHOLD,
    now: Callable[[], datetime] | None = None,
) -> EvalReport:
    """Run generation through the public runtime; no test-only shortcut is accepted."""

    if not 0 <= fixture_score_threshold <= 100:
        raise ValueError("fixture_score_threshold must be between 0 and 100")
    if not 0 <= overall_score_threshold <= 100:
        raise ValueError("overall_score_threshold must be between 0 and 100")
    effective_fixtures = tuple(fixtures) if fixtures is not None else load_eval_fixtures()
    current_time = (now or (lambda: datetime.now(timezone.utc)))()
    if current_time.tzinfo is None:
        raise ValueError("eval clock must return a timezone-aware datetime")

    if generation_provider is None:
        missing_status = EvalCaseStatus.FAIL if mode is EvalMode.STRICT else EvalCaseStatus.SKIPPED
        results = tuple(
            _empty_case(
                fixture,
                status=missing_status,
                error_message="Generation provider was not configured.",
            )
            for fixture in effective_fixtures
        )
        return EvalReport(
            run_id=_run_id(current_time),
            mode=mode,
            created_at=current_time.astimezone(timezone.utc).isoformat(),
            generation_provider=None,
            generation_model=None,
            judge_provider=judge_provider.name if judge_provider else None,
            judge_model=judge_provider.model if judge_provider else None,
            self_judged=False,
            fixture_score_threshold=fixture_score_threshold,
            overall_score_threshold=overall_score_threshold,
            overall_average=None,
            passed=mode is EvalMode.LOCAL,
            results=results,
        )

    results_list: list[EvalCaseResult] = []
    resolved_generation_model: str | None = generation_model
    for fixture in effective_fixtures:
        try:
            runtime_result = await runtime.run(
                plugin_id="ankify",
                provider=generation_provider,
                model=generation_model,
                input_bytes=fixture.source.text.encode("utf-8"),
                input_filename=fixture.source.filename,
                options=fixture.options.model_dump(mode="json"),
                allow_web_access=False,
                metadata={
                    "eval.fixture_id": fixture.id.value,
                    "eval.mode": mode.value,
                },
            )
            resolved_generation_model = runtime_result.model
            document = AnkifyResultDocument.model_validate_json(runtime_result.artifact.content)
            rule_result = check_generated_cards(fixture, document.cards)
            judge_result = None
            judge_error = None
            error_message = None
            if rule_result.passed and judge_provider is not None:
                try:
                    judge_result = await judge_generated_cards(
                        judge_provider,
                        fixture,
                        document.cards,
                    )
                except Exception as exc:
                    judge_error = _safe_error(exc)

            if not rule_result.passed:
                status = EvalCaseStatus.FAIL
            elif judge_provider is None:
                status = EvalCaseStatus.FAIL if mode is EvalMode.STRICT else EvalCaseStatus.WARN
                error_message = "Judge provider was not configured."
            elif judge_error is not None:
                status = EvalCaseStatus.FAIL if mode is EvalMode.STRICT else EvalCaseStatus.WARN
                error_message = judge_error
            elif judge_result is None:
                status = EvalCaseStatus.FAIL
                error_message = "Judge did not return a result."
            elif judge_result.overall_score < fixture_score_threshold:
                status = EvalCaseStatus.FAIL if mode is EvalMode.STRICT else EvalCaseStatus.WARN
            elif runtime_result.status is RunStatus.DEGRADED:
                status = EvalCaseStatus.WARN
            else:
                status = EvalCaseStatus.PASS

            results_list.append(
                EvalCaseResult(
                    fixture_id=fixture.id,
                    title=fixture.title,
                    strategy_profile=document.strategy_profile,
                    strategy_version=document.strategy_version,
                    runtime_status=runtime_result.status,
                    cards=document.cards,
                    rule_result=rule_result,
                    judge_result=judge_result,
                    status=status,
                    total_score=(judge_result.overall_score if judge_result is not None else None),
                    error_message=error_message,
                )
            )
        except Exception as exc:
            results_list.append(
                _empty_case(
                    fixture,
                    status=EvalCaseStatus.FAIL,
                    error_message=_safe_error(exc),
                )
            )

    results = tuple(results_list)
    scores = [result.total_score for result in results if result.total_score is not None]
    overall_average = round(sum(scores) / len(scores), 2) if scores else None
    strict_overall_pass = mode is EvalMode.LOCAL or (
        overall_average is not None and overall_average >= overall_score_threshold
    )
    passed = (
        all(
            result.status not in {EvalCaseStatus.FAIL, EvalCaseStatus.SKIPPED} for result in results
        )
        and strict_overall_pass
    )
    judge_name = judge_provider.name if judge_provider is not None else None
    judge_model = judge_provider.model if judge_provider is not None else None
    return EvalReport(
        run_id=_run_id(current_time),
        mode=mode,
        created_at=current_time.astimezone(timezone.utc).isoformat(),
        generation_provider=generation_provider,
        generation_model=resolved_generation_model,
        judge_provider=judge_name,
        judge_model=judge_model,
        self_judged=(
            generation_provider == judge_name
            and resolved_generation_model == judge_model
            and judge_name is not None
        ),
        fixture_score_threshold=fixture_score_threshold,
        overall_score_threshold=overall_score_threshold,
        overall_average=overall_average,
        passed=passed,
        results=results,
    )


__all__ = [
    "DEFAULT_FIXTURE_SCORE_THRESHOLD",
    "DEFAULT_OVERALL_SCORE_THRESHOLD",
    "run_eval_harness",
]
