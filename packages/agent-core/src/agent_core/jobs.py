"""Bounded in-memory job queue and expiring run store.

This backend is intentionally single-process. A deployment that uses multiple
Uvicorn workers must inject a durable queue/store implementation instead of
creating one manager per process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4

from agent_core.contracts import RunStatus
from agent_core.progress import ProgressEventType, ProgressSink, WorkflowProgress

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


class ArtifactIntegrityError(JobManagerError):
    """A durable artifact no longer matches its recorded digest."""


class RunEventType(str, Enum):
    """Provider-neutral lifecycle events safe to expose to a workbench UI."""

    RUN_QUEUED = "run.queued"
    RUN_STARTED = "run.started"
    RUN_CANCEL_REQUESTED = "run.cancel_requested"
    ARTIFACT_CREATED = "artifact.created"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    STEP_STARTED = ProgressEventType.STEP_STARTED.value
    STEP_COMPLETED = ProgressEventType.STEP_COMPLETED.value
    STEP_CANCELLED = ProgressEventType.STEP_CANCELLED.value
    STEP_FAILED = ProgressEventType.STEP_FAILED.value
    AGENT_BATCH_STARTED = ProgressEventType.AGENT_BATCH_STARTED.value
    AGENT_BATCH_COMPLETED = ProgressEventType.AGENT_BATCH_COMPLETED.value
    RETRY_SCHEDULED = ProgressEventType.RETRY_SCHEDULED.value
    VALIDATION_COMPLETED = ProgressEventType.VALIDATION_COMPLETED.value


@dataclass(frozen=True, slots=True)
class RunMetadata:
    """Safe, searchable run identity; raw source payloads never belong here."""

    plugin_id: str | None = None
    provider: str | None = None
    model: str | None = None
    input_filename: str | None = None
    input_media_type: str | None = None
    input_sha256: str | None = None
    source_upload_id: str | None = None
    parent_run_id: str | None = None
    plugin_version: str | None = None


@dataclass(frozen=True, slots=True)
class RunEvent:
    """One ordered event from a run's bounded replay buffer."""

    run_id: str
    sequence: int
    type: RunEventType
    occurred_at: datetime
    status: RunStatus
    partial: bool = False
    warning_count: int = 0
    error_code: str | None = None
    artifact_available: bool = False
    artifact_filename: str | None = None
    artifact_media_type: str | None = None
    artifact_size_bytes: int | None = None
    node_id: str | None = None
    node_kind: str | None = None
    node_status: str | None = None
    attempt: int | None = None
    max_attempts: int | None = None
    delay_seconds: float | None = None
    batch_size: int | None = None
    accepted_count: int | None = None
    duration_ms: float | None = None


@dataclass(frozen=True, slots=True)
class RunArtifact:
    content: bytes
    media_type: str = "application/octet-stream"
    filename: str = "artifact.bin"


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """Content metadata returned by a durable artifact store."""

    run_id: str
    media_type: str
    filename: str
    size_bytes: int
    sha256: str


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
    progress_sink: ProgressSink | None = None

    def raise_if_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise asyncio.CancelledError

    async def emit_progress(self, event: WorkflowProgress) -> None:
        if not isinstance(event, WorkflowProgress):
            raise TypeError("job progress must be a WorkflowProgress event")
        if event.run_id != self.run_id:
            raise ValueError("progress run_id does not match the job context")
        if self.progress_sink is not None:
            await self.progress_sink(event)


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
    metadata: RunMetadata = field(default_factory=RunMetadata)


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
    metadata: RunMetadata = field(default_factory=RunMetadata)
    events: list[RunEvent] = field(default_factory=list)
    event_sequence: int = 0

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
            metadata=self.metadata,
        )


JobExecutor = Callable[[Any, JobContext], Awaitable[JobExecutionResult]]
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunStore(Protocol):
    """Durable, metadata-only run history.

    Implementations must never receive or persist the submitted payload. A
    snapshot and its newly-created events are saved together so readers never
    observe a lifecycle transition without the event that describes it.
    """

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def save(
        self,
        snapshot: RunSnapshot,
        *,
        events: tuple[RunEvent, ...] = (),
    ) -> None: ...

    async def get(self, run_id: str) -> RunSnapshot: ...

    async def list_runs(
        self,
        *,
        states: frozenset[RunStatus] | None = None,
        plugin_id: str | None = None,
        provider: str | None = None,
        parent_run_id: str | None = None,
        limit: int = 50,
    ) -> tuple[RunSnapshot, ...]: ...

    async def get_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[RunEvent, ...]: ...

    async def recover_interrupted(self, *, occurred_at: datetime) -> int: ...

    async def delete_expired(
        self,
        *,
        finished_before: datetime,
    ) -> tuple[str, ...]: ...

    async def delete(self, run_id: str) -> None: ...

    async def count(self) -> int: ...


class ArtifactStore(Protocol):
    """Durable byte storage addressed by the non-secret run identifier."""

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def put(self, run_id: str, artifact: RunArtifact) -> ArtifactReference: ...

    async def get(self, run_id: str) -> RunArtifact: ...

    async def delete(self, run_id: str) -> None: ...


class JobManager(Protocol):
    """Persistence-ready boundary used by the Web composition root."""

    @property
    def started(self) -> bool: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def submit(
        self,
        payload: Any,
        *,
        metadata: RunMetadata | None = None,
    ) -> RunSnapshot: ...

    async def get(self, run_id: str) -> RunSnapshot: ...

    async def get_artifact(self, run_id: str) -> RunArtifact: ...

    async def list_runs(
        self,
        *,
        states: frozenset[RunStatus] | None = None,
        plugin_id: str | None = None,
        provider: str | None = None,
        parent_run_id: str | None = None,
        limit: int = 50,
    ) -> tuple[RunSnapshot, ...]: ...

    async def get_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[RunEvent, ...]: ...

    async def wait_for_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        timeout_seconds: float = 15.0,
    ) -> tuple[RunEvent, ...]: ...

    async def cancel(self, run_id: str) -> RunSnapshot: ...

    async def stats(self) -> dict[str, int | bool]: ...


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
        max_events_per_run: int = 256,
        clock: Clock = _utc_now,
        run_store: RunStore | None = None,
        artifact_store: ArtifactStore | None = None,
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
        if max_events_per_run <= 0:
            raise ValueError("max_events_per_run must be positive")
        self._executor = executor
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_capacity)
        self._worker_count = worker_count
        self._max_records = max_records
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_artifact_bytes = max_artifact_bytes
        self._max_events_per_run = max_events_per_run
        self._clock = clock
        self._run_store = run_store
        self._artifact_store = artifact_store
        self._records: dict[str, _RunRecord] = {}
        self._active_tasks: dict[str, asyncio.Task[JobExecutionResult]] = {}
        self._pending_artifact_deletions: set[str] = set()
        self._workers: list[asyncio.Task[None]] = []
        self._reaper: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._events_changed = asyncio.Condition(self._lock)
        self._started = False
        self._closing = False

    @property
    def started(self) -> bool:
        return self._started and not self._closing

    @property
    def queue_capacity(self) -> int:
        return self._queue.maxsize

    async def _execute_payload(self, payload: Any, context: JobContext) -> JobExecutionResult:
        return await self._executor(payload, context)

    async def start(self) -> None:
        if self._started:
            return
        initialized_stores: list[RunStore | ArtifactStore] = []
        try:
            if self._run_store is not None:
                initialized_stores.append(self._run_store)
                await self._run_store.initialize()
            if self._artifact_store is not None:
                initialized_stores.append(self._artifact_store)
                await self._artifact_store.initialize()
            if self._run_store is not None:
                await self._run_store.recover_interrupted(occurred_at=self._clock())
        except Exception:
            for store in reversed(initialized_stores):
                with suppress(Exception):
                    await store.close()
            raise
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
                    event = self._append_event_locked(record, RunEventType.RUN_CANCELLED)
                    await self._persist_record_locked(record, events=(event,))
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
        if self._artifact_store is not None:
            await self._artifact_store.close()
        if self._run_store is not None:
            await self._run_store.close()

    async def submit(
        self,
        payload: Any,
        *,
        metadata: RunMetadata | None = None,
    ) -> RunSnapshot:
        if not self.started:
            raise RuntimeError("job manager is not started")
        async with self._lock:
            await self._prune_expired_locked()
            record_count = (
                await self._run_store.count() if self._run_store is not None else len(self._records)
            )
            if record_count >= self._max_records:
                raise RunStoreFullError("run store is full")
            if self._queue.full():
                raise JobQueueFullError("job queue is full")
            run_id = uuid4().hex
            record = _RunRecord(
                run_id=run_id,
                payload=payload,
                state=RunStatus.QUEUED,
                created_at=self._clock(),
                cancel_event=asyncio.Event(),
                metadata=metadata or RunMetadata(),
            )
            self._records[run_id] = record
            event = self._append_event_locked(
                record,
                RunEventType.RUN_QUEUED,
            )
            try:
                await self._persist_record_locked(record, events=(event,))
            except Exception:
                self._records.pop(run_id, None)
                raise
            try:
                self._queue.put_nowait(run_id)
            except asyncio.QueueFull as exc:
                self._records.pop(run_id, None)
                if self._run_store is not None:
                    await self._run_store.delete(run_id)
                raise JobQueueFullError("job queue is full") from exc
            return record.snapshot()

    async def get(self, run_id: str) -> RunSnapshot:
        async with self._lock:
            await self._prune_expired_locked()
            record = self._records.get(run_id)
            if record is not None:
                return record.snapshot()
            if self._run_store is not None:
                return await self._run_store.get(run_id)
            raise RunNotFoundError("run was not found or has expired")

    async def get_artifact(self, run_id: str) -> RunArtifact:
        async with self._lock:
            await self._prune_expired_locked()
            record = self._records.get(run_id)
            if record is not None:
                snapshot = record.snapshot()
            elif self._run_store is not None:
                snapshot = await self._run_store.get(run_id)
            else:
                raise RunNotFoundError("run was not found or has expired")
            if not snapshot.artifact_available:
                raise ArtifactNotReadyError("artifact is not available")
            if self._artifact_store is not None:
                return await self._artifact_store.get(run_id)
            if record is not None and record.artifact is not None:
                return record.artifact
            raise ArtifactNotReadyError("artifact is not available in this process")

    async def list_runs(
        self,
        *,
        states: frozenset[RunStatus] | None = None,
        plugin_id: str | None = None,
        provider: str | None = None,
        parent_run_id: str | None = None,
        limit: int = 50,
    ) -> tuple[RunSnapshot, ...]:
        """Return newest runs first using only safe metadata fields."""

        if limit <= 0:
            raise ValueError("limit must be positive")
        async with self._lock:
            await self._prune_expired_locked()
            if self._run_store is not None:
                return await self._run_store.list_runs(
                    states=states,
                    plugin_id=plugin_id,
                    provider=provider,
                    parent_run_id=parent_run_id,
                    limit=limit,
                )
            records = (
                record
                for record in self._records.values()
                if (states is None or record.state in states)
                and (plugin_id is None or record.metadata.plugin_id == plugin_id)
                and (provider is None or record.metadata.provider == provider)
                and (parent_run_id is None or record.metadata.parent_run_id == parent_run_id)
            )
            ordered = sorted(
                records,
                key=lambda record: (record.created_at, record.run_id),
                reverse=True,
            )
            return tuple(record.snapshot() for record in ordered[:limit])

    async def get_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[RunEvent, ...]:
        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        async with self._lock:
            await self._prune_expired_locked()
            if self._run_store is not None:
                return await self._run_store.get_events(
                    run_id,
                    after_sequence=after_sequence,
                )
            record = self._records.get(run_id)
            if record is None:
                raise RunNotFoundError("run was not found or has expired")
            return tuple(event for event in record.events if event.sequence > after_sequence)

    async def wait_for_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        timeout_seconds: float = 15.0,
    ) -> tuple[RunEvent, ...]:
        """Wait for a replayable event, or return empty on timeout/terminal EOF."""

        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        async with self._events_changed:
            await self._prune_expired_locked()
            record = self._records.get(run_id)
            if record is None:
                if self._run_store is None:
                    raise RunNotFoundError("run was not found or has expired")
                snapshot = await self._run_store.get(run_id)
                events = await self._run_store.get_events(
                    run_id,
                    after_sequence=after_sequence,
                )
                if events or snapshot.state in TERMINAL_STATES:
                    return events
                return ()

            def ready() -> bool:
                current = self._records.get(run_id)
                return (
                    current is None
                    or any(event.sequence > after_sequence for event in current.events)
                    or current.state in TERMINAL_STATES
                )

            if not ready():
                try:
                    await asyncio.wait_for(
                        self._events_changed.wait_for(ready),
                        timeout=timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    return ()
            record = self._records.get(run_id)
            if record is None:
                if self._run_store is None:
                    raise RunNotFoundError("run was not found or has expired")
                return await self._run_store.get_events(
                    run_id,
                    after_sequence=after_sequence,
                )
            if self._run_store is not None:
                return await self._run_store.get_events(
                    run_id,
                    after_sequence=after_sequence,
                )
            return tuple(event for event in record.events if event.sequence > after_sequence)

    async def cancel(self, run_id: str) -> RunSnapshot:
        task: asyncio.Task[JobExecutionResult] | None = None
        async with self._lock:
            await self._prune_expired_locked()
            record = self._records.get(run_id)
            if record is None:
                if self._run_store is not None:
                    snapshot = await self._run_store.get(run_id)
                    if snapshot.state in TERMINAL_STATES:
                        return snapshot
                raise RunNotFoundError("run was not found or has expired")
            if record.state in TERMINAL_STATES:
                return record.snapshot()
            record.cancellation_requested = True
            record.cancel_event.set()
            events = [
                self._append_event_locked(
                    record,
                    RunEventType.RUN_CANCEL_REQUESTED,
                )
            ]
            if record.state is RunStatus.QUEUED:
                record.state = RunStatus.CANCELLED
                record.finished_at = self._clock()
                record.payload = None
                events.append(self._append_event_locked(record, RunEventType.RUN_CANCELLED))
            else:
                task = self._active_tasks.get(run_id)
            await self._persist_record_locked(record, events=tuple(events))
            snapshot = record.snapshot()
        if task is not None:
            task.cancel()
        return snapshot

    async def stats(self) -> dict[str, int | bool]:
        async with self._lock:
            await self._prune_expired_locked()
            record_count = (
                await self._run_store.count() if self._run_store is not None else len(self._records)
            )
            return {
                "started": self.started,
                "records": record_count,
                "queued": self._queue.qsize(),
                "active": len(self._active_tasks),
                "queue_capacity": self._queue.maxsize,
                "max_records": self._max_records,
            }

    async def _prune_expired_locked(self) -> None:
        now = self._clock()
        expired = {
            run_id
            for run_id, record in self._records.items()
            if record.finished_at is not None
            and record.state in TERMINAL_STATES
            and now - record.finished_at >= self._ttl
        }
        if self._run_store is not None:
            durable_expired = await self._run_store.delete_expired(
                finished_before=now - self._ttl,
            )
            expired.update(durable_expired)
        artifact_error: Exception | None = None
        if self._artifact_store is not None:
            artifact_targets = expired | self._pending_artifact_deletions
            for run_id in sorted(artifact_targets):
                try:
                    await self._artifact_store.delete(run_id)
                except Exception as exc:
                    self._pending_artifact_deletions.add(run_id)
                    artifact_error = artifact_error or exc
                else:
                    self._pending_artifact_deletions.discard(run_id)
        for run_id in expired:
            self._records.pop(run_id, None)
        if artifact_error is not None:
            raise artifact_error

    async def _persist_record_locked(
        self,
        record: _RunRecord,
        *,
        events: tuple[RunEvent, ...] = (),
    ) -> None:
        if self._run_store is not None:
            await self._run_store.save(record.snapshot(), events=events)

    def _append_event_locked(
        self,
        record: _RunRecord,
        event_type: RunEventType,
        *,
        partial: bool = False,
        warning_count: int = 0,
        error_code: str | None = None,
        artifact: RunArtifact | None = None,
        occurred_at: datetime | None = None,
        node_id: str | None = None,
        node_kind: str | None = None,
        node_status: str | None = None,
        attempt: int | None = None,
        max_attempts: int | None = None,
        delay_seconds: float | None = None,
        batch_size: int | None = None,
        accepted_count: int | None = None,
        duration_ms: float | None = None,
    ) -> RunEvent:
        available_artifact = artifact or record.artifact
        record.event_sequence += 1
        event = RunEvent(
            run_id=record.run_id,
            sequence=record.event_sequence,
            type=event_type,
            occurred_at=occurred_at or self._clock(),
            status=record.state,
            partial=partial,
            warning_count=warning_count,
            error_code=error_code,
            artifact_available=available_artifact is not None,
            artifact_filename=(
                available_artifact.filename if available_artifact is not None else None
            ),
            artifact_media_type=(
                available_artifact.media_type if available_artifact is not None else None
            ),
            artifact_size_bytes=(
                len(available_artifact.content) if available_artifact is not None else None
            ),
            node_id=node_id,
            node_kind=node_kind,
            node_status=node_status,
            attempt=attempt,
            max_attempts=max_attempts,
            delay_seconds=delay_seconds,
            batch_size=batch_size,
            accepted_count=accepted_count,
            duration_ms=duration_ms,
        )
        record.events.append(event)
        if len(record.events) > self._max_events_per_run:
            del record.events[: len(record.events) - self._max_events_per_run]
        self._events_changed.notify_all()
        return event

    async def _record_progress(self, event: WorkflowProgress) -> None:
        async with self._lock:
            record = self._records.get(event.run_id)
            if record is None or record.state in TERMINAL_STATES:
                return
            previous_event_sequence = record.event_sequence
            previous_events = list(record.events)
            run_event = self._append_event_locked(
                record,
                RunEventType(event.type.value),
                occurred_at=event.occurred_at,
                error_code=event.error_code,
                node_id=event.node_id,
                node_kind=event.node_kind,
                node_status=event.node_status,
                attempt=event.attempt,
                max_attempts=event.max_attempts,
                delay_seconds=event.delay_seconds,
                batch_size=event.batch_size,
                accepted_count=event.accepted_count,
                duration_ms=event.duration_ms,
            )
            try:
                await self._persist_record_locked(record, events=(run_event,))
            except Exception:
                record.event_sequence = previous_event_sequence
                record.events[:] = previous_events
                raise

    @staticmethod
    def _apply_result_metadata(record: _RunRecord, result: JobExecutionResult) -> None:
        """Merge only trusted identity fields, never arbitrary executor metadata."""

        updates: dict[str, str] = {}
        for field_name, max_length in (
            ("plugin_id", 64),
            ("plugin_version", 64),
            ("provider", 64),
            ("model", 128),
        ):
            value = result.metadata.get(field_name)
            if (
                isinstance(value, str)
                and value
                and value == value.strip()
                and len(value) <= max_length
            ):
                updates[field_name] = value
        if updates:
            record.metadata = replace(record.metadata, **updates)

    async def _reaper_loop(self, interval: float) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                async with self._lock:
                    await self._prune_expired_locked()
            except asyncio.CancelledError:
                raise
            except JobManagerError:
                # A transient persistence failure must not disable TTL cleanup.
                continue

    async def _worker(self, index: int) -> None:
        del index
        while True:
            run_id = await self._queue.get()
            try:
                try:
                    await self._execute(run_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Lifecycle persistence errors may already have moved the
                    # in-memory record to a safe terminal state. Keep capacity
                    # available for later jobs even if durable storage is down.
                    continue
            finally:
                self._queue.task_done()

    async def _execute(self, run_id: str) -> None:
        start_error: Exception | None = None
        async with self._lock:
            record = self._records.get(run_id)
            if record is None or record.state is RunStatus.CANCELLED:
                return
            if record.state is not RunStatus.QUEUED:
                return
            previous_started_at = record.started_at
            previous_event_sequence = record.event_sequence
            previous_events = list(record.events)
            record.state = RunStatus.RUNNING
            record.started_at = self._clock()
            event = self._append_event_locked(record, RunEventType.RUN_STARTED)
            try:
                await self._persist_record_locked(record, events=(event,))
            except Exception as exc:
                record.state = RunStatus.QUEUED
                record.started_at = previous_started_at
                record.event_sequence = previous_event_sequence
                record.events[:] = previous_events
                start_error = exc
            else:
                context = JobContext(
                    run_id=run_id,
                    cancel_event=record.cancel_event,
                    progress_sink=self._record_progress,
                )
                payload = record.payload
                record.payload = None
                task = asyncio.create_task(
                    self._execute_payload(payload, context),
                    name=f"agent-core-run-{run_id}",
                )
                self._active_tasks[run_id] = task

        if start_error is not None:
            # _mark_failed still leaves the in-memory record terminal if its
            # durable write fails; restart recovery handles the queued row.
            with suppress(Exception):
                await self._mark_failed(run_id, start_error)
            return

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
            try:
                await self._mark_succeeded(run_id, result)
            except Exception as exc:
                await self._mark_failed(run_id, exc)
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
            event = self._append_event_locked(record, RunEventType.RUN_CANCELLED)
            await self._persist_record_locked(record, events=(event,))

    async def _mark_failed(self, run_id: str, error: Exception) -> None:
        async with self._lock:
            record = self._records.get(run_id)
            if record is None or record.state in TERMINAL_STATES:
                return
            if record.cancellation_requested:
                record.state = RunStatus.CANCELLED
                record.finished_at = self._clock()
                record.payload = None
                event = self._append_event_locked(record, RunEventType.RUN_CANCELLED)
                await self._persist_record_locked(record, events=(event,))
                return
            record.state = RunStatus.FAILED
            record.finished_at = self._clock()
            record.payload = None
            record.error_code = str(getattr(error, "code", "execution_failed"))
            record.error_message = str(getattr(error, "public_message", "Run execution failed"))
            event = self._append_event_locked(
                record,
                RunEventType.RUN_FAILED,
                error_code=record.error_code,
            )
            await self._persist_record_locked(record, events=(event,))

    async def _mark_succeeded(self, run_id: str, result: JobExecutionResult) -> None:
        async with self._lock:
            record = self._records.get(run_id)
            if record is None or record.state in TERMINAL_STATES:
                return
            if record.cancellation_requested:
                record.state = RunStatus.CANCELLED
                record.finished_at = self._clock()
                record.payload = None
                event = self._append_event_locked(record, RunEventType.RUN_CANCELLED)
                await self._persist_record_locked(record, events=(event,))
                return
            if self._artifact_store is not None:
                try:
                    await self._artifact_store.put(run_id, result.artifact)
                except Exception:
                    try:
                        await self._artifact_store.delete(run_id)
                    except Exception:
                        self._pending_artifact_deletions.add(run_id)
                    raise
            previous_state = record.state
            previous_finished_at = record.finished_at
            previous_payload = record.payload
            previous_partial = record.partial
            previous_warnings = record.warnings
            previous_artifact = record.artifact
            previous_metadata = record.metadata
            previous_event_sequence = record.event_sequence
            previous_events = list(record.events)
            record.state = RunStatus.DEGRADED if result.partial else RunStatus.SUCCEEDED
            record.finished_at = self._clock()
            record.payload = None
            record.partial = result.partial
            record.warnings = result.warnings
            record.artifact = result.artifact
            self._apply_result_metadata(record, result)
            events = (
                self._append_event_locked(
                    record,
                    RunEventType.ARTIFACT_CREATED,
                    artifact=result.artifact,
                ),
                self._append_event_locked(
                    record,
                    RunEventType.RUN_COMPLETED,
                    partial=result.partial,
                    warning_count=len(result.warnings),
                ),
            )
            try:
                await self._persist_record_locked(record, events=events)
            except Exception:
                record.state = previous_state
                record.finished_at = previous_finished_at
                record.payload = previous_payload
                record.partial = previous_partial
                record.warnings = previous_warnings
                record.artifact = previous_artifact
                record.metadata = previous_metadata
                record.event_sequence = previous_event_sequence
                record.events[:] = previous_events
                if self._artifact_store is not None:
                    try:
                        await self._artifact_store.delete(run_id)
                    except Exception:
                        self._pending_artifact_deletions.add(run_id)
                raise


__all__ = [
    "ArtifactIntegrityError",
    "ArtifactNotReadyError",
    "ArtifactReference",
    "ArtifactStore",
    "ArtifactTooLargeError",
    "InMemoryJobManager",
    "JobContext",
    "JobExecutionResult",
    "JobManager",
    "JobManagerError",
    "JobQueueFullError",
    "RunArtifact",
    "RunEvent",
    "RunEventType",
    "RunMetadata",
    "RunNotFoundError",
    "RunSnapshot",
    "RunStore",
    "RunStatus",
    "RunStoreFullError",
    "TERMINAL_STATES",
]
