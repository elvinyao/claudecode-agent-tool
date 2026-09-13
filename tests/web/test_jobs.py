from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest

from agent_core.contracts import RunStatus
from agent_core.jobs import (
    TERMINAL_STATES,
    ArtifactNotReadyError,
    ArtifactReference,
    InMemoryJobManager,
    JobContext,
    JobExecutionResult,
    JobManagerError,
    JobQueueFullError,
    RunArtifact,
    RunEvent,
    RunEventType,
    RunMetadata,
    RunNotFoundError,
    RunSnapshot,
    RunStoreFullError,
)
from agent_core.progress import ProgressEventType, WorkflowProgress


class _MemoryRunStore:
    def __init__(self) -> None:
        self.snapshots: dict[str, RunSnapshot] = {}
        self.events: dict[str, list[RunEvent]] = {}
        self.initialize_calls = 0
        self.close_calls = 0
        self.recover_calls = 0

    async def initialize(self) -> None:
        self.initialize_calls += 1

    async def close(self) -> None:
        self.close_calls += 1

    async def save(
        self,
        snapshot: RunSnapshot,
        *,
        events: tuple[RunEvent, ...] = (),
    ) -> None:
        self.snapshots[snapshot.run_id] = snapshot
        existing = {event.sequence for event in self.events.get(snapshot.run_id, [])}
        self.events.setdefault(snapshot.run_id, []).extend(
            event for event in events if event.sequence not in existing
        )

    async def get(self, run_id: str) -> RunSnapshot:
        try:
            return self.snapshots[run_id]
        except KeyError as exc:
            raise RunNotFoundError("run was not found") from exc

    async def list_runs(
        self,
        *,
        states: frozenset[RunStatus] | None = None,
        plugin_id: str | None = None,
        provider: str | None = None,
        parent_run_id: str | None = None,
        limit: int = 50,
    ) -> tuple[RunSnapshot, ...]:
        snapshots = (
            snapshot
            for snapshot in self.snapshots.values()
            if (states is None or snapshot.state in states)
            and (plugin_id is None or snapshot.metadata.plugin_id == plugin_id)
            and (provider is None or snapshot.metadata.provider == provider)
            and (parent_run_id is None or snapshot.metadata.parent_run_id == parent_run_id)
        )
        return tuple(
            sorted(
                snapshots,
                key=lambda snapshot: (snapshot.created_at, snapshot.run_id),
                reverse=True,
            )[:limit]
        )

    async def get_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[RunEvent, ...]:
        if run_id not in self.snapshots:
            raise RunNotFoundError("run was not found")
        return tuple(
            event for event in self.events.get(run_id, []) if event.sequence > after_sequence
        )

    async def recover_interrupted(self, *, occurred_at: datetime) -> int:
        del occurred_at
        self.recover_calls += 1
        return 0

    async def delete_expired(
        self,
        *,
        finished_before: datetime,
    ) -> tuple[str, ...]:
        expired = tuple(
            run_id
            for run_id, snapshot in self.snapshots.items()
            if snapshot.state in TERMINAL_STATES
            and snapshot.finished_at is not None
            and snapshot.finished_at <= finished_before
        )
        for run_id in expired:
            await self.delete(run_id)
        return expired

    async def delete(self, run_id: str) -> None:
        self.snapshots.pop(run_id, None)
        self.events.pop(run_id, None)

    async def count(self) -> int:
        return len(self.snapshots)


class _MemoryArtifactStore:
    def __init__(self) -> None:
        self.artifacts: dict[str, RunArtifact] = {}
        self.initialize_calls = 0
        self.close_calls = 0
        self.delete_calls: list[str] = []

    async def initialize(self) -> None:
        self.initialize_calls += 1

    async def close(self) -> None:
        self.close_calls += 1

    async def put(self, run_id: str, artifact: RunArtifact) -> ArtifactReference:
        self.artifacts[run_id] = artifact
        return ArtifactReference(
            run_id=run_id,
            media_type=artifact.media_type,
            filename=artifact.filename,
            size_bytes=len(artifact.content),
            sha256=sha256(artifact.content).hexdigest(),
        )

    async def get(self, run_id: str) -> RunArtifact:
        try:
            return self.artifacts[run_id]
        except KeyError as exc:
            raise ArtifactNotReadyError("artifact is not available") from exc

    async def delete(self, run_id: str) -> None:
        self.delete_calls.append(run_id)
        self.artifacts.pop(run_id, None)


async def wait_for_status(
    manager: InMemoryJobManager,
    run_id: str,
    expected: set[RunStatus],
) -> RunStatus:
    for _ in range(100):
        snapshot = await manager.get(run_id)
        if snapshot.state in expected:
            return snapshot.state
        await asyncio.sleep(0)
    raise AssertionError(f"run {run_id} did not reach {expected}")


@pytest.mark.asyncio
async def test_job_context_validates_progress_before_calling_optional_sink() -> None:
    event = WorkflowProgress(
        run_id="expected-run",
        type=ProgressEventType.STEP_STARTED,
        node_id="extract",
        node_kind="transform",
    )
    without_sink = JobContext(run_id="expected-run", cancel_event=asyncio.Event())
    await without_sink.emit_progress(event)

    received: list[WorkflowProgress] = []

    async def sink(progress: WorkflowProgress) -> None:
        received.append(progress)

    with_sink = JobContext(
        run_id="expected-run",
        cancel_event=asyncio.Event(),
        progress_sink=sink,
    )
    await with_sink.emit_progress(event)
    assert received == [event]

    mismatched = event.model_copy(update={"run_id": "different-run"})
    with pytest.raises(ValueError, match="run_id"):
        await with_sink.emit_progress(mismatched)
    with pytest.raises(TypeError, match="WorkflowProgress"):
        await with_sink.emit_progress(object())  # ty: ignore[invalid-argument-type]  # Deliberately invalid event.
    assert received == [event]


@pytest.mark.asyncio
async def test_start_closes_initialized_run_store_when_artifact_init_fails() -> None:
    class FailingArtifactStore(_MemoryArtifactStore):
        async def initialize(self) -> None:
            await super().initialize()
            raise JobManagerError("injected artifact initialization failure")

    async def executor(payload: object, context: JobContext) -> JobExecutionResult:
        del payload, context
        raise AssertionError("executor must not run")

    run_store = _MemoryRunStore()
    artifact_store = FailingArtifactStore()
    manager = InMemoryJobManager(
        executor,
        run_store=run_store,
        artifact_store=artifact_store,
    )

    with pytest.raises(JobManagerError, match="artifact initialization"):
        await manager.start()

    assert manager.started is False
    assert manager._workers == []
    assert run_store.initialize_calls == 1
    assert run_store.close_calls == 1
    assert artifact_store.initialize_calls == 1
    assert artifact_store.close_calls == 1


@pytest.mark.asyncio
async def test_run_store_initialization_failure_does_not_start_other_resources() -> None:
    class FailingRunStore(_MemoryRunStore):
        async def initialize(self) -> None:
            await super().initialize()
            raise JobManagerError("injected run-store initialization failure")

    async def executor(payload: object, context: JobContext) -> JobExecutionResult:
        del payload, context
        raise AssertionError("executor must not run")

    run_store = FailingRunStore()
    artifact_store = _MemoryArtifactStore()
    manager = InMemoryJobManager(
        executor,
        run_store=run_store,
        artifact_store=artifact_store,
    )

    with pytest.raises(JobManagerError, match="run-store initialization"):
        await manager.start()

    assert manager.started is False
    assert manager._workers == []
    assert run_store.initialize_calls == 1
    assert run_store.close_calls == 1
    assert artifact_store.initialize_calls == 0


@pytest.mark.asyncio
async def test_start_closes_both_stores_when_interrupted_recovery_fails() -> None:
    class FailingRecoveryStore(_MemoryRunStore):
        async def recover_interrupted(self, *, occurred_at: datetime) -> int:
            await super().recover_interrupted(occurred_at=occurred_at)
            raise JobManagerError("injected recovery failure")

    async def executor(payload: object, context: JobContext) -> JobExecutionResult:
        del payload, context
        raise AssertionError("executor must not run")

    run_store = FailingRecoveryStore()
    artifact_store = _MemoryArtifactStore()
    manager = InMemoryJobManager(
        executor,
        run_store=run_store,
        artifact_store=artifact_store,
    )

    with pytest.raises(JobManagerError, match="recovery failure"):
        await manager.start()

    assert manager.started is False
    assert run_store.close_calls == 1
    assert artifact_store.close_calls == 1


@pytest.mark.asyncio
async def test_submit_save_failure_rolls_back_record_and_never_enqueues() -> None:
    class FailingQueuedSaveStore(_MemoryRunStore):
        async def save(
            self,
            snapshot: RunSnapshot,
            *,
            events: tuple[RunEvent, ...] = (),
        ) -> None:
            if snapshot.state is RunStatus.QUEUED:
                raise JobManagerError("injected queued save failure")
            await super().save(snapshot, events=events)

    executor_called = False

    async def executor(payload: object, context: JobContext) -> JobExecutionResult:
        del payload, context
        nonlocal executor_called
        executor_called = True
        return JobExecutionResult(artifact=RunArtifact(b"unexpected"))

    run_store = FailingQueuedSaveStore()
    manager = InMemoryJobManager(executor, worker_count=1, run_store=run_store)
    await manager.start()
    try:
        with pytest.raises(JobManagerError, match="queued save failure"):
            await manager.submit("sensitive payload")
        await asyncio.sleep(0)
        assert manager._records == {}
        assert manager._queue.qsize() == 0
        assert executor_called is False
        assert await run_store.count() == 0
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_progress_save_failure_rolls_back_event_before_failing_run() -> None:
    class FailingProgressSaveStore(_MemoryRunStore):
        failed = False

        async def save(
            self,
            snapshot: RunSnapshot,
            *,
            events: tuple[RunEvent, ...] = (),
        ) -> None:
            if any(event.type is RunEventType.STEP_STARTED for event in events) and not self.failed:
                self.failed = True
                raise JobManagerError("injected progress save failure")
            await super().save(snapshot, events=events)

    async def executor(payload: object, context: JobContext) -> JobExecutionResult:
        del payload
        await context.emit_progress(
            WorkflowProgress(
                run_id=context.run_id,
                type=ProgressEventType.STEP_STARTED,
                node_id="extract",
                node_kind="transform",
            )
        )
        return JobExecutionResult(artifact=RunArtifact(b"unexpected"))

    run_store = FailingProgressSaveStore()
    manager = InMemoryJobManager(executor, worker_count=1, run_store=run_store)
    await manager.start()
    try:
        submitted = await manager.submit("payload")
        state = await wait_for_status(manager, submitted.run_id, {RunStatus.FAILED})
        events = await manager.get_events(submitted.run_id)
    finally:
        await manager.close()

    assert state is RunStatus.FAILED
    assert [event.sequence for event in events] == [1, 2, 3]
    assert [event.type for event in events] == [
        RunEventType.RUN_QUEUED,
        RunEventType.RUN_STARTED,
        RunEventType.RUN_FAILED,
    ]


@pytest.mark.asyncio
async def test_artifact_put_failure_compensates_partial_write_and_marks_failed() -> None:
    class FailAfterWriteArtifactStore(_MemoryArtifactStore):
        async def put(self, run_id: str, artifact: RunArtifact) -> ArtifactReference:
            await super().put(run_id, artifact)
            raise JobManagerError("injected artifact put failure")

    async def executor(payload: object, context: JobContext) -> JobExecutionResult:
        del payload
        context.raise_if_cancelled()
        return JobExecutionResult(artifact=RunArtifact(b"partial durable write"))

    run_store = _MemoryRunStore()
    artifact_store = FailAfterWriteArtifactStore()
    manager = InMemoryJobManager(
        executor,
        worker_count=1,
        run_store=run_store,
        artifact_store=artifact_store,
    )
    await manager.start()
    try:
        submitted = await manager.submit("payload")
        state = await wait_for_status(manager, submitted.run_id, {RunStatus.FAILED})
        events = await manager.get_events(submitted.run_id)
    finally:
        await manager.close()

    assert state is RunStatus.FAILED
    assert submitted.run_id not in artifact_store.artifacts
    assert artifact_store.delete_calls == [submitted.run_id]
    assert [event.type for event in events] == [
        RunEventType.RUN_QUEUED,
        RunEventType.RUN_STARTED,
        RunEventType.RUN_FAILED,
    ]


@pytest.mark.asyncio
async def test_success_save_failure_removes_artifact_before_recording_failure() -> None:
    class FailOnceOnSuccessStore(_MemoryRunStore):
        failed = False

        async def save(
            self,
            snapshot: RunSnapshot,
            *,
            events: tuple[RunEvent, ...] = (),
        ) -> None:
            if snapshot.state is RunStatus.SUCCEEDED and not self.failed:
                self.failed = True
                raise JobManagerError("injected success save failure")
            await super().save(snapshot, events=events)

    async def executor(payload: object, context: JobContext) -> JobExecutionResult:
        del payload
        context.raise_if_cancelled()
        return JobExecutionResult(artifact=RunArtifact(b"must be compensated"))

    run_store = FailOnceOnSuccessStore()
    artifact_store = _MemoryArtifactStore()
    manager = InMemoryJobManager(
        executor,
        worker_count=1,
        run_store=run_store,
        artifact_store=artifact_store,
    )
    await manager.start()
    try:
        submitted = await manager.submit("payload")
        state = await wait_for_status(manager, submitted.run_id, {RunStatus.FAILED})
        events = await manager.get_events(submitted.run_id)
    finally:
        await manager.close()

    assert state is RunStatus.FAILED
    assert submitted.run_id not in artifact_store.artifacts
    assert artifact_store.delete_calls == [submitted.run_id]
    assert [event.type for event in events] == [
        RunEventType.RUN_QUEUED,
        RunEventType.RUN_STARTED,
        RunEventType.RUN_FAILED,
    ]


@pytest.mark.asyncio
async def test_reaper_retries_transient_artifact_delete_after_history_is_gone() -> None:
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)

    def clock() -> datetime:
        return now

    class FailFirstDeleteArtifactStore(_MemoryArtifactStore):
        delete_attempts = 0

        async def delete(self, run_id: str) -> None:
            self.delete_attempts += 1
            if self.delete_attempts == 1:
                raise JobManagerError("injected artifact delete failure")
            await super().delete(run_id)

    async def executor(payload: str, context: JobContext) -> JobExecutionResult:
        context.raise_if_cancelled()
        return JobExecutionResult(artifact=RunArtifact(payload.encode()))

    run_store = _MemoryRunStore()
    artifact_store = FailFirstDeleteArtifactStore()
    manager = InMemoryJobManager(
        executor,
        worker_count=1,
        ttl_seconds=0.1,
        clock=clock,
        run_store=run_store,
        artifact_store=artifact_store,
    )
    await manager.start()
    try:
        submitted = await manager.submit("artifact")
        await wait_for_status(manager, submitted.run_id, {RunStatus.SUCCEEDED})
        now += timedelta(seconds=1)

        for _ in range(100):
            if artifact_store.delete_attempts >= 2:
                break
            await asyncio.sleep(0.01)

        assert artifact_store.delete_attempts >= 2
        assert submitted.run_id not in artifact_store.artifacts
        assert submitted.run_id not in manager._pending_artifact_deletions
        assert await run_store.count() == 0
        assert manager._reaper is not None
        assert not manager._reaper.done()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_durable_count_enforces_capacity_without_local_records() -> None:
    class FullRunStore(_MemoryRunStore):
        async def count(self) -> int:
            return 1

    async def executor(payload: object, context: JobContext) -> JobExecutionResult:
        del payload, context
        raise AssertionError("executor must not run")

    manager = InMemoryJobManager(
        executor,
        worker_count=1,
        max_records=1,
        run_store=FullRunStore(),
    )
    await manager.start()
    try:
        with pytest.raises(RunStoreFullError):
            await manager.submit("payload")
        assert manager._records == {}
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_job_success_artifact_and_degraded_status() -> None:
    async def executor(payload: str, context: JobContext) -> JobExecutionResult:
        context.raise_if_cancelled()
        return JobExecutionResult(
            artifact=RunArtifact(payload.encode(), media_type="text/plain", filename="run.txt"),
            warnings=("partial evidence",),
            partial=True,
            metadata={"model": "actual-model", "raw_prompt": "do-not-store"},
        )

    manager = InMemoryJobManager(executor, worker_count=1)
    await manager.start()
    try:
        submitted = await manager.submit("result")
        state = await wait_for_status(manager, submitted.run_id, {RunStatus.DEGRADED})
        snapshot = await manager.get(submitted.run_id)
        artifact = await manager.get_artifact(submitted.run_id)
        events = await manager.get_events(submitted.run_id)
    finally:
        await manager.close()

    assert state is RunStatus.DEGRADED
    assert snapshot.partial is True
    assert snapshot.warnings == ("partial evidence",)
    assert snapshot.metadata.model == "actual-model"
    assert "do-not-store" not in repr(snapshot)
    assert artifact.content == b"result"
    assert [event.type for event in events] == [
        RunEventType.RUN_QUEUED,
        RunEventType.RUN_STARTED,
        RunEventType.ARTIFACT_CREATED,
        RunEventType.RUN_COMPLETED,
    ]
    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert events[-1].partial is True
    assert events[-1].warning_count == 1
    assert events[-1].artifact_available is True
    assert events[-1].artifact_filename == "run.txt"
    assert manager._records[submitted.run_id].payload is None


@pytest.mark.asyncio
async def test_job_progress_is_bridged_to_safe_replayable_run_events() -> None:
    occurred_at = datetime(2026, 9, 4, 12, 30, tzinfo=timezone.utc)
    sensitive_payload = "private prompt must never enter progress history"

    async def executor(payload: str, context: JobContext) -> JobExecutionResult:
        assert payload == sensitive_payload
        await context.emit_progress(
            WorkflowProgress(
                run_id=context.run_id,
                type=ProgressEventType.RETRY_SCHEDULED,
                occurred_at=occurred_at,
                node_id="generate-cards",
                node_kind="agent",
                node_status="running",
                attempt=2,
                max_attempts=3,
                delay_seconds=0.25,
                batch_size=12,
                accepted_count=7,
                duration_ms=15.5,
                error_code="provider_busy",
            )
        )
        return JobExecutionResult(artifact=RunArtifact(b"done"))

    manager = InMemoryJobManager(executor, worker_count=1)
    await manager.start()
    try:
        submitted = await manager.submit(sensitive_payload)
        await wait_for_status(manager, submitted.run_id, {RunStatus.SUCCEEDED})
        events = await manager.get_events(submitted.run_id)
    finally:
        await manager.close()

    assert [event.type for event in events] == [
        RunEventType.RUN_QUEUED,
        RunEventType.RUN_STARTED,
        RunEventType.RETRY_SCHEDULED,
        RunEventType.ARTIFACT_CREATED,
        RunEventType.RUN_COMPLETED,
    ]
    progress = events[2]
    assert progress.occurred_at == occurred_at
    assert progress.status is RunStatus.RUNNING
    assert progress.node_id == "generate-cards"
    assert progress.node_kind == "agent"
    assert progress.node_status == "running"
    assert progress.attempt == 2
    assert progress.max_attempts == 3
    assert progress.delay_seconds == 0.25
    assert progress.batch_size == 12
    assert progress.accepted_count == 7
    assert progress.duration_ms == 15.5
    assert progress.error_code == "provider_busy"
    assert sensitive_payload not in repr(events)
    assert manager._records[submitted.run_id].payload is None


@pytest.mark.asyncio
async def test_running_job_can_be_cancelled_and_releases_worker() -> None:
    started = asyncio.Event()

    async def executor(payload: str, context: JobContext) -> JobExecutionResult:
        started.set()
        await asyncio.Event().wait()
        return JobExecutionResult(artifact=RunArtifact(payload.encode()))

    manager = InMemoryJobManager(executor, worker_count=1)
    await manager.start()
    try:
        submitted = await manager.submit("never")
        await started.wait()
        cancelling = await manager.cancel(submitted.run_id)
        state = await wait_for_status(manager, submitted.run_id, {RunStatus.CANCELLED})
    finally:
        await manager.close()

    assert cancelling.cancellation_requested is True
    assert state is RunStatus.CANCELLED
    with pytest.raises(ArtifactNotReadyError):
        await manager.get_artifact(submitted.run_id)


@pytest.mark.asyncio
async def test_queue_capacity_is_bounded() -> None:
    release = asyncio.Event()
    first_started = asyncio.Event()

    async def executor(payload: str, context: JobContext) -> JobExecutionResult:
        first_started.set()
        await release.wait()
        return JobExecutionResult(artifact=RunArtifact(payload.encode()))

    manager = InMemoryJobManager(
        executor,
        worker_count=1,
        queue_capacity=1,
        max_records=10,
    )
    await manager.start()
    try:
        await manager.submit("running")
        await first_started.wait()
        await manager.submit("queued")
        with pytest.raises(JobQueueFullError):
            await manager.submit("rejected")
        release.set()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_run_store_is_bounded_until_terminal_record_expires() -> None:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)

    def clock() -> datetime:
        return now

    async def executor(payload: str, context: JobContext) -> JobExecutionResult:
        return JobExecutionResult(artifact=RunArtifact(payload.encode()))

    manager = InMemoryJobManager(
        executor,
        worker_count=1,
        max_records=1,
        ttl_seconds=60,
        clock=clock,
    )
    await manager.start()
    try:
        first = await manager.submit("first")
        await wait_for_status(manager, first.run_id, {RunStatus.SUCCEEDED})
        with pytest.raises(RunStoreFullError):
            await manager.submit("second")

        now += timedelta(seconds=61)
        second = await manager.submit("second")
        with pytest.raises(RunNotFoundError):
            await manager.get(first.run_id)
        assert second.state is RunStatus.QUEUED
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_unexpected_error_is_redacted_in_snapshot() -> None:
    async def executor(payload: str, context: JobContext) -> JobExecutionResult:
        raise RuntimeError("secret-key-should-not-leak")

    manager = InMemoryJobManager(executor, worker_count=1)
    await manager.start()
    try:
        submitted = await manager.submit("payload")
        await wait_for_status(manager, submitted.run_id, {RunStatus.FAILED})
        snapshot = await manager.get(submitted.run_id)
        events = await manager.get_events(submitted.run_id)
    finally:
        await manager.close()

    assert snapshot.error_code == "execution_failed"
    assert snapshot.error_message == "Run execution failed"
    assert "secret-key" not in snapshot.error_message
    assert events[-1].type is RunEventType.RUN_FAILED
    assert events[-1].error_code == "execution_failed"
    assert "secret-key" not in repr(events)


@pytest.mark.asyncio
async def test_list_runs_filters_safe_metadata_newest_first() -> None:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)

    def clock() -> datetime:
        return now

    release = asyncio.Event()

    async def executor(payload: str, context: JobContext) -> JobExecutionResult:
        await release.wait()
        return JobExecutionResult(artifact=RunArtifact(payload.encode()))

    manager = InMemoryJobManager(executor, worker_count=1, clock=clock)
    await manager.start()
    try:
        first = await manager.submit(
            "first",
            metadata=RunMetadata(plugin_id="demo", provider="codex"),
        )
        now += timedelta(seconds=1)
        second = await manager.submit(
            "second",
            metadata=RunMetadata(
                plugin_id="demo",
                provider="claude",
                parent_run_id=first.run_id,
            ),
        )
        all_runs = await manager.list_runs()
        claude_runs = await manager.list_runs(provider="claude")
        child_runs = await manager.list_runs(parent_run_id=first.run_id)
        release.set()
    finally:
        await manager.close()

    assert [item.run_id for item in all_runs] == [second.run_id, first.run_id]
    assert [item.run_id for item in claude_runs] == [second.run_id]
    assert [item.run_id for item in child_runs] == [second.run_id]


@pytest.mark.asyncio
async def test_wait_for_events_supports_cursor_and_terminal_eof() -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    async def executor(payload: str, context: JobContext) -> JobExecutionResult:
        started.set()
        await release.wait()
        return JobExecutionResult(artifact=RunArtifact(payload.encode()))

    manager = InMemoryJobManager(executor, worker_count=1)
    await manager.start()
    try:
        submitted = await manager.submit("value")
        await started.wait()
        before = await manager.get_events(submitted.run_id)
        waiter = asyncio.create_task(
            manager.wait_for_events(
                submitted.run_id,
                after_sequence=before[-1].sequence,
                timeout_seconds=1,
            )
        )
        await asyncio.sleep(0)
        release.set()
        new_events = await waiter
        eof = await manager.wait_for_events(
            submitted.run_id,
            after_sequence=new_events[-1].sequence,
            timeout_seconds=1,
        )
    finally:
        await manager.close()

    assert [event.type for event in new_events] == [
        RunEventType.ARTIFACT_CREATED,
        RunEventType.RUN_COMPLETED,
    ]
    assert eof == ()
