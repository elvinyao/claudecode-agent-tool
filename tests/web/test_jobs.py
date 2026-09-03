from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from agent_core.contracts import RunStatus
from agent_core.jobs import (
    ArtifactNotReadyError,
    InMemoryJobManager,
    JobContext,
    JobExecutionResult,
    JobQueueFullError,
    RunArtifact,
    RunEventType,
    RunMetadata,
    RunNotFoundError,
    RunStoreFullError,
)


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
