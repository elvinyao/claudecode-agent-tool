"""Bounded in-memory job queue and expiring run store.

This backend is intentionally single-process. A deployment that uses multiple
Uvicorn workers must inject a durable queue/store implementation instead of
creating one manager per process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from agent_core.contracts import RunStatus

TERMINAL_STATES = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.DEGRADED, RunStatus.FAILED, RunStatus.CANCELLED}
)


class JobManagerError(RuntimeError):
    """Base class for errors safe for the web adapter to classify."""


class JobQueueFullError(JobManagerError):
    pass


class RunStoreFullError(JobManagerError):
    pass


class RunNotFoundError(JobManagerError):
    pass


class ArtifactNotReadyError(JobManagerError):
    pass


class ArtifactTooLargeError(JobManagerError):
    pass


@dataclass(frozen=True, slots=True)
class RunArtifact:
    content: bytes
    media_type: str = "application/octet-stream"
    filename: str = "artifact.bin"


@dataclass(frozen=True, slots=True)
class JobExecutionResult:
    artifact: RunArtifact
    warnings: tuple[str, ...] = ()
    partial: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class JobContext:
    run_id: str
    cancel_event: asyncio.Event

    def raise_if_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise asyncio.CancelledError


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    run_id: str
    state: RunStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cancellation_requested: bool
    partial: bool
    warnings: tuple[str, ...]
    error_code: str | None
    error_message: str | None
    artifact_available: bool
    artifact_media_type: str | None
    artifact_filename: str | None


@dataclass(slots=True)
class _RunRecord:
    run_id: str
    payload: Any
    state: RunStatus
    created_at: datetime
    cancel_event: asyncio.Event
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancellation_requested: bool = False
    partial: bool = False
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    artifact: RunArtifact | None = None

    def snapshot(self) -> RunSnapshot:
        artifact = self.artifact
        return RunSnapshot(
            run_id=self.run_id,
            state=self.state,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            cancellation_requested=self.cancellation_requested,
            partial=self.partial,
            warnings=self.warnings,
            error_code=self.error_code,
            error_message=self.error_message,
            artifact_available=artifact is not None,
            artifact_media_type=artifact.media_type if artifact is not None else None,
            artifact_filename=artifact.filename if artifact is not None else None,
        )


JobExecutor = Callable[[Any, JobContext], Awaitable[JobExecutionResult]]
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryJobManager:
    """Run jobs with bounded memory, bounded concurrency, TTL, and cancellation."""

    def __init__(
        self,
        executor: JobExecutor,
        *,
        queue_capacity: int = 16,
        worker_count: int = 2,
        max_records: int = 256,
        ttl_seconds: float = 3600.0,
        max_artifact_bytes: int = 20 * 1024 * 1024,
        clock: Clock = _utc_now,
    ) -> None:
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        if worker_count <= 0:
            raise ValueError("worker_count must be positive")
        if max_records <= 0:
            raise ValueError("max_records must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_artifact_bytes <= 0:
            raise ValueError("max_artifact_bytes must be positive")
        self._executor = executor
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_capacity)
        self._worker_count = worker_count
        self._max_records = max_records
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_artifact_bytes = max_artifact_bytes
        self._clock = clock
        self._records: dict[str, _RunRecord] = {}
        self._active_tasks: dict[str, asyncio.Task[JobExecutionResult]] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._reaper: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._started = False
        self._closing = False

    @property
    def started(self) -> bool:
        return self._started and not self._closing

    @property
    def queue_capacity(self) -> int:
        return self._queue.maxsize

    async def start(self) -> None:
        if self._started:
            return
        self._closing = False
        self._started = True
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"agent-core-worker-{index}")
            for index in range(self._worker_count)
        ]
        interval = min(60.0, max(0.05, self._ttl.total_seconds() / 2))
        self._reaper = asyncio.create_task(
            self._reaper_loop(interval), name="agent-core-run-reaper"
        )

    async def close(self) -> None:
        if not self._started:
            return
        self._closing = True
        async with self._lock:
            for record in self._records.values():
                if record.state not in TERMINAL_STATES:
                    record.cancellation_requested = True
                    record.cancel_event.set()
                    record.state = RunStatus.RUNNING
                record.payload = None
            active = list(self._active_tasks.values())
        for task in active:
            task.cancel()
        for worker in self._workers:
            worker.cancel()
        if self._reaper is not None:
            self._reaper.cancel()
        await asyncio.gather(*active, *self._workers, return_exceptions=True)
        if self._reaper is not None:
            await asyncio.gather(self._reaper, return_exceptions=True)
        async with self._lock:
            now = self._clock()
            for record in self._records.values():
                if record.state not in TERMINAL_STATES:
                    record.state = RunStatus.CANCELLED
                    record.finished_at = now
            self._active_tasks.clear()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._queue.task_done()
        self._workers.clear()
        self._reaper = None
        self._started = False

    async def submit(self, payload: Any) -> RunSnapshot:
        if not self.started:
            raise RuntimeError("job manager is not started")
        async with self._lock:
            self._prune_expired_locked()
            if len(self._records) >= self._max_records:
                raise RunStoreFullError("run store is full")
            run_id = uuid4().hex
            record = _RunRecord(
                run_id=run_id,
                payload=payload,
                state=RunStatus.QUEUED,
                created_at=self._clock(),
                cancel_event=asyncio.Event(),
            )
            self._records[run_id] = record
            try:
                self._queue.put_nowait(run_id)
            except asyncio.QueueFull as exc:
                self._records.pop(run_id, None)
                raise JobQueueFullError("job queue is full") from exc
            return record.snapshot()

    async def get(self, run_id: str) -> RunSnapshot:
        async with self._lock:
            self._prune_expired_locked()
            record = self._records.get(run_id)
            if record is None:
                raise RunNotFoundError("run was not found or has expired")
            return record.snapshot()

    async def get_artifact(self, run_id: str) -> RunArtifact:
        async with self._lock:
            self._prune_expired_locked()
            record = self._records.get(run_id)
            if record is None:
                raise RunNotFoundError("run was not found or has expired")
            if record.artifact is None:
                raise ArtifactNotReadyError("artifact is not available")
            return record.artifact

    async def cancel(self, run_id: str) -> RunSnapshot:
        task: asyncio.Task[JobExecutionResult] | None = None
        async with self._lock:
            self._prune_expired_locked()
            record = self._records.get(run_id)
            if record is None:
                raise RunNotFoundError("run was not found or has expired")
            if record.state in TERMINAL_STATES:
                return record.snapshot()
            record.cancellation_requested = True
            record.cancel_event.set()
            if record.state is RunStatus.QUEUED:
                record.state = RunStatus.CANCELLED
                record.finished_at = self._clock()
                record.payload = None
            else:
                task = self._active_tasks.get(run_id)
            snapshot = record.snapshot()
        if task is not None:
            task.cancel()
        return snapshot

    async def stats(self) -> dict[str, int | bool]:
        async with self._lock:
            self._prune_expired_locked()
            return {
                "started": self.started,
                "records": len(self._records),
                "queued": self._queue.qsize(),
                "active": len(self._active_tasks),
                "queue_capacity": self._queue.maxsize,
                "max_records": self._max_records,
            }

    def _prune_expired_locked(self) -> None:
        now = self._clock()
        expired = [
            run_id
            for run_id, record in self._records.items()
            if record.finished_at is not None
            and record.state in TERMINAL_STATES
            and now - record.finished_at >= self._ttl
        ]
        for run_id in expired:
            self._records.pop(run_id, None)

    async def _reaper_loop(self, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                async with self._lock:
                    self._prune_expired_locked()
        except asyncio.CancelledError:
            raise

    async def _worker(self, index: int) -> None:
        del index
        while True:
            run_id = await self._queue.get()
            try:
                await self._execute(run_id)
            finally:
                self._queue.task_done()

    async def _execute(self, run_id: str) -> None:
        async with self._lock:
            record = self._records.get(run_id)
            if record is None or record.state is RunStatus.CANCELLED:
                return
            if record.state is not RunStatus.QUEUED:
                return
            record.state = RunStatus.RUNNING
            record.started_at = self._clock()
            context = JobContext(run_id=run_id, cancel_event=record.cancel_event)
            payload = record.payload
            record.payload = None
            task = asyncio.create_task(
                self._executor(payload, context),
                name=f"agent-core-run-{run_id}",
            )
            self._active_tasks[run_id] = task

        try:
            result = await task
            if len(result.artifact.content) > self._max_artifact_bytes:
                raise ArtifactTooLargeError("artifact exceeds configured size limit")
        except asyncio.CancelledError:
            await self._mark_cancelled(run_id)
            if self._closing:
                raise
        except Exception as exc:
            await self._mark_failed(run_id, exc)
        else:
            await self._mark_succeeded(run_id, result)
        finally:
            async with self._lock:
                self._active_tasks.pop(run_id, None)

    async def _mark_cancelled(self, run_id: str) -> None:
        async with self._lock:
            record = self._records.get(run_id)
            if record is None or record.state in TERMINAL_STATES:
                return
            record.state = RunStatus.CANCELLED
            record.cancellation_requested = True
            record.finished_at = self._clock()
            record.payload = None

    async def _mark_failed(self, run_id: str, error: Exception) -> None:
        async with self._lock:
            record = self._records.get(run_id)
            if record is None or record.state in TERMINAL_STATES:
                return
            if record.cancellation_requested:
                record.state = RunStatus.CANCELLED
                record.finished_at = self._clock()
                return
            record.state = RunStatus.FAILED
            record.finished_at = self._clock()
            record.payload = None
            record.error_code = str(getattr(error, "code", "execution_failed"))
            record.error_message = str(
                getattr(error, "public_message", "Run execution failed")
            )

    async def _mark_succeeded(self, run_id: str, result: JobExecutionResult) -> None:
        async with self._lock:
            record = self._records.get(run_id)
            if record is None or record.state in TERMINAL_STATES:
                return
            if record.cancellation_requested:
                record.state = RunStatus.CANCELLED
                record.finished_at = self._clock()
                return
            record.state = RunStatus.DEGRADED if result.partial else RunStatus.SUCCEEDED
            record.finished_at = self._clock()
            record.payload = None
            record.partial = result.partial
            record.warnings = result.warnings
            record.artifact = result.artifact


__all__ = [
    "ArtifactNotReadyError",
    "ArtifactTooLargeError",
    "InMemoryJobManager",
    "JobContext",
    "JobExecutionResult",
    "JobManagerError",
    "JobQueueFullError",
    "RunArtifact",
    "RunNotFoundError",
    "RunSnapshot",
    "RunStatus",
    "RunStoreFullError",
    "TERMINAL_STATES",
]
