"""Asynchronous HTTP transport for trusted, pre-registered Agent Core plugins."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
from ipaddress import ip_address
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlsplit

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
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
    TERMINAL_STATES,
    ArtifactNotReadyError,
    InMemoryJobManager,
    JobContext,
    JobExecutionResult,
    JobManager,
    JobQueueFullError,
    RunEvent,
    RunEventType,
    RunMetadata,
    RunNotFoundError,
    RunSnapshot,
    RunStoreFullError,
)
from agent_core.uploads import (
    InMemoryUploadStore,
    UploadedArtifact,
    UploadNotFoundError,
    UploadSnapshot,
    UploadStore,
    UploadStoreFullError,
    UploadTooLargeError,
)

_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_UPLOAD_ID = re.compile(r"^[a-f0-9]{32}$")
_ACTION_MODE_RANK = {
    ActionMode.DISABLED: 0,
    ActionMode.DRY_RUN: 1,
    ActionMode.APPLY: 2,
}
_TERMINAL_EVENT_TYPES = frozenset(
    {
        RunEventType.RUN_COMPLETED,
        RunEventType.RUN_FAILED,
        RunEventType.RUN_CANCELLED,
    }
)


class _StrictApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InlineJsonSource(_StrictApiModel):
    type: Literal["inline"] = "inline"
    data: dict[str, Any]


class InlineTextSource(_StrictApiModel):
    type: Literal["text"] = "text"
    text: str = Field(max_length=10 * 1024 * 1024)
    filename: str = Field(default="input.txt", min_length=1, max_length=255)
    media_type: str = Field(default="text/plain; charset=utf-8", min_length=1, max_length=255)

    @field_validator("filename")
    @classmethod
    def valid_filename(cls, value: str) -> str:
        return _validate_input_filename(value)

    @field_validator("media_type")
    @classmethod
    def valid_media_type(cls, value: str) -> str:
        return _validate_media_type(value)


class UploadedSource(_StrictApiModel):
    type: Literal["upload"] = "upload"
    upload_id: str

    @field_validator("upload_id")
    @classmethod
    def valid_upload_id(cls, value: str) -> str:
        if _UPLOAD_ID.fullmatch(value) is None:
            raise ValueError("upload_id is invalid")
        return value


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


RunSource = Annotated[
    InlineJsonSource | InlineTextSource | UploadedSource | HttpsSource,
    Field(discriminator="type"),
]


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
    plugin_id: str | None = None
    provider: str | None = None
    model: str | None = None
    input_filename: str | None = None
    input_media_type: str | None = None
    input_sha256: str | None = None
    parent_run_id: str | None = None


class RunsResponse(_StrictApiModel):
    runs: list[RunStatusResponse]
    count: int


class UploadResponse(_StrictApiModel):
    upload_id: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    created_at: str
    expires_at: str


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
    max_upload_records: int = Field(default=128, ge=1, le=100_000)
    max_upload_bytes: int = Field(default=8 * 1024 * 1024, ge=1)
    max_upload_total_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    upload_ttl_seconds: float = Field(default=3600.0, gt=0)
    max_action_mode: ActionMode = ActionMode.DISABLED
    allow_web_enrichment: bool = False
    https_policy: HttpsIoPolicy = Field(default_factory=HttpsIoPolicy)

    @model_validator(mode="after")
    def require_auth_for_remote_bind(self) -> WebSettings:
        if not _is_loopback_bind(self.host) and self.api_token is None:
            raise ValueError("a bearer API token is required for a non-loopback bind")
        if self.api_token is not None and not self.api_token.get_secret_value():
            raise ValueError("api_token must not be empty")
        if self.max_upload_bytes > self.max_upload_total_bytes:
            raise ValueError("max_upload_bytes must not exceed max_upload_total_bytes")
        if self.max_upload_bytes > self.https_policy.max_input_bytes:
            raise ValueError("max_upload_bytes must not exceed the input size limit")
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


@dataclass(frozen=True, slots=True)
class _AdmittedRunRequest:
    """Queued request plus an immutable snapshot of an admitted upload."""

    request: RunSubmitRequest
    uploaded_artifact: UploadedArtifact | None = None


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
        plugin_id=snapshot.metadata.plugin_id,
        provider=snapshot.metadata.provider,
        model=snapshot.metadata.model,
        input_filename=snapshot.metadata.input_filename,
        input_media_type=snapshot.metadata.input_media_type,
        input_sha256=snapshot.metadata.input_sha256,
        parent_run_id=snapshot.metadata.parent_run_id,
    )


def _upload_response(snapshot: UploadSnapshot) -> UploadResponse:
    return UploadResponse(
        upload_id=snapshot.upload_id,
        filename=snapshot.filename,
        media_type=snapshot.media_type,
        size_bytes=snapshot.size_bytes,
        sha256=snapshot.sha256,
        created_at=snapshot.created_at.isoformat(),
        expires_at=snapshot.expires_at.isoformat(),
    )


def _validate_input_filename(value: str) -> str:
    if value != value.strip() or value in {".", ".."}:
        raise ValueError("filename must be non-empty and trimmed")
    if "/" in value or "\\" in value or "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("filename must not contain a path or control characters")
    return value


def _uploaded_filename(value: str | None) -> str:
    candidate = (value or "upload.bin").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not candidate or candidate in {".", ".."}:
        candidate = "upload.bin"
    candidate = candidate.replace("\x00", "_").replace("\r", "_").replace("\n", "_")
    if len(candidate) <= 255:
        return candidate
    stem, separator, suffix = candidate.rpartition(".")
    if stem and separator and 0 < len(suffix) <= 32:
        return f"{stem[: 254 - len(suffix)]}.{suffix}"
    return candidate[:255]


def _validate_media_type(value: str) -> str:
    if value != value.strip() or "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("media_type must be trimmed single-line text")
    return value


def _safe_filename(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]", "_", value)[:128]
    return result or "artifact.bin"


def _event_payload(event: RunEvent) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": event.run_id,
        "sequence": event.sequence,
        "type": event.type.value,
        "occurred_at": event.occurred_at.isoformat(),
        "status": event.status.value,
        "partial": event.partial,
        "warning_count": event.warning_count,
        "artifact_available": event.artifact_available,
    }
    optional = {
        "error_code": event.error_code,
        "artifact_filename": event.artifact_filename,
        "artifact_media_type": event.artifact_media_type,
        "artifact_size_bytes": event.artifact_size_bytes,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    return payload


def _sse_frame(event: RunEvent) -> str:
    data = json.dumps(_event_payload(event), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.sequence}\nevent: {event.type.value}\ndata: {data}\n\n"


def _event_cursor(after: int, last_event_id: str | None) -> int:
    if last_event_id is None:
        return after
    if len(last_event_id) > 20 or not last_event_id.isdecimal():
        raise HTTPException(status_code=400, detail="Last-Event-ID must be a non-negative integer")
    header_cursor = int(last_event_id)
    return max(after, header_cursor)


def create_app(
    *,
    registry: PluginRegistry,
    provider_names: Sequence[str],
    run_executor: RunExecutor | None = None,
    settings: WebSettings | None = None,
    io_client: HttpsIoClient | None = None,
    job_manager: JobManager | None = None,
    upload_store: UploadStore | None = None,
    manage_job_lifecycle: bool = True,
    close_io_on_shutdown: bool | None = None,
) -> FastAPI:
    """Build an app from explicit registry, executor, queue, and I/O dependencies."""

    effective_settings = settings or WebSettings()
    normalized_providers = tuple(sorted({name.strip().lower() for name in provider_names}))
    if any(_PROVIDER_ID.fullmatch(name) is None for name in normalized_providers):
        raise ValueError("provider_names contains an invalid provider identifier")
    effective_io = io_client or HttpsIoClient(effective_settings.https_policy)
    effective_uploads = upload_store or InMemoryUploadStore(
        max_records=effective_settings.max_upload_records,
        max_upload_bytes=effective_settings.max_upload_bytes,
        max_total_bytes=effective_settings.max_upload_total_bytes,
        ttl_seconds=effective_settings.upload_ttl_seconds,
    )
    owns_io = io_client is None if close_io_on_shutdown is None else close_io_on_shutdown

    if job_manager is None and run_executor is None:
        raise ValueError("run_executor is required when job_manager is not supplied")

    async def execute_submission(
        payload: _AdmittedRunRequest,
        context: JobContext,
    ) -> JobExecutionResult:
        context.raise_if_cancelled()
        request = payload.request
        if isinstance(request.source, InlineJsonSource):
            raw_input = json.dumps(
                request.source.data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(raw_input) > effective_settings.https_policy.max_input_bytes:
                raise HttpsIoError("input_too_large", "Inline input exceeds the size limit")
            media_type = "application/json"
            filename = "input.json"
        elif isinstance(request.source, InlineTextSource):
            raw_input = request.source.text.encode("utf-8")
            if len(raw_input) > effective_settings.https_policy.max_input_bytes:
                raise HttpsIoError("input_too_large", "Inline input exceeds the size limit")
            media_type = request.source.media_type
            filename = request.source.filename
        elif isinstance(request.source, UploadedSource):
            uploaded = payload.uploaded_artifact
            if uploaded is None or uploaded.upload_id != request.source.upload_id:
                raise UploadNotFoundError("admitted upload snapshot is unavailable")
            if uploaded.size_bytes > effective_settings.https_policy.max_input_bytes:
                raise HttpsIoError("input_too_large", "Uploaded input exceeds the size limit")
            raw_input = uploaded.content
            media_type = uploaded.media_type
            filename = uploaded.filename
        else:
            source_url = request.source.url.get_secret_value()
            raw_input = await effective_io.fetch(source_url)
            media_type = "application/json"
            filename = _safe_filename(urlsplit(source_url).path.rsplit("/", 1)[-1])
        context.raise_if_cancelled()
        if run_executor is None:  # protected by factory validation
            raise RuntimeError("run executor is unavailable")
        resolved = ResolvedRunRequest(
            plugin_id=request.plugin_id,
            provider=request.provider,
            input_bytes=raw_input,
            input_media_type=media_type,
            input_filename=filename,
            model=request.options.model,
            enrich_web=request.options.enrich_web,
            timeout_seconds=request.options.timeout_seconds,
            action_mode=request.options.action_mode,
            parameters=dict(request.options.parameters),
        )
        result = await run_executor(resolved, context)
        if not isinstance(result, JobExecutionResult):
            raise TypeError("run_executor must return JobExecutionResult")
        context.raise_if_cancelled()
        if isinstance(request.sink, HttpsPutSink):
            await effective_io.publish(
                request.sink.url.get_secret_value(),
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
    app.state.upload_store = effective_uploads

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

    async def run_metadata(
        request: RunSubmitRequest,
        *,
        parent_run_id: str | None,
    ) -> tuple[RunMetadata, UploadedArtifact | None]:
        source = request.source
        input_sha256: str | None = None
        upload_id: str | None = None
        uploaded_artifact: UploadedArtifact | None = None
        if isinstance(source, InlineJsonSource):
            encoded = json.dumps(
                source.data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > effective_settings.https_policy.max_input_bytes:
                raise HttpsIoError("input_too_large", "Inline input exceeds the size limit")
            filename = "input.json"
            media_type = "application/json"
            input_sha256 = sha256(encoded).hexdigest()
        elif isinstance(source, InlineTextSource):
            encoded = source.text.encode("utf-8")
            if len(encoded) > effective_settings.https_policy.max_input_bytes:
                raise HttpsIoError("input_too_large", "Inline input exceeds the size limit")
            filename = source.filename
            media_type = source.media_type
            input_sha256 = sha256(encoded).hexdigest()
        elif isinstance(source, UploadedSource):
            uploaded = await effective_uploads.get(source.upload_id)
            if uploaded.size_bytes > effective_settings.https_policy.max_input_bytes:
                raise HttpsIoError("input_too_large", "Uploaded input exceeds the size limit")
            uploaded_artifact = uploaded
            filename = uploaded.filename
            media_type = uploaded.media_type
            input_sha256 = sha256(uploaded.content).hexdigest()
            upload_id = uploaded.upload_id
        else:
            source_url = source.url.get_secret_value()
            effective_io.validate_allowed_server(source_url)
            filename = _safe_filename(urlsplit(source_url).path.rsplit("/", 1)[-1])
            media_type = "application/json"
        return (
            RunMetadata(
                plugin_id=request.plugin_id,
                provider=request.provider,
                model=request.options.model,
                input_filename=filename,
                input_media_type=media_type,
                input_sha256=input_sha256,
                source_upload_id=upload_id,
                parent_run_id=parent_run_id,
            ),
            uploaded_artifact,
        )

    async def admit_and_submit(
        request: RunSubmitRequest,
        *,
        parent_run_id: str | None = None,
    ) -> RunSnapshot:
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
            metadata, uploaded_artifact = await run_metadata(
                request,
                parent_run_id=parent_run_id,
            )
            if isinstance(request.sink, HttpsPutSink):
                effective_io.validate_allowed_server(request.sink.url.get_secret_value())
            queued_payload: Any = request
            if job_manager is None:
                queued_payload = _AdmittedRunRequest(
                    request=request,
                    uploaded_artifact=uploaded_artifact,
                )
            return await manager.submit(queued_payload, metadata=metadata)
        except UploadNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Upload not found") from exc
        except HttpsIoError as exc:
            raise HTTPException(status_code=422, detail=exc.public_message) from exc
        except (JobQueueFullError, RunStoreFullError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Run capacity is exhausted",
                headers={"Retry-After": "1"},
            ) from exc

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
        "/api/v1/uploads",
        response_model=UploadResponse,
        status_code=status.HTTP_201_CREATED,
        dependencies=[api_auth],
    )
    async def upload_input(
        file: Annotated[UploadFile, File(description="One input artifact")],
    ) -> UploadResponse:
        filename = _uploaded_filename(file.filename)
        supplied_media_type = file.content_type or "application/octet-stream"
        upload_limit = min(
            effective_settings.max_upload_bytes,
            effective_settings.https_policy.max_input_bytes,
            effective_uploads.max_upload_bytes,
        )
        content = bytearray()
        try:
            while chunk := await file.read(64 * 1024):
                content.extend(chunk)
                if len(content) > upload_limit:
                    raise HTTPException(status_code=413, detail="Upload exceeds the size limit")
        finally:
            await file.close()
        try:
            media_type = _validate_media_type(supplied_media_type.strip())
            snapshot = await effective_uploads.put(
                bytes(content),
                filename=filename,
                media_type=media_type,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except UploadTooLargeError as exc:
            raise HTTPException(status_code=413, detail="Upload exceeds the size limit") from exc
        except UploadStoreFullError as exc:
            raise HTTPException(
                status_code=503,
                detail="Upload capacity is exhausted",
                headers={"Retry-After": "1"},
            ) from exc
        return _upload_response(snapshot)

    @app.post(
        "/api/v1/runs",
        response_model=RunStatusResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[api_auth],
    )
    async def submit_run(request: RunSubmitRequest) -> RunStatusResponse:
        return _status_response(await admit_and_submit(request))

    @app.get(
        "/api/v1/runs",
        response_model=RunsResponse,
        dependencies=[api_auth],
    )
    async def list_runs(
        run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
        plugin_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        provider: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        parent_run_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> RunsResponse:
        snapshots = await manager.list_runs(
            states=frozenset({run_status}) if run_status is not None else None,
            plugin_id=plugin_id,
            provider=provider,
            parent_run_id=parent_run_id,
            limit=limit,
        )
        runs = [_status_response(snapshot) for snapshot in snapshots]
        return RunsResponse(runs=runs, count=len(runs))

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
        "/api/v1/runs/{run_id}/rerun",
        response_model=RunStatusResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[api_auth],
    )
    async def rerun(run_id: str, request: RunSubmitRequest) -> RunStatusResponse:
        """Create a child run from an explicit, freshly admitted run specification."""

        try:
            parent = await manager.get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        if parent.state not in TERMINAL_STATES:
            raise HTTPException(status_code=409, detail="Only a terminal run can be rerun")
        if (
            parent.metadata.plugin_id is not None
            and request.plugin_id != parent.metadata.plugin_id
        ):
            raise HTTPException(status_code=409, detail="Rerun plugin must match parent run")
        return _status_response(await admit_and_submit(request, parent_run_id=run_id))

    @app.get(
        "/api/v1/runs/{run_id}/events",
        dependencies=[api_auth],
    )
    async def run_events(
        run_id: str,
        request: Request,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        cursor = _event_cursor(after, last_event_id)
        try:
            initial_events = await manager.get_events(run_id, after_sequence=cursor)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor
            pending = initial_events
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    if not pending:
                        try:
                            pending = await manager.wait_for_events(
                                run_id,
                                after_sequence=cursor,
                                timeout_seconds=15.0,
                            )
                        except RunNotFoundError:
                            return
                        if not pending:
                            try:
                                snapshot = await manager.get(run_id)
                            except RunNotFoundError:
                                return
                            if snapshot.state in TERMINAL_STATES:
                                return
                            yield ": keep-alive\n\n"
                            continue
                    for event in pending:
                        cursor = event.sequence
                        yield _sse_frame(event)
                    if pending[-1].type in _TERMINAL_EVENT_TYPES:
                        return
                    pending = ()
            except asyncio.CancelledError:
                raise

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

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
    "InlineTextSource",
    "PluginSchemaResponse",
    "PluginsResponse",
    "ResolvedRunRequest",
    "RunExecutor",
    "RunOptions",
    "RunSink",
    "RunSource",
    "RunStatusResponse",
    "RunSubmitRequest",
    "RunsResponse",
    "UploadedSource",
    "UploadResponse",
    "WebSettings",
    "create_app",
    "uvicorn_settings",
]
