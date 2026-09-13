"""Bounded, expiring upload storage for the local Web transport."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Protocol
from uuid import uuid4


class UploadStoreError(RuntimeError):
    """Base error safe for the Web adapter to classify."""

    code = "upload_store"
    public_message = "Upload storage operation failed"


class UploadNotFoundError(UploadStoreError):
    code = "upload_not_found"
    public_message = "Uploaded input is unavailable"


class UploadStoreFullError(UploadStoreError):
    code = "upload_store_full"
    public_message = "Upload capacity is exhausted"


class UploadTooLargeError(UploadStoreError):
    code = "upload_too_large"
    public_message = "Upload exceeds the size limit"


@dataclass(frozen=True, slots=True)
class UploadedArtifact:
    upload_id: str
    content: bytes
    filename: str
    media_type: str
    created_at: datetime
    expires_at: datetime
    sha256: str

    @property
    def size_bytes(self) -> int:
        return len(self.content)


@dataclass(frozen=True, slots=True)
class UploadSnapshot:
    upload_id: str
    filename: str
    media_type: str
    size_bytes: int
    created_at: datetime
    expires_at: datetime
    sha256: str


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def validate_storage_filename(filename: str) -> str:
    """Validate untrusted file metadata before it reaches a storage backend."""

    if (
        not filename
        or filename != filename.strip()
        or len(filename) > 255
        or filename in {".", ".."}
        or any(character in filename for character in ("/", "\\", "\x00", "\r", "\n"))
    ):
        raise ValueError("filename must be a safe basename of at most 255 characters")
    return filename


def validate_storage_media_type(media_type: str) -> str:
    """Validate bounded single-line media-type metadata."""

    if (
        not media_type
        or media_type != media_type.strip()
        or len(media_type) > 255
        or any(character in media_type for character in ("\x00", "\r", "\n"))
    ):
        raise ValueError("media_type must be trimmed single-line text")
    return media_type


class UploadStore(Protocol):
    """Ephemeral input storage; uploads are not part of durable run history."""

    @property
    def max_upload_bytes(self) -> int: ...

    async def put(
        self,
        content: bytes,
        *,
        filename: str,
        media_type: str,
    ) -> UploadSnapshot: ...

    async def get(self, upload_id: str) -> UploadedArtifact: ...

    async def delete(self, upload_id: str) -> None: ...

    async def stats(self) -> dict[str, int]: ...


class InMemoryUploadStore:
    """Keep uploaded inputs outside run records with explicit memory and TTL bounds."""

    def __init__(
        self,
        *,
        max_records: int = 128,
        max_upload_bytes: int = 10 * 1024 * 1024,
        max_total_bytes: int = 100 * 1024 * 1024,
        ttl_seconds: float = 3600.0,
        clock: Clock = _utc_now,
    ) -> None:
        if max_records <= 0:
            raise ValueError("max_records must be positive")
        if max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")
        if max_total_bytes <= 0:
            raise ValueError("max_total_bytes must be positive")
        if max_upload_bytes > max_total_bytes:
            raise ValueError("max_upload_bytes must not exceed max_total_bytes")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._max_records = max_records
        self._max_upload_bytes = max_upload_bytes
        self._max_total_bytes = max_total_bytes
        self._ttl = timedelta(seconds=ttl_seconds)
        self._clock = clock
        self._records: dict[str, UploadedArtifact] = {}
        self._total_bytes = 0
        self._lock = asyncio.Lock()

    @property
    def max_upload_bytes(self) -> int:
        return self._max_upload_bytes

    async def put(
        self,
        content: bytes,
        *,
        filename: str,
        media_type: str,
    ) -> UploadSnapshot:
        validate_storage_filename(filename)
        validate_storage_media_type(media_type)
        if len(content) > self._max_upload_bytes:
            raise UploadTooLargeError("upload exceeds configured size limit")
        now = self._clock()
        async with self._lock:
            self._prune_expired_locked(now)
            if len(self._records) >= self._max_records:
                raise UploadStoreFullError("upload store is full")
            if self._total_bytes + len(content) > self._max_total_bytes:
                raise UploadStoreFullError("upload byte capacity is exhausted")
            upload_id = uuid4().hex
            record = UploadedArtifact(
                upload_id=upload_id,
                content=bytes(content),
                filename=filename,
                media_type=media_type,
                created_at=now,
                expires_at=now + self._ttl,
                sha256=sha256(content).hexdigest(),
            )
            self._records[upload_id] = record
            self._total_bytes += record.size_bytes
            return self._snapshot(record)

    async def get(self, upload_id: str) -> UploadedArtifact:
        async with self._lock:
            self._prune_expired_locked(self._clock())
            record = self._records.get(upload_id)
            if record is None:
                raise UploadNotFoundError("upload was not found or has expired")
            return record

    async def delete(self, upload_id: str) -> None:
        async with self._lock:
            self._prune_expired_locked(self._clock())
            record = self._records.pop(upload_id, None)
            if record is None:
                raise UploadNotFoundError("upload was not found or has expired")
            self._total_bytes -= record.size_bytes

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            self._prune_expired_locked(self._clock())
            return {
                "records": len(self._records),
                "bytes": self._total_bytes,
                "max_records": self._max_records,
                "max_total_bytes": self._max_total_bytes,
            }

    def _prune_expired_locked(self, now: datetime) -> None:
        expired = [
            upload_id for upload_id, record in self._records.items() if now >= record.expires_at
        ]
        for upload_id in expired:
            record = self._records.pop(upload_id)
            self._total_bytes -= record.size_bytes

    @staticmethod
    def _snapshot(record: UploadedArtifact) -> UploadSnapshot:
        return UploadSnapshot(
            upload_id=record.upload_id,
            filename=record.filename,
            media_type=record.media_type,
            size_bytes=record.size_bytes,
            created_at=record.created_at,
            expires_at=record.expires_at,
            sha256=record.sha256,
        )


__all__ = [
    "InMemoryUploadStore",
    "UploadNotFoundError",
    "UploadSnapshot",
    "UploadStoreError",
    "UploadStoreFullError",
    "UploadTooLargeError",
    "UploadStore",
    "UploadedArtifact",
    "validate_storage_filename",
    "validate_storage_media_type",
]
