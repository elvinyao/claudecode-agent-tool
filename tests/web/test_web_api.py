from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from agent_core.io import HttpsIoError, HttpsIoPolicy, PublishReceipt
from agent_core.jobs import JobContext, JobExecutionResult, RunArtifact
from agent_core.uploads import InMemoryUploadStore
from agent_core.web import ResolvedRunRequest, WebSettings, create_app


class DemoInput(BaseModel):
    value: str


class DemoOutput(BaseModel):
    rendered: str


@dataclass(frozen=True)
class DemoDescriptor:
    plugin_id: str = "demo"
    api_version: str = "1.0"
    plugin_version: str = "0.1.0"
    description: str = "Demo plugin"
    input_model: type[BaseModel] = DemoInput
    output_model: type[BaseModel] = DemoOutput


class DemoRegistry:
    def __init__(self, *, empty: bool = False) -> None:
        self._descriptors = () if empty else (DemoDescriptor(),)

    def list(self):
        return self._descriptors

    def create(self, plugin_id: str):
        if plugin_id != "demo" or not self._descriptors:
            raise KeyError(plugin_id)
        return self._descriptors[0]


async def echo_executor(
    request: ResolvedRunRequest,
    context: JobContext,
) -> JobExecutionResult:
    context.raise_if_cancelled()
    assert request.plugin_id == "demo"
    assert request.provider == "codex"
    assert request.input_filename in {"input.json", "input", "notes.md", "input.txt"}
    return JobExecutionResult(
        artifact=RunArtifact(
            request.input_bytes,
            media_type="application/json",
            filename="report.json",
        )
    )


def wait_for_terminal(client: TestClient, run_id: str) -> dict[str, Any]:
    for _ in range(100):
        response = client.get(f"/api/v1/runs/{run_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"succeeded", "degraded", "failed", "cancelled"}:
            return payload
        time.sleep(0.005)
    raise AssertionError("run did not become terminal")


def test_liveness_readiness_plugins_schema_and_inline_run() -> None:
    app = create_app(
        registry=DemoRegistry(), provider_names=("codex",), run_executor=echo_executor
    )

    with TestClient(app) as client:
        assert client.get("/livez").json() == {
            "status": "ok",
            "plugins": [],
            "providers": [],
        }
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json() == {
            "status": "ok",
            "plugins": ["demo"],
            "providers": ["codex"],
        }

        plugins = client.get("/api/v1/plugins")
        assert plugins.status_code == 200
        descriptor = plugins.json()["plugins"][0]
        assert descriptor["plugin_id"] == "demo"
        assert descriptor["input_schema"]["title"] == "DemoInput"

        submitted = client.post(
            "/api/v1/runs",
            json={
                "plugin_id": "demo",
                "provider": "codex",
                "source": {"type": "inline", "data": {"value": "safe"}},
                "sink": {"type": "artifact"},
            },
        )
        assert submitted.status_code == 202
        run_id = submitted.json()["run_id"]
        terminal = wait_for_terminal(client, run_id)
        assert terminal["status"] == "succeeded"
        assert terminal["artifact_url"].endswith(f"/{run_id}/artifact")

        artifact = client.get(terminal["artifact_url"])
        assert artifact.status_code == 200
        assert artifact.json() == {"value": "safe"}
        assert artifact.headers["x-content-type-options"] == "nosniff"
        assert "report.json" in artifact.headers["content-disposition"]


def test_readiness_fails_closed_when_no_plugins_are_registered() -> None:
    app = create_app(
        registry=DemoRegistry(empty=True),
        provider_names=("codex",),
        run_executor=echo_executor,
    )

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "plugins": [],
        "providers": ["codex"],
    }


def test_readiness_fails_closed_without_providers_and_unknown_provider_is_rejected() -> None:
    empty_provider_app = create_app(
        registry=DemoRegistry(), provider_names=(), run_executor=echo_executor
    )
    normal_app = create_app(
        registry=DemoRegistry(), provider_names=("codex",), run_executor=echo_executor
    )

    with TestClient(empty_provider_app) as client:
        ready = client.get("/readyz")
    with TestClient(normal_app) as client:
        unknown = client.post(
            "/api/v1/runs",
            json={
                "plugin_id": "demo",
                "provider": "unknown",
                "source": {"type": "inline", "data": {}},
            },
        )

    assert ready.status_code == 503
    assert ready.json()["providers"] == []
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "Provider not found"


def test_request_cannot_expand_action_or_web_policy() -> None:
    app = create_app(
        registry=DemoRegistry(), provider_names=("codex",), run_executor=echo_executor
    )

    with TestClient(app) as client:
        dry_run = client.post(
            "/api/v1/runs",
            json={
                "plugin_id": "demo",
                "provider": "codex",
                "source": {"type": "inline", "data": {}},
                "options": {"action_mode": "dry_run"},
            },
        )
        web = client.post(
            "/api/v1/runs",
            json={
                "plugin_id": "demo",
                "provider": "codex",
                "source": {"type": "inline", "data": {}},
                "options": {"enrich_web": True},
            },
        )

    assert dry_run.status_code == 403
    assert web.status_code == 403


@pytest.mark.parametrize(
    "source",
    [
        {"type": "local", "path": "/etc/passwd"},
        {"type": "https", "url": "file:///etc/passwd"},
        {"type": "https", "url": "http://example.com/input"},
        {"type": "inline", "data": {}, "extra": True},
    ],
)
def test_source_is_strict_and_web_has_no_local_file_variant(source: dict[str, Any]) -> None:
    app = create_app(
        registry=DemoRegistry(), provider_names=("codex",), run_executor=echo_executor
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/runs",
            json={"plugin_id": "demo", "provider": "codex", "source": source},
        )

    assert response.status_code == 422


def test_validation_errors_never_echo_secret_url_input() -> None:
    app = create_app(
        registry=DemoRegistry(), provider_names=("codex",), run_executor=echo_executor
    )
    secret = "DO-NOT-ECHO-VALIDATION-SECRET"

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/runs",
            json={
                "plugin_id": "demo",
                "provider": "codex",
                "source": {
                    "type": "https",
                    "url": f"https://user:{secret}@example.com/input?token={secret}",
                },
            },
        )

    assert response.status_code == 422
    assert secret not in response.text
    assert "input" not in response.json()["detail"][0]


def test_unknown_request_fields_and_unknown_plugin_are_rejected() -> None:
    app = create_app(
        registry=DemoRegistry(), provider_names=("codex",), run_executor=echo_executor
    )

    with TestClient(app) as client:
        extra = client.post(
            "/api/v1/runs",
            json={
                "plugin_id": "demo",
                "provider": "codex",
                "source": {"type": "inline", "data": {}},
                "input_url": "https://example.com/ambiguous",
            },
        )
        missing = client.post(
            "/api/v1/runs",
            json={
                "plugin_id": "missing",
                "provider": "codex",
                "source": {"type": "inline", "data": {}},
            },
        )

    assert extra.status_code == 422
    assert missing.status_code == 404


def test_request_body_limit_returns_413_before_executor() -> None:
    called = False

    async def executor(request: ResolvedRunRequest, context: JobContext) -> JobExecutionResult:
        nonlocal called
        called = True
        return await echo_executor(request, context)

    settings = WebSettings(max_request_bytes=256)
    app = create_app(
        registry=DemoRegistry(),
        provider_names=("codex",),
        run_executor=executor,
        settings=settings,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/runs",
            json={
                "plugin_id": "demo",
                "provider": "codex",
                "source": {"type": "inline", "data": {"padding": "x" * 512}},
            },
        )

    assert response.status_code == 413
    assert called is False


def test_non_loopback_bind_requires_bearer_token_and_auth_is_constant_shape() -> None:
    with pytest.raises(ValidationError, match="bearer API token"):
        WebSettings(host="0.0.0.0")
    with pytest.raises(ValidationError, match="input size limit"):
        WebSettings(
            max_upload_bytes=5,
            max_upload_total_bytes=10,
            https_policy=HttpsIoPolicy(max_input_bytes=4),
        )

    settings = WebSettings(host="0.0.0.0", api_token="very-secret-token")
    app = create_app(
        registry=DemoRegistry(),
        provider_names=("codex",),
        run_executor=echo_executor,
        settings=settings,
    )

    with TestClient(app) as client:
        unauthorized = client.get("/api/v1/plugins")
        wrong = client.get(
            "/api/v1/plugins", headers={"Authorization": "Bearer wrong-secret"}
        )
        authorized = client.get(
            "/api/v1/plugins",
            headers={"Authorization": "Bearer very-secret-token"},
        )

    assert unauthorized.status_code == 401
    assert wrong.status_code == 401
    assert "very-secret-token" not in unauthorized.text + wrong.text
    assert authorized.status_code == 200


class FakeHttpsIo:
    def __init__(self) -> None:
        self.fetched: list[str] = []
        self.published: list[tuple[str, bytes, str]] = []
        self.closed = False

    def validate_allowed_server(self, url: str):
        if not url.startswith("https://allowed.example/"):
            raise HttpsIoError("server_not_allowed", "HTTPS destination is not allowed")
        return object()

    async def fetch(self, url: str) -> bytes:
        self.fetched.append(url)
        return b'{"remote":true}'

    async def publish(self, url: str, data: bytes, *, media_type: str) -> PublishReceipt:
        self.published.append((url, data, media_type))
        return PublishReceipt(status_code=204, server="allowed.example:443")

    async def aclose(self) -> None:
        self.closed = True


def test_https_source_and_sink_are_injected_and_signed_urls_are_not_echoed() -> None:
    fake_io = FakeHttpsIo()
    app = create_app(
        registry=DemoRegistry(),
        provider_names=("codex",),
        run_executor=echo_executor,
        io_client=fake_io,  # type: ignore[arg-type]
        close_io_on_shutdown=False,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/runs",
            json={
                "plugin_id": "demo",
                "provider": "codex",
                "source": {
                    "type": "https",
                    "url": "https://allowed.example/input?signature=SOURCE-SECRET",
                },
                "sink": {
                    "type": "https",
                    "url": "https://allowed.example/output?signature=SINK-SECRET",
                },
            },
        )
        assert response.status_code == 202
        terminal = wait_for_terminal(client, response.json()["run_id"])

    assert terminal["status"] == "succeeded"
    assert "SOURCE-SECRET" not in str(terminal)
    assert "SINK-SECRET" not in str(terminal)
    assert fake_io.fetched == [
        "https://allowed.example/input?signature=SOURCE-SECRET"
    ]
    assert fake_io.published[0][0].endswith("signature=SINK-SECRET")
    assert fake_io.published[0][1] == b'{"remote":true}'


def test_unallowed_https_server_is_rejected_before_queueing_and_redacted() -> None:
    policy = HttpsIoPolicy(allowed_servers={"allowed.example"})
    settings = WebSettings(https_policy=policy)
    app = create_app(
        registry=DemoRegistry(),
        provider_names=("codex",),
        run_executor=echo_executor,
        settings=settings,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/runs",
            json={
                "plugin_id": "demo",
                "provider": "codex",
                "source": {
                    "type": "https",
                    "url": "https://blocked.example/input?token=DO-NOT-ECHO",
                },
            },
        )

    assert response.status_code == 422
    assert "DO-NOT-ECHO" not in response.text


def test_cancel_endpoint_cancels_a_running_job() -> None:
    started = asyncio.Event()

    async def slow_executor(
        request: ResolvedRunRequest,
        context: JobContext,
    ) -> JobExecutionResult:
        started.set()
        await asyncio.Event().wait()
        return JobExecutionResult(artifact=RunArtifact(b"never"))

    app = create_app(
        registry=DemoRegistry(), provider_names=("codex",), run_executor=slow_executor
    )
    with TestClient(app) as client:
        submitted = client.post(
            "/api/v1/runs",
            json={
                "plugin_id": "demo",
                "provider": "codex",
                "source": {"type": "inline", "data": {}},
            },
        )
        run_id = submitted.json()["run_id"]
        for _ in range(100):
            current = client.get(f"/api/v1/runs/{run_id}").json()
            if current["status"] == "running":
                break
            time.sleep(0.005)
        cancelled = client.post(f"/api/v1/runs/{run_id}/cancel")
        terminal = wait_for_terminal(client, run_id)

    assert cancelled.status_code == 202
    assert cancelled.json()["cancellation_requested"] is True
    assert terminal["status"] == "cancelled"


def test_upload_source_list_and_explicit_rerun_lineage() -> None:
    app = create_app(
        registry=DemoRegistry(), provider_names=("codex",), run_executor=echo_executor
    )

    with TestClient(app) as client:
        uploaded = client.post(
            "/api/v1/uploads",
            files={"file": ("notes.md", b"# source\n", "text/markdown")},
        )
        assert uploaded.status_code == 201
        upload = uploaded.json()
        assert upload["filename"] == "notes.md"
        assert upload["media_type"] == "text/markdown"
        assert upload["size_bytes"] == len(b"# source\n")
        assert len(upload["sha256"]) == 64

        spec = {
            "plugin_id": "demo",
            "provider": "codex",
            "source": {"type": "upload", "upload_id": upload["upload_id"]},
            "sink": {"type": "artifact"},
        }
        submitted = client.post("/api/v1/runs", json=spec)
        assert submitted.status_code == 202
        parent_id = submitted.json()["run_id"]
        parent = wait_for_terminal(client, parent_id)
        assert parent["input_filename"] == "notes.md"
        assert parent["input_media_type"] == "text/markdown"
        assert parent["input_sha256"] == upload["sha256"]

        listing = client.get("/api/v1/runs", params={"plugin_id": "demo"})
        assert listing.status_code == 200
        assert listing.json()["count"] == 1
        assert listing.json()["runs"][0]["run_id"] == parent_id

        rerun = client.post(f"/api/v1/runs/{parent_id}/rerun", json=spec)
        assert rerun.status_code == 202
        child_id = rerun.json()["run_id"]
        child = wait_for_terminal(client, child_id)
        assert child_id != parent_id
        assert child["parent_run_id"] == parent_id

        children = client.get("/api/v1/runs", params={"parent_run_id": parent_id})
        assert children.json()["count"] == 1
        assert children.json()["runs"][0]["run_id"] == child_id

        artifact = client.get(child["artifact_url"])
        assert artifact.content == b"# source\n"


def test_upload_limits_unknown_upload_and_filename_sanitization() -> None:
    settings = WebSettings(
        max_request_bytes=1024,
        max_upload_bytes=4,
        max_upload_total_bytes=8,
    )
    app = create_app(
        registry=DemoRegistry(),
        provider_names=("codex",),
        run_executor=echo_executor,
        settings=settings,
    )

    with TestClient(app) as client:
        sanitized = client.post(
            "/api/v1/uploads",
            files={"file": ("../x.md", b"1234", "text/markdown")},
        )
        too_large = client.post(
            "/api/v1/uploads",
            files={"file": ("big.txt", b"12345", "text/plain")},
        )
        missing = client.post(
            "/api/v1/runs",
            json={
                "plugin_id": "demo",
                "provider": "codex",
                "source": {"type": "upload", "upload_id": "0" * 32},
            },
        )

    assert sanitized.status_code == 201
    assert sanitized.json()["filename"] == "x.md"
    assert too_large.status_code == 413
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Upload not found"


def test_accepted_upload_is_snapshotted_while_waiting_in_queue() -> None:
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)

    def clock() -> datetime:
        return now

    blocker_started = Event()
    release_blocker = Event()

    async def executor(
        request: ResolvedRunRequest,
        context: JobContext,
    ) -> JobExecutionResult:
        if request.input_filename == "input.json":
            blocker_started.set()
            await asyncio.to_thread(release_blocker.wait)
        context.raise_if_cancelled()
        return JobExecutionResult(
            artifact=RunArtifact(request.input_bytes, media_type="text/plain")
        )

    upload_store = InMemoryUploadStore(ttl_seconds=1, clock=clock)
    app = create_app(
        registry=DemoRegistry(),
        provider_names=("codex",),
        run_executor=executor,
        upload_store=upload_store,
        settings=WebSettings(worker_count=1),
    )

    with TestClient(app) as client:
        blocker = client.post(
            "/api/v1/runs",
            json={
                "plugin_id": "demo",
                "provider": "codex",
                "source": {"type": "inline", "data": {}},
            },
        )
        assert blocker.status_code == 202
        assert blocker_started.wait(timeout=1)

        uploaded = client.post(
            "/api/v1/uploads",
            files={"file": ("notes.md", b"retained", "text/markdown")},
        ).json()
        spec = {
            "plugin_id": "demo",
            "provider": "codex",
            "source": {"type": "upload", "upload_id": uploaded["upload_id"]},
        }
        queued = client.post("/api/v1/runs", json=spec)
        assert queued.status_code == 202

        now += timedelta(seconds=2)
        assert client.post("/api/v1/runs", json=spec).status_code == 404
        release_blocker.set()
        completed = wait_for_terminal(client, queued.json()["run_id"])
        artifact = client.get(completed["artifact_url"])

    assert completed["status"] == "succeeded"
    assert artifact.content == b"retained"


def test_sse_replays_safe_lifecycle_events_and_resumes_from_cursor() -> None:
    app = create_app(
        registry=DemoRegistry(), provider_names=("codex",), run_executor=echo_executor
    )

    with TestClient(app) as client:
        submitted = client.post(
            "/api/v1/runs",
            json={
                "plugin_id": "demo",
                "provider": "codex",
                "source": {"type": "text", "text": "DO-NOT-ECHO", "filename": "input.txt"},
            },
        )
        run_id = submitted.json()["run_id"]
        wait_for_terminal(client, run_id)

        stream = client.get(f"/api/v1/runs/{run_id}/events")
        resumed = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "2"},
        )
        terminal_only = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "3"},
        )
        invalid = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "invalid"},
        )

    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "event: run.queued" in stream.text
    assert "event: run.started" in stream.text
    assert "event: artifact.created" in stream.text
    assert "event: run.completed" in stream.text
    assert "DO-NOT-ECHO" not in stream.text
    assert "id: 1" not in resumed.text
    assert "id: 2" not in resumed.text
    assert "id: 3" in resumed.text
    assert "event: artifact.created" not in terminal_only.text
    assert "event: run.completed" in terminal_only.text
    assert '"artifact_available":true' in terminal_only.text
    assert invalid.status_code == 400


def test_rerun_requires_terminal_parent_and_same_plugin() -> None:
    started = asyncio.Event()

    async def slow_executor(
        request: ResolvedRunRequest,
        context: JobContext,
    ) -> JobExecutionResult:
        started.set()
        await asyncio.Event().wait()
        return JobExecutionResult(artifact=RunArtifact(b"never"))

    app = create_app(
        registry=DemoRegistry(), provider_names=("codex",), run_executor=slow_executor
    )
    spec = {
        "plugin_id": "demo",
        "provider": "codex",
        "source": {"type": "inline", "data": {}},
    }

    with TestClient(app) as client:
        parent = client.post("/api/v1/runs", json=spec)
        parent_id = parent.json()["run_id"]
        for _ in range(100):
            if client.get(f"/api/v1/runs/{parent_id}").json()["status"] == "running":
                break
            time.sleep(0.005)
        rerun = client.post(f"/api/v1/runs/{parent_id}/rerun", json=spec)
        client.post(f"/api/v1/runs/{parent_id}/cancel")

    assert rerun.status_code == 409
    assert rerun.json()["detail"] == "Only a terminal run can be rerun"
