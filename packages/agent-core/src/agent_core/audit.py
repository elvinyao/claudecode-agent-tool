"""Metadata-only audit records with SHA-256 payload fingerprints."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Any, Protocol, TypeAlias, runtime_checkable
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_core.contracts import AgentCoreError, RunStatus, StrictFrozenModel

AuditScalar: TypeAlias = str | int | float | bool | None
_AUDIT_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RAW_METADATA_KEYS = {
    "body",
    "content",
    "input",
    "output",
    "payload",
    "prompt",
    "raw",
    "request",
    "response",
    "text",
}


class AuditError(AgentCoreError):
    """Base class for audit contract and sink failures."""


class AuditPayloadError(AuditError, TypeError):
    """A payload cannot be deterministically fingerprinted."""


class AuditMetadataError(AuditError, ValueError):
    """Metadata is invalid or appears to contain raw request/response content."""


class AuditEventType(str, Enum):
    """Stable event names emitted across workflow and transport boundaries."""

    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    NODE_STARTED = "node_started"
    NODE_COMPLETED = "node_completed"
    PROVIDER_REQUESTED = "provider_requested"
    PROVIDER_COMPLETED = "provider_completed"
    ACTION_DECIDED = "action_decided"
    PLUGIN_CREATED = "plugin_created"


class AuditMetadataItem(StrictFrozenModel):
    """One bounded scalar metadata field; raw content is deliberately unsupported."""

    key: str
    value: AuditScalar

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if _AUDIT_KEY.fullmatch(value) is None:
            raise ValueError("audit metadata keys must be lowercase dotted identifiers")
        leaf = value.rsplit(".", 1)[-1]
        if leaf in _RAW_METADATA_KEYS:
            raise ValueError(f"raw-content metadata key is forbidden: {value!r}")
        return value

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: AuditScalar) -> AuditScalar:
        if isinstance(value, str) and len(value) > 512:
            raise ValueError("audit metadata strings must not exceed 512 characters")
        return value


class AuditRecord(StrictFrozenModel):
    """An immutable audit envelope that never contains the fingerprinted payload."""

    # Audit records are a persistence format.  JSON-native strings/lists must
    # round-trip back into enums, datetimes and tuples without weakening the
    # strict contracts used at provider/workflow boundaries.
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str
    event_type: AuditEventType
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    subject_id: str | None = None
    status: RunStatus | None = None
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    payload_size_bytes: int = Field(ge=0)
    metadata: tuple[AuditMetadataItem, ...]

    @field_validator("event_id", "run_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if _IDENTIFIER.fullmatch(value) is None:
            raise ValueError("audit identifiers must use the framework identifier syntax")
        return value

    @field_validator("subject_id")
    @classmethod
    def validate_subject_id(cls, value: str | None) -> str | None:
        if value is not None and _IDENTIFIER.fullmatch(value) is None:
            raise ValueError("subject_id must use the framework identifier syntax")
        return value

    @field_validator("metadata")
    @classmethod
    def validate_unique_metadata(
        cls, value: tuple[AuditMetadataItem, ...]
    ) -> tuple[AuditMetadataItem, ...]:
        keys = [item.key for item in value]
        if len(set(keys)) != len(keys):
            raise ValueError("audit metadata keys must be unique")
        return value

    @property
    def metadata_dict(self) -> dict[str, AuditScalar]:
        """Return a copy suitable for JSON transports."""

        return {item.key: item.value for item in self.metadata}


@runtime_checkable
class AuditSink(Protocol):
    """Synchronous sink boundary; sinks receive redacted records only."""

    def emit(self, record: AuditRecord) -> None: ...


class InMemoryAuditSink:
    """Thread-safe bounded-process sink useful as the safe default and in tests."""

    def __init__(self, *, max_records: int = 10_000) -> None:
        if max_records < 1:
            raise ValueError("max_records must be positive")
        self._records: deque[AuditRecord] = deque(maxlen=max_records)
        self._lock = Lock()

    def emit(self, record: AuditRecord) -> None:
        with self._lock:
            self._records.append(record)

    @property
    def records(self) -> tuple[AuditRecord, ...]:
        with self._lock:
            return tuple(self._records)


def default_audit_path(
    *,
    env: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
    platform: str | None = None,
) -> Path:
    """Return the per-user audit log path without third-party path helpers.

    ``env`` and ``home`` are injectable so callers and tests do not need to
    mutate process-global environment or home-directory state.
    """

    environment = os.environ if env is None else env
    home_path = Path.home() if home is None else Path(home)
    current_platform = sys.platform if platform is None else platform

    if current_platform == "win32":
        local_app_data = _absolute_environment_path(environment.get("LOCALAPPDATA"))
        if local_app_data is not None:
            return local_app_data / "agent_core" / "audit.jsonl"
        return home_path / "AppData" / "Local" / "agent_core" / "audit.jsonl"

    xdg_state_home = _absolute_environment_path(environment.get("XDG_STATE_HOME"))
    if xdg_state_home is not None:
        return xdg_state_home / "agent_core" / "audit.jsonl"

    if current_platform == "darwin":
        return home_path / "Library" / "Application Support" / "agent_core" / "audit.jsonl"

    return home_path / ".agent_core" / "audit.jsonl"


class JsonlAuditSink:
    """Durable, metadata-only JSON Lines sink for a single process.

    Each append is protected by a process-local lock and is flushed and
    fsynced before :meth:`emit` returns. The file is reopened for every append
    so the sink does not retain descriptors across forks or application
    lifecycle transitions.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = default_audit_path() if path is None else Path(path)
        self._lock = Lock()
        with self._lock:
            self._ensure_storage()

    def emit(self, record: AuditRecord) -> None:
        if not isinstance(record, AuditRecord):
            raise TypeError("JsonlAuditSink only accepts AuditRecord instances")

        persisted_record = _without_url_queries(record)
        encoded = (persisted_record.model_dump_json() + "\n").encode("utf-8")
        with self._lock:
            self._ensure_storage()
            self._append(encoded)

    def _ensure_storage(self) -> None:
        parent = self.path.parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if parent.is_symlink():
                raise AuditError("audit directory must not be a symbolic link")
            _chmod_private(parent, 0o700)

            descriptor = _open_private_append(self.path)
            try:
                _restrict_open_file(descriptor)
            finally:
                os.close(descriptor)
        except AuditError:
            raise
        except OSError as exc:
            raise AuditError("unable to prepare audit storage") from exc

    def _append(self, encoded: bytes) -> None:
        descriptor = -1
        try:
            descriptor = _open_private_append(self.path)
            _restrict_open_file(descriptor)
            with os.fdopen(
                descriptor,
                mode="a",
                encoding="utf-8",
                newline="\n",
                closefd=True,
            ) as stream:
                descriptor = -1
                stream.write(encoded.decode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
        except AuditError:
            raise
        except OSError as exc:
            raise AuditError("unable to append audit record") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)


class AuditLogger:
    """Create metadata-only records and emit them to a configured sink."""

    def __init__(self, sink: AuditSink | None = None) -> None:
        self.sink = sink or InMemoryAuditSink()

    def record(
        self,
        *,
        run_id: str,
        event_type: AuditEventType,
        payload: Any = None,
        subject_id: str | None = None,
        status: RunStatus | None = None,
        metadata: Mapping[str, AuditScalar] | None = None,
        occurred_at: datetime | None = None,
    ) -> AuditRecord:
        """Fingerprint a payload while retaining only bounded scalar metadata."""

        payload_bytes, payload_type = _canonical_payload(payload)
        supplied = {} if metadata is None else dict(metadata)
        defaults: dict[str, AuditScalar] = {
            "audit.hash_algorithm": "sha256",
            "audit.payload_type": payload_type,
        }
        overlap = set(defaults) & set(supplied)
        if overlap:
            raise AuditMetadataError(
                "reserved audit metadata cannot be overridden: " + ", ".join(sorted(overlap))
            )
        combined = {**defaults, **supplied}
        try:
            metadata_items = tuple(
                AuditMetadataItem(key=key, value=value)
                for key, value in sorted(combined.items())
            )
        except (TypeError, ValueError) as exc:
            raise AuditMetadataError(str(exc)) from exc

        record = AuditRecord(
            run_id=run_id,
            event_type=event_type,
            occurred_at=occurred_at or datetime.now(timezone.utc),
            subject_id=subject_id,
            status=status,
            payload_sha256=hashlib.sha256(payload_bytes).hexdigest(),
            payload_size_bytes=len(payload_bytes),
            metadata=metadata_items,
        )
        self.sink.emit(record)
        return record


def _absolute_environment_path(value: str | None) -> Path | None:
    if not value:
        return None
    candidate = Path(value)
    return candidate if candidate.is_absolute() else None


def _open_private_append(path: Path) -> int:
    try:
        if path.is_symlink():
            raise AuditError("audit file must not be a symbolic link")
    except OSError as exc:
        raise AuditError("unable to inspect audit file") from exc

    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise AuditError("unable to open audit file") from exc

    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise AuditError("audit path must identify a regular file")
    return descriptor


def _restrict_open_file(descriptor: int) -> None:
    try:
        os.fchmod(descriptor, 0o600)
    except AttributeError:
        # Windows only interprets the writable bit; creation still uses the
        # most restrictive portable mode and user ACLs remain authoritative.
        return


def _chmod_private(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except NotImplementedError:
        return


def _without_url_queries(record: AuditRecord) -> AuditRecord:
    metadata = tuple(
        item.model_copy(update={"value": _strip_url_query(item.value)})
        if isinstance(item.value, str)
        else item
        for item in record.metadata
    )
    if metadata == record.metadata:
        return record
    return record.model_copy(update={"metadata": metadata})


def _strip_url_query(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return value
    host = parsed.hostname
    if host is None:
        return value
    rendered_host = f"[{host}]" if ":" in host else host
    try:
        port = parsed.port
    except ValueError:
        return value
    netloc = rendered_host if port is None else f"{rendered_host}:{port}"
    sanitized = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    return sanitized if sanitized != value else value


def _canonical_payload(payload: Any) -> tuple[bytes, str]:
    if payload is None:
        return b"", "none"
    if isinstance(payload, bytes):
        return payload, "bytes"
    if isinstance(payload, bytearray):
        return bytes(payload), "bytes"
    if isinstance(payload, str):
        return payload.encode("utf-8"), "text"
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
        payload_type = "pydantic"
    elif isinstance(payload, Mapping):
        payload = dict(payload)
        payload_type = "mapping"
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        payload = list(payload)
        payload_type = "sequence"
    else:
        raise AuditPayloadError(
            "audit payloads must be bytes, text, mappings, sequences, Pydantic models, or None"
        )
    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AuditPayloadError("audit payload is not canonical JSON data") from exc
    return canonical, payload_type


__all__ = [
    "AuditError",
    "AuditEventType",
    "AuditLogger",
    "AuditMetadataError",
    "AuditMetadataItem",
    "AuditPayloadError",
    "AuditRecord",
    "AuditScalar",
    "AuditSink",
    "InMemoryAuditSink",
    "JsonlAuditSink",
    "default_audit_path",
]
