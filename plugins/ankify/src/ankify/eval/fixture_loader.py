"""Stable loading and cross-validation of bundled Ankify fixtures."""

from __future__ import annotations

import json
from importlib import resources

from ankify.eval.models import EvalFixture, EvalFixtureId
from ankify.strategies import resolve_strategy

EVAL_FIXTURE_IDS: tuple[EvalFixtureId, ...] = tuple(EvalFixtureId)


def load_eval_fixtures() -> tuple[EvalFixture, ...]:
    fixture_root = resources.files("ankify.eval.fixtures")
    fixtures: list[EvalFixture] = []
    for fixture_id in EVAL_FIXTURE_IDS:
        resource = fixture_root.joinpath(f"{fixture_id.value}.json")
        fixture = EvalFixture.model_validate_json(resource.read_text(encoding="utf-8"))
        if fixture.id is not fixture_id:
            raise ValueError(f"fixture file {fixture_id.value!r} contains id {fixture.id.value!r}")
        strategy = resolve_strategy(fixture.options)
        if strategy.version != fixture.options.strategy_version:
            raise ValueError(f"fixture {fixture_id.value!r} pins an invalid strategy version")
        fixtures.append(fixture)
    return tuple(fixtures)


def fixture_manifest_json() -> str:
    """Return stable metadata without copying full source text into logs."""

    manifest = [
        {
            "id": fixture.id.value,
            "strategy_profile": resolve_strategy(fixture.options).profile.value,
            "strategy_version": fixture.options.strategy_version,
            "source_name": fixture.source.name,
        }
        for fixture in load_eval_fixtures()
    ]
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


__all__ = ["EVAL_FIXTURE_IDS", "fixture_manifest_json", "load_eval_fixtures"]
