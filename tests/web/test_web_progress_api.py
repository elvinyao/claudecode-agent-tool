from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient
from pydantic import BaseModel, field_validator

from agent_core.jobs import JobContext, JobExecutionResult, RunArtifact
from agent_core.progress import ProgressEventType, WorkflowProgress
from agent_core.web import ResolvedRunRequest, WebSettings, create_app


class _Input(BaseModel):
    value: str | None = None


@dataclass(frozen=True)
class _Descriptor:
    plugin_id: str = "demo"
    api_version: str = "1.0"
    plugin_version: str = "0.1.0"
    input_model: type[BaseModel] = _Input


class _Registry:
    def list(self) -> tuple[_Descriptor, ...]:
        return (_Descriptor(),)

    def create(self, plugin_id: str) -> _Descriptor:
        if plugin_id != "demo":
            raise KeyError(plugin_id)
        return _Descriptor()


def _client(app: Any) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1")


def _request(*, source: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "plugin_id": "demo",
        "provider": "codex",
        "source": source or {"type": "inline", "data": {"value": "safe"}},
    }


def _wait_for_terminal(client: TestClient, run_id: str) -> dict[str, Any]:
    for _ in range(100):
        response = client.get(f"/api/v1/runs/{run_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"succeeded", "degraded", "failed", "cancelled"}:
            return payload
        time.sleep(0.005)
    raise AssertionError("run did not become terminal")


async def _progress_executor(
    request: ResolvedRunRequest,
    context: JobContext,
) -> JobExecutionResult:
    await context.emit_progress(
        WorkflowProgress(
            run_id=context.run_id,
            type=ProgressEventType.AGENT_BATCH_STARTED,
            node_id="generate",
            node_kind="agent",
            node_status="running",
            attempt=1,
            max_attempts=3,
            delay_seconds=0.25,
            batch_size=4,
            accepted_count=2,
            duration_ms=12.5,
        )
    )
    return JobExecutionResult(
        artifact=RunArtifact(
            content=request.input_bytes,
            media_type="application/json",
            filename="result.json",
        )
    )


def test_sse_exposes_structured_progress_without_input_payload() -> None:
    app = create_app(
        registry=_Registry(),
        provider_names=("codex",),
        run_executor=_progress_executor,
    )

    with _client(app) as client:
        submitted = client.post(
            "/api/v1/runs",
            json=_request(
                source={
                    "type": "text",
                    "text": "DO-NOT-ECHO-PROGRESS",
                    "filename": "input.txt",
                }
            ),
        )
        run_id = submitted.json()["run_id"]
        _wait_for_terminal(client, run_id)
        response = client.get(f"/api/v1/runs/{run_id}/events")

    assert response.status_code == 200
    assert "event: agent.batch.started" in response.text
    for fragment in (
        '"node_id":"generate"',
        '"node_kind":"agent"',
        '"node_status":"running"',
        '"attempt":1',
        '"max_attempts":3',
        '"delay_seconds":0.25',
        '"batch_size":4',
        '"accepted_count":2',
        '"duration_ms":12.5',
    ):
        assert fragment in response.text
    assert "DO-NOT-ECHO-PROGRESS" not in response.text


def test_sse_explicitly_reports_a_bounded_replay_gap() -> None:
    app = create_app(
        registry=_Registry(),
        provider_names=("codex",),
        run_executor=_progress_executor,
        settings=WebSettings(max_events_per_run=2),
    )

    with _client(app) as client:
        submitted = client.post("/api/v1/runs", json=_request())
        run_id = submitted.json()["run_id"]
        _wait_for_terminal(client, run_id)
        response = client.get(f"/api/v1/runs/{run_id}/events", params={"after": 0})

    assert response.status_code == 200
    assert response.headers["x-event-replay-gap"] == "true"
    assert "event: stream.gap" in response.text
    assert '"after_sequence":0' in response.text
    assert '"first_available_sequence":' in response.text
    assert "event: run.completed" in response.text


def test_upload_can_be_explicitly_released_and_cannot_be_reused() -> None:
    app = create_app(
        registry=_Registry(),
        provider_names=("codex",),
        run_executor=_progress_executor,
    )

    with _client(app) as client:
        uploaded = client.post(
            "/api/v1/uploads",
            files={"file": ("notes.txt", b"temporary", "text/plain")},
        )
        upload_id = uploaded.json()["upload_id"]
        released = client.delete(f"/api/v1/uploads/{upload_id}")
        submitted = client.post(
            "/api/v1/runs",
            json=_request(source={"type": "upload", "upload_id": upload_id}),
        )
        released_again = client.delete(f"/api/v1/uploads/{upload_id}")

    assert uploaded.status_code == 201
    assert released.status_code == 204
    assert submitted.status_code == 404
    assert submitted.json()["detail"] == "Upload not found"
    assert released_again.status_code == 404


def test_plugin_preflight_never_echoes_custom_validator_input() -> None:
    secret = "DO-NOT-ECHO-PLUGIN-SECRET"

    class _Options(BaseModel):
        credential: str

        @field_validator("credential")
        @classmethod
        def reject_credential(cls, value: str) -> str:
            raise ValueError(f"rejected credential: {value}")

    def preflight(request: Any) -> None:
        _Options.model_validate(request.options.parameters, strict=True)

    app = create_app(
        registry=_Registry(),
        provider_names=("codex",),
        run_executor=_progress_executor,
        run_preflight=preflight,
    )

    with _client(app) as client:
        response = client.post(
            "/api/v1/runs/validate",
            json={
                **_request(),
                "options": {"parameters": {"credential": secret}},
            },
        )

    assert response.status_code == 422
    assert secret not in response.text
    assert response.json()["detail"] == [
        {
            "type": "value_error",
            "loc": ["options", "credential"],
            "msg": "Invalid plugin option",
        }
    ]
