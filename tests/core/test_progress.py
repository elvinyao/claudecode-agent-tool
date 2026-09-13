from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from agent_core.progress import ProgressEventType, WorkflowProgress


def _progress(**updates: object) -> WorkflowProgress:
    values: dict[str, object] = {
        "run_id": "run-1",
        "type": ProgressEventType.STEP_STARTED,
        "node_id": "parse",
        "node_kind": "transform",
    }
    values.update(updates)
    return WorkflowProgress.model_validate(values, strict=True)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("run_id", "../run"),
        ("node_id", "node with spaces"),
        ("error_code", "https://secret.example/error"),
    ),
)
def test_progress_identifiers_fail_closed(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match="framework identifier"):
        _progress(**{field: value})


def test_progress_rejects_naive_time_and_normalizes_aware_time_to_utc() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _progress(occurred_at=datetime(2026, 9, 4, 12, 0))

    event = _progress(
        occurred_at=datetime(
            2026,
            9,
            4,
            21,
            0,
            tzinfo=timezone(timedelta(hours=9)),
        )
    )

    assert event.occurred_at == datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("attempt", 0),
        ("max_attempts", 11),
        ("delay_seconds", -0.1),
        ("batch_size", 1_000_001),
        ("accepted_count", -1),
        ("duration_ms", -1),
        ("duration_ms", float("nan")),
        ("duration_ms", float("inf")),
    ),
)
def test_progress_numeric_metadata_is_bounded(field: str, value: int | float) -> None:
    with pytest.raises(ValidationError):
        _progress(**{field: value})
