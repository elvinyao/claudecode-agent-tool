"""Asynchronous HTTP transport for trusted, pre-registered Agent Core plugins."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from agent_core.contracts import ActionMode, RunStatus
from agent_core.io import HttpsIoClient, HttpsIoError, HttpsIoPolicy, validate_https_url_syntax
from agent_core.jobs import (
    ArtifactNotReadyError,
    InMemoryJobManager,
    JobContext,
    JobExecutionResult,
    JobQueueFullError,
    RunNotFoundError,
    RunSnapshot,
    RunStoreFullError,
)

_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_ACTION_MODE_RANK = {
    ActionMode.DISABLED: 0,
    ActionMode.DRY_RUN: 1,
    ActionMode.APPLY: 2,
}


class _StrictApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InlineJsonSource(_StrictApiModel):
    type: Literal["inline"] = "inline"
    data: dict[str, Any]


class HttpsSource(_StrictApiModel):
    type: Literal["https"] = "https"
    url: SecretStr

    @field_validator("url")
    @classmethod
    def require_https(cls, value: SecretStr) -> SecretStr:
        try:
            validate_https_url_syntax(value.get_secret_value())
        except HttpsIoError as exc:
            raise ValueError(exc.public_message) from exc
        return value


RunSource = Annotated[InlineJsonSource | HttpsSource, Field(discriminator="type")]


class ArtifactSink(_StrictApiModel):
    type: Literal["artifact"] = "artifact"


class HttpsPutSink(_StrictApiModel):
    type: Literal["https"] = "https"
    url: SecretStr

    @field_validator("url")
    @classmethod
    def require_https(cls, value: SecretStr) -> SecretStr:
        try:
            validate_https_url_syntax(value.get_secret_value())
        except HttpsIoError as exc:
            raise ValueError(exc.public_message) from exc
        return value


RunSink = Annotated[ArtifactSink | HttpsPutSink, Field(discriminator="type")]


class RunOptions(_StrictApiModel):
    model: str | None = Field(default=None, min_length=1, max_length=128)
    enrich_web: bool = False
    timeout_seconds: float = Field(default=300.0, gt=0, le=3600.0)
    action_mode: ActionMode = ActionMode.DISABLED
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("model")
    @classmethod
    def trimmed_model(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("model must be trimmed")
        return value


class RunSubmitRequest(_StrictApiModel):
    plugin_id: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=64)
    source: RunSource
    sink: RunSink = Field(default_factory=ArtifactSink)
    options: RunOptions = Field(default_factory=RunOptions)

    @field_validator("plugin_id")
    @classmethod
    def valid_plugin_id(cls, value: str) -> str:
        if _PLUGIN_ID.fullmatch(value) is None:
            raise ValueError("plugin_id is invalid")
        return value

    @field_validator("provider")
    @classmethod
    def valid_provider(cls, value: str) -> str:
        if _PROVIDER_ID.fullmatch(value) is None:
            raise ValueError("provider is invalid")
        return value


class RunStatusResponse(_StrictApiModel):
    run_id: str
    status: RunStatus
    created_at: str
    started_at: str | None
    finished_at: str | None
    cancellation_requested: bool
    partial: bool
    warnings: list[str]
    error: dict[str, str] | None
    artifact_available: bool
    artifact_url: str | None


class PluginSchemaResponse(_StrictApiModel):
    plugin_id: str
    api_version: str | None = None
    plugin_version: str | None = None
    description: str | None = None
    source: str | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] | None = None
    options_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None


class PluginsResponse(_StrictApiModel):
    plugins: list[PluginSchemaResponse]


class HealthResponse(_StrictApiModel):
    status: Literal["ok", "not_ready"]
    plugins: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)


class WebSettings(_StrictApiModel):
    """Web safety defaults; a non-loopback bind requires bearer auth."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    api_token: SecretStr | None = None
    max_request_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    queue_capacity: int = Field(default=16, ge=1, le=10_000)
    worker_count: int = Field(default=2, ge=1, le=128)
    max_run_records: int = Field(default=256, ge=1, le=100_000)
    run_ttl_seconds: float = Field(default=3600.0, gt=0)
    max_action_mode: ActionMode = ActionMode.DISABLED
    allow_web_enrichment: bool = False
    https_policy: HttpsIoPolicy = Field(default_factory=HttpsIoPolicy)

    @model_validator(mode="after")
    def require_auth_for_remote_bind(self) -> WebSettings:
        if not _is_loopback_bind(self.host) and self.api_token is None:
            raise ValueError("a bearer API token is required for a non-loopback bind")
        if self.api_token is not None and not self.api_token.get_secret_value():
            raise ValueError("api_token must not be empty")
        return self


@dataclass(frozen=True, slots=True)
class ResolvedRunRequest:
    """URL-free work item passed from the web transport to the composition root."""

    plugin_id: str
    provider: str
    input_bytes: bytes
    input_media_type: str
    input_filename: str
    model: str | None
    enrich_web: bool
    timeout_seconds: float
    action_mode: ActionMode
    parameters: Mapping[str, Any]


class PluginRegistry(Protocol):
    def list(self) -> Sequence[Any]: ...

    def create(self, plugin_id: str) -> Any: ...


RunExecutor = Callable[[ResolvedRunRequest, JobContext], Awaitable[JobExecutionResult]]


class RequestBodyLimitMiddleware:
    """Count ASGI request chunks so chunked bodies cannot bypass the limit."""

    def __init__(self, app: Any, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_body_bytes:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                response = JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Invalid Content-Length header"},
                )
                await response(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> dict[str, Any]:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _RequestTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestTooLarge:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope: dict[str, Any], receive: Any, send: Any) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": "Request body exceeds the configured size limit"},
        )
        await response(scope, receive, send)


class _RequestTooLarge(Exception):
    pass


def _is_loopback_bind(host: str) -> bool:
    normalized = host.strip().lower().rstrip(".")
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _registry_descriptors(registry: PluginRegistry) -> tuple[Any, ...]:
    return tuple(registry.list())


def _value(source: Any, *names: str) -> Any:
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return source[name]
        return None
    for name in names:
        if hasattr(source, name):
            return getattr(source, name)
    return None


def _plugin_id_of(descriptor: Any) -> str:
    value = _value(descriptor, "plugin_id", "id", "name")
    return str(value or "")


def _json_schema(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, type) and issubclass(value, BaseModel):
        return value.model_json_schema()
    schema_method = getattr(value, "model_json_schema", None)
    if callable(schema_method):
        return dict(schema_method())
    return None


def _descriptor_schema(descriptor: Any) -> PluginSchemaResponse:
    plugin_id = _plugin_id_of(descriptor)

    def candidate(*names: str) -> Any:
        return _value(descriptor, *names)

    capabilities = candidate("required_capabilities") or ()

    return PluginSchemaResponse(
        plugin_id=plugin_id,
        api_version=_optional_string(candidate("api_version", "core_api_version")),
        plugin_version=_optional_string(candidate("plugin_version", "version")),
        description=_optional_string(candidate("description", "display_name")),
        source=_optional_string(candidate("source")),
        required_capabilities=[str(item) for item in capabilities],
        input_schema=_json_schema(candidate("input_schema", "input_model", "input_type")),
        options_schema=_json_schema(
            candidate("options_schema", "options_model", "options_type")
        ),
        output_schema=_json_schema(
            candidate("output_schema", "output_model", "output_type")
        ),
    )


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _status_response(snapshot: RunSnapshot) -> RunStatusResponse:
    error = None
    if snapshot.error_code is not None:
        error = {
            "code": snapshot.error_code,
            "message": snapshot.error_message or "Run failed",
        }
    return RunStatusResponse(
        run_id=snapshot.run_id,
        status=snapshot.state,
        created_at=snapshot.created_at.isoformat(),
        started_at=snapshot.started_at.isoformat() if snapshot.started_at else None,
        finished_at=snapshot.finished_at.isoformat() if snapshot.finished_at else None,
        cancellation_requested=snapshot.cancellation_requested,
        partial=snapshot.partial or snapshot.state is RunStatus.DEGRADED,
        warnings=list(snapshot.warnings),
        error=error,
        artifact_available=snapshot.artifact_available,
        artifact_url=(
            f"/api/v1/runs/{snapshot.run_id}/artifact"
            if snapshot.artifact_available
            else None
        ),
    )


def _safe_filename(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]", "_", value)[:128]
    return result or "artifact.bin"


def create_app(
    *,
    registry: PluginRegistry,
    provider_names: Sequence[str],
    run_executor: RunExecutor | None = None,
    settings: WebSettings | None = None,
    io_client: HttpsIoClient | None = None,
    job_manager: InMemoryJobManager | None = None,
    manage_job_lifecycle: bool = True,
    close_io_on_shutdown: bool | None = None,
) -> FastAPI:
    """Build an app from explicit registry, executor, queue, and I/O dependencies."""

    effective_settings = settings or WebSettings()
    normalized_providers = tuple(sorted({name.strip().lower() for name in provider_names}))
    if any(_PROVIDER_ID.fullmatch(name) is None for name in normalized_providers):
        raise ValueError("provider_names contains an invalid provider identifier")
    effective_io = io_client or HttpsIoClient(effective_settings.https_policy)
    owns_io = io_client is None if close_io_on_shutdown is None else close_io_on_shutdown

    if job_manager is None and run_executor is None:
        raise ValueError("run_executor is required when job_manager is not supplied")

    async def execute_submission(
        payload: RunSubmitRequest,
        context: JobContext,
    ) -> JobExecutionResult:
        context.raise_if_cancelled()
        if isinstance(payload.source, InlineJsonSource):
            raw_input = json.dumps(
                payload.source.data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(raw_input) > effective_settings.https_policy.max_input_bytes:
                raise HttpsIoError("input_too_large", "Inline input exceeds the size limit")
            media_type = "application/json"
            filename = "input.json"
        else:
            source_url = payload.source.url.get_secret_value()
            raw_input = await effective_io.fetch(source_url)
            media_type = "application/json"
            filename = _safe_filename(urlsplit(source_url).path.rsplit("/", 1)[-1])
        context.raise_if_cancelled()
        if run_executor is None:  # protected by factory validation
            raise RuntimeError("run executor is unavailable")
        resolved = ResolvedRunRequest(
            plugin_id=payload.plugin_id,
            provider=payload.provider,
            input_bytes=raw_input,
            input_media_type=media_type,
            input_filename=filename,
            model=payload.options.model,
            enrich_web=payload.options.enrich_web,
            timeout_seconds=payload.options.timeout_seconds,
            action_mode=payload.options.action_mode,
            parameters=dict(payload.options.parameters),
        )
        result = await run_executor(resolved, context)
        if not isinstance(result, JobExecutionResult):
            raise TypeError("run_executor must return JobExecutionResult")
        context.raise_if_cancelled()
        if isinstance(payload.sink, HttpsPutSink):
            await effective_io.publish(
                payload.sink.url.get_secret_value(),
                result.artifact.content,
                media_type=result.artifact.media_type,
            )
        return result

    manager = job_manager or InMemoryJobManager(
        execute_submission,
        queue_capacity=effective_settings.queue_capacity,
        worker_count=effective_settings.worker_count,
        max_records=effective_settings.max_run_records,
        ttl_seconds=effective_settings.run_ttl_seconds,
        max_artifact_bytes=effective_settings.https_policy.max_output_bytes,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        del app
        if manage_job_lifecycle:
            await manager.start()
        try:
            yield
        finally:
            if manage_job_lifecycle:
                await manager.close()
            if owns_io:
                await effective_io.aclose()

    app = FastAPI(
        title="Agent Core API",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=effective_settings.max_request_bytes,
    )
    app.state.settings = effective_settings
    app.state.registry = registry
    app.state.job_manager = manager
    app.state.io_client = effective_io

    @app.exception_handler(RequestValidationError)
    async def sanitized_validation_error(
        _request: Any,
        exc: RequestValidationError,
    ) -> JSONResponse:
        # FastAPI/Pydantic normally includes the rejected raw `input` value.
        # Source/sink URLs may carry signed query parameters, so expose only
        # structural location and safe validator messages.
        detail = [
            {
                "type": str(error.get("type", "value_error")),
                "loc": list(error.get("loc", ())),
                "msg": str(error.get("msg", "Invalid request")),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": detail},
        )

    async def authorize(authorization: str | None = Header(default=None)) -> None:
        configured = effective_settings.api_token
        if configured is None:
            return
        expected = configured.get_secret_value()
        scheme, separator, supplied = (authorization or "").partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not secrets.compare_digest(supplied, expected)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    api_auth = Depends(authorize)

    @app.get("/livez", response_model=HealthResponse)
    async def livez() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/readyz", response_model=HealthResponse)
    async def readyz(response: Response) -> HealthResponse:
        try:
            plugin_ids = sorted(
                plugin_id
                for descriptor in _registry_descriptors(registry)
                if (plugin_id := _plugin_id_of(descriptor))
            )
        except Exception:
            plugin_ids = []
        if not plugin_ids or not manager.started:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return HealthResponse(
                status="not_ready",
                plugins=plugin_ids,
                providers=list(normalized_providers),
            )
        if not normalized_providers:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return HealthResponse(status="not_ready", plugins=plugin_ids, providers=[])
        return HealthResponse(
            status="ok",
            plugins=plugin_ids,
            providers=list(normalized_providers),
        )

    @app.get(
        "/api/v1/plugins",
        response_model=PluginsResponse,
        dependencies=[api_auth],
    )
    async def plugins() -> PluginsResponse:
        descriptors = _registry_descriptors(registry)
        return PluginsResponse(
            plugins=[_descriptor_schema(descriptor) for descriptor in descriptors]
        )

    @app.get(
        "/api/v1/plugins/{plugin_id}/schema",
        response_model=PluginSchemaResponse,
        dependencies=[api_auth],
    )
    async def plugin_schema(plugin_id: str) -> PluginSchemaResponse:
        descriptor = next(
            (
                item
                for item in _registry_descriptors(registry)
                if _plugin_id_of(item) == plugin_id
            ),
            None,
        )
        if descriptor is None:
            raise HTTPException(status_code=404, detail="Plugin not found")
        return _descriptor_schema(descriptor)

    @app.post(
        "/api/v1/runs",
        response_model=RunStatusResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[api_auth],
    )
    async def submit_run(request: RunSubmitRequest) -> RunStatusResponse:
        plugin_ids = {_plugin_id_of(item) for item in _registry_descriptors(registry)}
        if request.plugin_id not in plugin_ids:
            raise HTTPException(status_code=404, detail="Plugin not found")
        if request.provider not in normalized_providers:
            raise HTTPException(status_code=404, detail="Provider not found")
        if (
            _ACTION_MODE_RANK[request.options.action_mode]
            > _ACTION_MODE_RANK[effective_settings.max_action_mode]
        ):
            raise HTTPException(status_code=403, detail="Requested action mode is not allowed")
        if request.options.enrich_web and not effective_settings.allow_web_enrichment:
            raise HTTPException(status_code=403, detail="Web enrichment is not allowed")
        try:
            if isinstance(request.source, HttpsSource):
                effective_io.validate_allowed_server(request.source.url.get_secret_value())
            if isinstance(request.sink, HttpsPutSink):
                effective_io.validate_allowed_server(request.sink.url.get_secret_value())
            snapshot = await manager.submit(request)
        except HttpsIoError as exc:
            raise HTTPException(status_code=422, detail=exc.public_message) from exc
        except (JobQueueFullError, RunStoreFullError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Run capacity is exhausted",
                headers={"Retry-After": "1"},
            ) from exc
        return _status_response(snapshot)

    @app.get(
        "/api/v1/runs/{run_id}",
        response_model=RunStatusResponse,
        dependencies=[api_auth],
    )
    async def run_status(run_id: str) -> RunStatusResponse:
        try:
            return _status_response(await manager.get(run_id))
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc

    @app.post(
        "/api/v1/runs/{run_id}/cancel",
        response_model=RunStatusResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[api_auth],
    )
    async def cancel_run(run_id: str) -> RunStatusResponse:
        try:
            return _status_response(await manager.cancel(run_id))
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc

    @app.get(
        "/api/v1/runs/{run_id}/artifact",
        dependencies=[api_auth],
    )
    async def run_artifact(run_id: str) -> Response:
        try:
            artifact = await manager.get_artifact(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        except ArtifactNotReadyError as exc:
            raise HTTPException(status_code=409, detail="Artifact is not ready") from exc
        filename = _safe_filename(artifact.filename)
        return Response(
            content=artifact.content,
            media_type=artifact.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    return app


def uvicorn_settings(settings: WebSettings) -> dict[str, Any]:
    """Return validated bind arguments without importing or starting Uvicorn."""

    return {"host": settings.host, "port": settings.port}


__all__ = [
    "ArtifactSink",
    "HealthResponse",
    "HttpsPutSink",
    "HttpsSource",
    "InlineJsonSource",
    "PluginSchemaResponse",
    "PluginsResponse",
    "ResolvedRunRequest",
    "RunExecutor",
    "RunOptions",
    "RunSink",
    "RunSource",
    "RunStatusResponse",
    "RunSubmitRequest",
    "WebSettings",
    "create_app",
    "uvicorn_settings",
]
