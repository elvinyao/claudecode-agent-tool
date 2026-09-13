from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from agent_core.contracts import RunStatus
from agent_core.jobs import (
    InMemoryJobManager,
    JobContext,
    JobExecutionResult,
    JobManagerError,
    RunArtifact,
    RunEvent,
    RunEventType,
    RunNotFoundError,
)
from agent_core.uploads import InMemoryUploadStore, UploadNotFoundError, UploadSnapshot
from agent_core.web import ResolvedRunRequest, RunSubmitRequest, WebSettings, create_app


class _Input(BaseModel):
    value: str | None = None


@dataclass(frozen=True)
class _Descriptor:
    plugin_id: str
    api_version: str = "1.0"
    plugin_version: str = "0.1.0"
    input_model: type[BaseModel] = _Input


class _Registry:
    def __init__(self, *plugin_ids: str) -> None:
        self._descriptors = tuple(
            _Descriptor(plugin_id=plugin_id) for plugin_id in (plugin_ids or ("demo",))
        )

    def list(self) -> tuple[_Descriptor, ...]:
        return self._descriptors

    def create(self, plugin_id: str) -> _Descriptor:
        for descriptor in self._descriptors:
            if descriptor.plugin_id == plugin_id:
                return descriptor
        raise KeyError(plugin_id)


class _FailingRegistry:
    def list(self) -> tuple[_Descriptor, ...]:
        raise RuntimeError("DO-NOT-ECHO-REGISTRY-DETAIL")

    def create(self, plugin_id: str) -> _Descriptor:
        raise KeyError(plugin_id)


def _client(app: Any) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1")


def _request(*, plugin_id: str = "demo") -> dict[str, Any]:
    return {
        "plugin_id": plugin_id,
        "provider": "codex",
        "source": {"type": "inline", "data": {"value": "safe"}},
    }


async def _echo_executor(
    request: ResolvedRunRequest,
    context: JobContext,
) -> JobExecutionResult:
    context.raise_if_cancelled()
    return JobExecutionResult(
        artifact=RunArtifact(
            request.input_bytes,
            media_type="application/json",
            filename="result.json",
        )
    )


def _wait_for_terminal(client: TestClient, run_id: str) -> dict[str, Any]:
    for _ in range(100):
        response = client.get(f"/api/v1/runs/{run_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"succeeded", "degraded", "failed", "cancelled"}:
            return payload
        time.sleep(0.005)
    raise AssertionError("run did not become terminal")


def test_async_preflight_is_awaited_and_validation_does_not_enqueue() -> None:
    seen: list[str] = []

    async def preflight(request: RunSubmitRequest) -> None:
        await asyncio.sleep(0)
        seen.append(request.plugin_id)

    app = create_app(
        registry=_Registry(),
        provider_names=("codex",),
        run_executor=_echo_executor,
        run_preflight=preflight,
    )

    with _client(app) as client:
        validated = client.post("/api/v1/runs/validate", json=_request())
        runs = client.get("/api/v1/runs")

    assert validated.status_code == 204
    assert seen == ["demo"]
    assert runs.json() == {"runs": [], "count": 0}


def test_unexpected_async_preflight_failure_is_redacted_and_not_enqueued() -> None:
    secret = "DO-NOT-ECHO-PREFLIGHT-DETAIL"

    async def preflight(_request: RunSubmitRequest) -> None:
        await asyncio.sleep(0)
        raise RuntimeError(secret)

    app = create_app(
        registry=_Registry(),
        provider_names=("codex",),
        run_executor=_echo_executor,
        run_preflight=preflight,
    )

    with _client(app) as client:
        validated = client.post("/api/v1/runs/validate", json=_request())
        submitted = client.post("/api/v1/runs", json=_request())
        runs = client.get("/api/v1/runs")

    assert validated.status_code == submitted.status_code == 503
    assert (
        validated.json()
        == submitted.json()
        == {"detail": "Plugin preflight validation is unavailable"}
    )
    assert secret not in validated.text + submitted.text
    assert runs.json()["count"] == 0


@pytest.mark.parametrize(
    "run_spec",
    (
        {
            **_request(),
            "plugin_id": "Invalid Plugin",
        },
        {
            **_request(),
            "provider": "Invalid Provider",
        },
        {
            **_request(),
            "source": {"type": "upload", "upload_id": "not-an-upload-id"},
        },
        {
            **_request(),
            "source": {"type": "text", "text": "safe", "filename": " input.txt"},
        },
        {
            **_request(),
            "source": {"type": "text", "text": "safe", "filename": "../input.txt"},
        },
        {
            **_request(),
            "source": {
                "type": "text",
                "text": "safe",
                "media_type": "text/plain\r\nX-Unsafe: yes",
            },
        },
        {
            **_request(),
            "options": {"model": " untrimmed"},
        },
    ),
)
def test_run_request_boundaries_are_rejected_before_admission(
    run_spec: dict[str, Any],
) -> None:
    app = create_app(
        registry=_Registry(),
        provider_names=("codex",),
        run_executor=_echo_executor,
    )

    with _client(app) as client:
        response = client.post("/api/v1/runs", json=run_spec)
        runs = client.get("/api/v1/runs")

    assert response.status_code == 422
    assert runs.json()["count"] == 0


def test_invalid_https_sink_is_redacted_before_admission() -> None:
    secret = "DO-NOT-ECHO-SINK-SECRET"
    app = create_app(
        registry=_Registry(),
        provider_names=("codex",),
        run_executor=_echo_executor,
    )

    with _client(app) as client:
        response = client.post(
            "/api/v1/runs",
            json={
                **_request(),
                "sink": {
                    "type": "https",
                    "url": f"http://example.test/output?token={secret}",
                },
            },
        )

    assert response.status_code == 422
    assert secret not in response.text


def test_web_settings_and_factory_reject_ambiguous_unsafe_configuration() -> None:
    with pytest.raises(ValidationError, match="api_token must not be empty"):
        WebSettings(api_token="")
    with pytest.raises(ValidationError, match="max_upload_bytes must not exceed"):
        WebSettings(max_upload_bytes=2, max_upload_total_bytes=1)
    with pytest.raises(ValueError, match="provider_names"):
        create_app(
            registry=_Registry(),
            provider_names=("invalid provider",),
            run_executor=_echo_executor,
        )
    with pytest.raises(ValueError, match="run_executor is required"):
        create_app(registry=_Registry(), provider_names=("codex",))


class _SchemaCarrier:
    def model_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"count": {"type": "integer"}}}


class _MappingRegistry:
    def list(self) -> tuple[dict[str, Any], ...]:
        return (
            {
                "id": "mapped",
                "version": 7,
                "required_capabilities": ["filesystem_read"],
                "input_schema": {"type": "object"},
                "options_schema": _SchemaCarrier(),
            },
        )

    def create(self, plugin_id: str) -> dict[str, Any]:
        if plugin_id != "mapped":
            raise KeyError(plugin_id)
        return self.list()[0]


def test_mapping_descriptor_and_schema_provider_are_normalized() -> None:
    app = create_app(
        registry=_MappingRegistry(),
        provider_names=("codex",),
        run_executor=_echo_executor,
    )

    with _client(app) as client:
        response = client.get("/api/v1/plugins/mapped/schema")

    assert response.status_code == 200
    assert response.json() == {
        "plugin_id": "mapped",
        "api_version": None,
        "plugin_version": "7",
        "description": None,
        "source": None,
        "required_capabilities": ["filesystem_read"],
        "input_schema": {"type": "object"},
        "options_schema": {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
        },
        "output_schema": None,
        "artifact_content_schema": None,
        "ownership": None,
    }


def test_failed_run_status_and_events_redact_executor_exception() -> None:
    secret = "DO-NOT-ECHO-EXECUTOR-DETAIL"

    async def failing_executor(
        _request: ResolvedRunRequest,
        _context: JobContext,
    ) -> JobExecutionResult:
        raise RuntimeError(secret)

    app = create_app(
        registry=_Registry(),
        provider_names=("codex",),
        run_executor=failing_executor,
    )

    with _client(app) as client:
        submitted = client.post("/api/v1/runs", json=_request())
        terminal = _wait_for_terminal(client, submitted.json()["run_id"])
        events = client.get(f"/api/v1/runs/{terminal['run_id']}/events")

    assert terminal["status"] == "failed"
    assert terminal["error"] == {
        "code": "execution_failed",
        "message": "Run execution failed",
    }
    assert "event: run.failed" in events.text
    assert secret not in str(terminal) + events.text


def test_missing_plugin_run_and_artifact_have_stable_not_found_responses() -> None:
    app = create_app(
        registry=_Registry(),
        provider_names=("codex",),
        run_executor=_echo_executor,
    )

    with _client(app) as client:
        known_plugin = client.get("/api/v1/plugins/demo/schema")
        plugin = client.get("/api/v1/plugins/missing/schema")
        run = client.get("/api/v1/runs/missing")
        artifact = client.get("/api/v1/runs/missing/artifact")
        cancel = client.post("/api/v1/runs/missing/cancel")
        rerun = client.post("/api/v1/runs/missing/rerun", json=_request())
        events = client.get("/api/v1/runs/missing/events")

    assert known_plugin.status_code == 200
    assert known_plugin.json()["plugin_id"] == "demo"
    assert plugin.status_code == 404
    assert plugin.json() == {"detail": "Plugin not found"}
    for response in (run, cancel, rerun, events):
        assert response.status_code == 404
        assert response.json() == {"detail": "Run not found"}
    assert artifact.status_code == 404
    assert artifact.json() == {"detail": "Run not found"}


def test_artifact_is_conflict_until_ready_and_rerun_plugin_must_match() -> None:
    blocker = asyncio.Event()

    async def blocked_executor(
        _request: ResolvedRunRequest,
        _context: JobContext,
    ) -> JobExecutionResult:
        await blocker.wait()
        return JobExecutionResult(artifact=RunArtifact(b"done"))

    blocked_app = create_app(
        registry=_Registry(),
        provider_names=("codex",),
        run_executor=blocked_executor,
    )
    with _client(blocked_app) as client:
        submitted = client.post("/api/v1/runs", json=_request())
        run_id = submitted.json()["run_id"]
        artifact = client.get(f"/api/v1/runs/{run_id}/artifact")
        client.post(f"/api/v1/runs/{run_id}/cancel")

    assert artifact.status_code == 409
    assert artifact.json() == {"detail": "Artifact is not ready"}

    rerun_app = create_app(
        registry=_Registry("demo", "other"),
        provider_names=("codex",),
        run_executor=_echo_executor,
    )
    with _client(rerun_app) as client:
        parent = client.post("/api/v1/runs", json=_request())
        parent_id = parent.json()["run_id"]
        _wait_for_terminal(client, parent_id)
        rerun = client.post(
            f"/api/v1/runs/{parent_id}/rerun",
            json=_request(plugin_id="other"),
        )

    assert rerun.status_code == 409
    assert rerun.json() == {"detail": "Rerun plugin must match parent run"}


def test_upload_capacity_returns_retryable_sanitized_response() -> None:
    upload_store = InMemoryUploadStore(max_records=1)
    app = create_app(
        registry=_Registry(),
        provider_names=("codex",),
        run_executor=_echo_executor,
        upload_store=upload_store,
    )

    with _client(app) as client:
        accepted = client.post(
            "/api/v1/uploads",
            files={"file": ("first.txt", b"first", "text/plain")},
        )
        exhausted = client.post(
            "/api/v1/uploads",
            files={"file": ("second.txt", b"second", "text/plain")},
        )
        invalid_delete = client.delete("/api/v1/uploads/not-an-upload-id")

    assert accepted.status_code == 201
    assert exhausted.status_code == 503
    assert exhausted.headers["retry-after"] == "1"
    assert exhausted.json() == {"detail": "Upload capacity is exhausted"}
    assert invalid_delete.status_code == 404
    assert invalid_delete.json() == {"detail": "Upload not found"}


def test_run_queue_capacity_returns_retryable_sanitized_response() -> None:
    started = Event()
    release = Event()

    async def blocked_executor(
        request: ResolvedRunRequest,
        context: JobContext,
    ) -> JobExecutionResult:
        started.set()
        await asyncio.to_thread(release.wait)
        return await _echo_executor(request, context)

    app = create_app(
        registry=_Registry(),
        provider_names=("codex",),
        run_executor=blocked_executor,
        settings=WebSettings(worker_count=1, queue_capacity=1),
    )

    with _client(app) as client:
        running = client.post("/api/v1/runs", json=_request())
        try:
            assert running.status_code == 202
            assert started.wait(timeout=1)
            queued = client.post("/api/v1/runs", json=_request())
            exhausted = client.post("/api/v1/runs", json=_request())
        finally:
            release.set()

    assert queued.status_code == 202
    assert exhausted.status_code == 503
    assert exhausted.headers["retry-after"] == "1"
    assert exhausted.json() == {"detail": "Run capacity is exhausted"}


class _RejectingUploadStore:
    max_upload_bytes = 1024

    async def put(
        self,
        content: bytes,
        *,
        filename: str,
        media_type: str,
    ) -> UploadSnapshot:
        del content, filename, media_type
        raise ValueError("invalid metadata: DO-NOT-ECHO-UPLOAD-DETAIL")

    async def get(self, upload_id: str) -> Any:
        raise UploadNotFoundError

    async def delete(self, upload_id: str) -> None:
        raise UploadNotFoundError

    async def stats(self) -> dict[str, int]:
        return {}


def test_injected_upload_store_metadata_error_is_redacted() -> None:
    app = create_app(
        registry=_Registry(),
        provider_names=("codex",),
        run_executor=_echo_executor,
        upload_store=_RejectingUploadStore(),
    )

    with _client(app) as client:
        response = client.post(
            "/api/v1/uploads",
            files={"file": ("safe.txt", b"safe", "text/plain")},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid upload metadata"}
    assert "DO-NOT-ECHO" not in response.text


def test_readiness_contains_registry_failure_and_does_not_leak_details() -> None:
    app = create_app(
        registry=_FailingRegistry(),
        provider_names=("codex",),
        run_executor=_echo_executor,
    )

    with _client(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "plugins": [],
        "providers": ["codex"],
    }
    assert "DO-NOT-ECHO" not in response.text


class _ExplodingListManager(InMemoryJobManager):
    async def list_runs(self, **_kwargs: Any) -> tuple[Any, ...]:
        raise JobManagerError("DO-NOT-ECHO-STORAGE-DETAIL")


async def _admitted_executor(_payload: Any, context: JobContext) -> JobExecutionResult:
    context.raise_if_cancelled()
    return JobExecutionResult(artifact=RunArtifact(b"unused"))


def test_global_job_manager_error_handler_is_sanitized() -> None:
    manager = _ExplodingListManager(_admitted_executor)
    app = create_app(
        registry=_Registry(),
        provider_names=("codex",),
        job_manager=manager,
    )

    with _client(app) as client:
        response = client.get("/api/v1/runs")

    assert response.status_code == 503
    assert response.json() == {"detail": "Run storage is unavailable"}
    assert "DO-NOT-ECHO" not in response.text


class _DisappearingEventManager:
    started = True

    async def get_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[RunEvent, ...]:
        assert after_sequence == 0
        return (
            RunEvent(
                run_id=run_id,
                sequence=1,
                type=RunEventType.RUN_STARTED,
                occurred_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
                status=RunStatus.RUNNING,
            ),
        )

    async def wait_for_events(self, *_args: Any, **_kwargs: Any) -> tuple[RunEvent, ...]:
        raise RunNotFoundError("DO-NOT-ECHO-DISAPPEARED-RUN")


def test_sse_ends_cleanly_when_run_disappears_after_initial_replay() -> None:
    app = create_app(
        registry=_Registry(),
        provider_names=("codex",),
        job_manager=_DisappearingEventManager(),  # ty: ignore[invalid-argument-type]  # Minimal job-manager double.
        manage_job_lifecycle=False,
    )

    with _client(app) as client:
        response = client.get("/api/v1/runs/transient/events")

    assert response.status_code == 200
    assert response.headers["x-event-replay-gap"] == "false"
    assert response.text.count("event: run.started") == 1
    assert "DO-NOT-ECHO" not in response.text
