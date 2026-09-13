from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_core.contracts import RunStatus
from agent_core.jobs import (
    ArtifactIntegrityError,
    ArtifactNotReadyError,
    InMemoryJobManager,
    JobContext,
    JobExecutionResult,
    RunArtifact,
    RunEvent,
    RunEventType,
    RunMetadata,
    RunNotFoundError,
    RunSnapshot,
    RunStoreFullError,
)
from agent_core.persistence import (
    FilesystemArtifactStore,
    PersistenceError,
    SQLiteRunStore,
)


async def _wait_for_terminal(
    manager: InMemoryJobManager,
    run_id: str,
) -> RunSnapshot:
    for _ in range(100):
        snapshot = await manager.get(run_id)
        if snapshot.state in {
            RunStatus.SUCCEEDED,
            RunStatus.DEGRADED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return snapshot
        await asyncio.sleep(0)
    raise AssertionError("run did not become terminal")


@pytest.mark.asyncio
async def test_durable_manager_reopens_terminal_run_events_and_artifact(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history" / "runs.sqlite3"
    artifact_root = tmp_path / "artifacts"
    sensitive_payload = "https://input.example/private?token=do-not-persist"

    async def executor(payload: str, context: JobContext) -> JobExecutionResult:
        assert payload == sensitive_payload
        context.raise_if_cancelled()
        return JobExecutionResult(
            artifact=RunArtifact(
                b"durable result",
                media_type="text/plain",
                filename="result.txt",
            ),
            metadata={"plugin_version": "2.4.1"},
        )

    first = InMemoryJobManager(
        executor,
        worker_count=1,
        run_store=SQLiteRunStore(database_path),
        artifact_store=FilesystemArtifactStore(artifact_root),
    )
    await first.start()
    submitted = await first.submit(
        sensitive_payload,
        metadata=RunMetadata(
            plugin_id="demo",
            plugin_version="2.4.0",
            provider="codex",
            model="gpt-test",
            input_filename="input.json",
            input_media_type="application/json",
            input_sha256="a" * 64,
        ),
    )
    terminal = await _wait_for_terminal(first, submitted.run_id)
    await first.close()

    executor_called = False

    async def must_not_execute(payload: object, context: JobContext) -> JobExecutionResult:
        del payload, context
        nonlocal executor_called
        executor_called = True
        raise AssertionError("terminal history must not be re-executed")

    reopened = InMemoryJobManager(
        must_not_execute,
        worker_count=1,
        run_store=SQLiteRunStore(database_path),
        artifact_store=FilesystemArtifactStore(artifact_root),
    )
    await reopened.start()
    try:
        snapshot = await reopened.get(submitted.run_id)
        events = await reopened.get_events(submitted.run_id)
        artifact = await reopened.get_artifact(submitted.run_id)
        listed = await reopened.list_runs(plugin_id="demo")
    finally:
        await reopened.close()

    assert terminal.state is RunStatus.SUCCEEDED
    assert snapshot.state is RunStatus.SUCCEEDED
    assert snapshot.metadata.plugin_version == "2.4.1"
    assert [event.type for event in events] == [
        RunEventType.RUN_QUEUED,
        RunEventType.RUN_STARTED,
        RunEventType.ARTIFACT_CREATED,
        RunEventType.RUN_COMPLETED,
    ]
    assert artifact.content == b"durable result"
    assert [item.run_id for item in listed] == [submitted.run_id]
    assert executor_called is False
    persisted_database = database_path.read_bytes()
    assert sensitive_payload.encode() not in persisted_database
    assert b"do-not-persist" not in persisted_database
    assert not list(artifact_root.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_startup_marks_queued_and_running_history_as_worker_interrupted(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.initialize()
    for index, state in enumerate((RunStatus.QUEUED, RunStatus.RUNNING), start=1):
        run_id = f"interrupted-{index}"
        event_type = (
            RunEventType.RUN_QUEUED if state is RunStatus.QUEUED else RunEventType.RUN_STARTED
        )
        snapshot = RunSnapshot(
            run_id=run_id,
            state=state,
            created_at=now,
            started_at=now if state is RunStatus.RUNNING else None,
            finished_at=None,
            cancellation_requested=False,
            partial=False,
            warnings=(),
            error_code=None,
            error_message=None,
            artifact_available=False,
            artifact_media_type=None,
            artifact_filename=None,
            metadata=RunMetadata(plugin_id="demo", plugin_version="1.0.0"),
        )
        await store.save(
            snapshot,
            events=(
                RunEvent(
                    run_id=run_id,
                    sequence=1,
                    type=event_type,
                    occurred_at=now,
                    status=state,
                ),
            ),
        )

    async def executor(payload: object, context: JobContext) -> JobExecutionResult:
        del payload, context
        raise AssertionError("recovered history must not execute")

    manager = InMemoryJobManager(
        executor,
        worker_count=1,
        run_store=SQLiteRunStore(tmp_path / "runs.sqlite3"),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        clock=lambda: now,
    )
    await manager.start()
    try:
        for index in (1, 2):
            run_id = f"interrupted-{index}"
            snapshot = await manager.get(run_id)
            events = await manager.get_events(run_id)
            assert snapshot.state is RunStatus.FAILED
            assert snapshot.finished_at == now
            assert snapshot.error_code == "worker_interrupted"
            assert events[-1].sequence == 2
            assert events[-1].type is RunEventType.RUN_FAILED
            assert events[-1].error_code == "worker_interrupted"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_filesystem_artifact_store_verifies_hash_and_rejects_path_escape(
    tmp_path: Path,
) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    reference = await store.put(
        "safe-run",
        RunArtifact(b"trusted", media_type="text/plain", filename="result.txt"),
    )

    assert reference.sha256
    assert (await store.get("safe-run")).content == b"trusted"

    deleted_reference = await store.put(
        "delete-run",
        RunArtifact(b"remove me", media_type="text/plain", filename="delete.txt"),
    )
    deleted_blob = store.path_for("delete-run")
    deleted_manifest = tmp_path / "artifacts" / "refs" / "delete-run.json"
    assert deleted_reference.sha256 in deleted_blob.name
    assert deleted_blob.is_file()
    assert deleted_manifest.is_file()
    await store.delete("delete-run")
    await store.delete("delete-run")
    assert not deleted_blob.exists()
    assert not deleted_manifest.exists()

    store.path_for("safe-run").write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        await store.get("safe-run")
    with pytest.raises(ValueError, match="run_id"):
        await store.put("../escape", RunArtifact(b"unsafe"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("manifest_patch", "error_match"),
    [
        ({"schema_version": 2}, "identity"),
        ({"run_id": "different-run"}, "identity"),
        ({"filename": "../escape.txt"}, "metadata"),
        ({"media_type": "text/plain\nsecret"}, "metadata"),
        ({"size_bytes": -1}, "digest"),
        ({"sha256": "not-a-sha256"}, "digest"),
        ({"blob": "../../outside.blob"}, "digest"),
    ],
)
async def test_filesystem_artifact_store_rejects_unsafe_manifest_fields(
    tmp_path: Path,
    manifest_patch: dict[str, object],
    error_match: str,
) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    await store.put(
        "manifest-run",
        RunArtifact(b"trusted", media_type="text/plain", filename="result.txt"),
    )
    manifest_path = tmp_path / "artifacts" / "refs" / "manifest-run.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(manifest_patch)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactIntegrityError, match=error_match):
        await store.get_reference("manifest-run")


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded_manifest", [b"{", b"[]"])
async def test_filesystem_artifact_store_rejects_malformed_manifest(
    tmp_path: Path,
    encoded_manifest: bytes,
) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    await store.put("malformed-run", RunArtifact(b"trusted"))
    manifest_path = tmp_path / "artifacts" / "refs" / "malformed-run.json"
    manifest_path.write_bytes(encoded_manifest)

    with pytest.raises(ArtifactIntegrityError, match="reference is invalid"):
        await store.get_reference("malformed-run")


@pytest.mark.asyncio
async def test_filesystem_artifact_store_rejects_missing_and_symlinked_files(
    tmp_path: Path,
) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    await store.initialize()
    with pytest.raises(ArtifactNotReadyError, match="not available"):
        await store.get_reference("missing-run")

    await store.put("missing-blob", RunArtifact(b"trusted"))
    store.path_for("missing-blob").unlink()
    with pytest.raises(ArtifactIntegrityError, match="content file is unavailable"):
        await store.get("missing-blob")

    await store.put("symlink-ref", RunArtifact(b"trusted"))
    reference_path = tmp_path / "artifacts" / "refs" / "symlink-ref.json"
    outside = tmp_path / "outside.json"
    outside.write_text(reference_path.read_text(encoding="utf-8"), encoding="utf-8")
    reference_path.unlink()
    reference_path.symlink_to(outside)
    with pytest.raises(ArtifactIntegrityError, match="symbolic link"):
        await store.get_reference("symlink-ref")


@pytest.mark.asyncio
async def test_sqlite_store_uses_wal_and_rejects_future_schema_version(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runs.sqlite3"
    store = SQLiteRunStore(database_path)
    await store.initialize()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        event_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(run_events)").fetchall()
        }
        assert {
            "node_id",
            "node_kind",
            "node_status",
            "attempt",
            "max_attempts",
            "delay_seconds",
            "batch_size",
            "accepted_count",
            "duration_ms",
        } <= event_columns

    future_path = tmp_path / "future.sqlite3"
    with sqlite3.connect(future_path) as connection:
        connection.execute("PRAGMA user_version = 999")

    with pytest.raises(PersistenceError, match="newer unsupported schema"):
        await SQLiteRunStore(future_path).initialize()
    with sqlite3.connect(future_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_sqlite_store_round_trips_progress_event_fields(tmp_path: Path) -> None:
    now = datetime(2026, 9, 4, 12, 30, tzinfo=timezone.utc)
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    snapshot = RunSnapshot(
        run_id="progress-round-trip",
        state=RunStatus.RUNNING,
        created_at=now,
        started_at=now,
        finished_at=None,
        cancellation_requested=False,
        partial=False,
        warnings=(),
        error_code=None,
        error_message=None,
        artifact_available=False,
        artifact_media_type=None,
        artifact_filename=None,
    )
    progress = RunEvent(
        run_id=snapshot.run_id,
        sequence=1,
        type=RunEventType.RETRY_SCHEDULED,
        occurred_at=now,
        status=RunStatus.RUNNING,
        error_code="provider_busy",
        node_id="generate-cards",
        node_kind="agent",
        node_status="running",
        attempt=2,
        max_attempts=3,
        delay_seconds=0.25,
        batch_size=12,
        accepted_count=7,
        duration_ms=15.5,
    )

    await store.save(snapshot, events=(progress,))

    assert await store.get_events(snapshot.run_id) == (progress,)
    assert await store.get_events(snapshot.run_id, after_sequence=1) == ()


@pytest.mark.asyncio
async def test_sqlite_store_migrates_v1_event_table_without_losing_base_columns(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "v1.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE run_events (
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                status TEXT NOT NULL,
                partial INTEGER NOT NULL,
                warning_count INTEGER NOT NULL,
                error_code TEXT,
                artifact_available INTEGER NOT NULL,
                artifact_filename TEXT,
                artifact_media_type TEXT,
                artifact_size_bytes INTEGER,
                PRIMARY KEY (run_id, sequence)
            );
            PRAGMA user_version = 1;
            """
        )

    await SQLiteRunStore(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        event_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(run_events)").fetchall()
        }
    assert {
        "run_id",
        "sequence",
        "artifact_size_bytes",
        "node_id",
        "node_kind",
        "node_status",
        "attempt",
        "max_attempts",
        "delay_seconds",
        "batch_size",
        "accepted_count",
        "duration_ms",
    } <= event_columns


@pytest.mark.asyncio
async def test_sqlite_store_bounds_events_without_reusing_sequence_numbers(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    store = SQLiteRunStore(tmp_path / "runs.sqlite3", max_events_per_run=2)
    snapshot = RunSnapshot(
        run_id="bounded-events",
        state=RunStatus.RUNNING,
        created_at=now,
        started_at=now,
        finished_at=None,
        cancellation_requested=False,
        partial=False,
        warnings=(),
        error_code=None,
        error_message=None,
        artifact_available=False,
        artifact_media_type=None,
        artifact_filename=None,
    )
    await store.save(
        snapshot,
        events=tuple(
            RunEvent(
                run_id=snapshot.run_id,
                sequence=sequence,
                type=RunEventType.RUN_STARTED,
                occurred_at=now,
                status=RunStatus.RUNNING,
            )
            for sequence in range(1, 6)
        ),
    )

    assert [event.sequence for event in await store.get_events(snapshot.run_id)] == [
        4,
        5,
    ]
    assert await store.recover_interrupted(occurred_at=now) == 1
    recovered = await store.get_events(snapshot.run_id)
    assert [event.sequence for event in recovered] == [5, 6]
    assert recovered[-1].type is RunEventType.RUN_FAILED


@pytest.mark.asyncio
async def test_sqlite_save_rolls_back_snapshot_when_event_insert_fails(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    database_path = tmp_path / "runs.sqlite3"
    store = SQLiteRunStore(database_path)
    await store.initialize()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_test_event
            BEFORE INSERT ON run_events
            BEGIN
                SELECT RAISE(ABORT, 'injected event write failure');
            END
            """
        )

    snapshot = RunSnapshot(
        run_id="rollback-run",
        state=RunStatus.RUNNING,
        created_at=now,
        started_at=now,
        finished_at=None,
        cancellation_requested=False,
        partial=False,
        warnings=(),
        error_code=None,
        error_message=None,
        artifact_available=False,
        artifact_media_type=None,
        artifact_filename=None,
    )
    event = RunEvent(
        run_id=snapshot.run_id,
        sequence=1,
        type=RunEventType.RUN_STARTED,
        occurred_at=now,
        status=RunStatus.RUNNING,
    )

    with pytest.raises(PersistenceError, match="unable to save run history"):
        await store.save(snapshot, events=(event,))

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 0
        connection.execute("DROP TRIGGER reject_test_event")

    await store.save(snapshot, events=(event,))
    assert await store.count() == 1
    assert await store.get_events(snapshot.run_id) == (event,)


@pytest.mark.asyncio
async def test_sqlite_reads_fail_closed_for_corrupt_snapshot_and_event(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    database_path = tmp_path / "runs.sqlite3"
    store = SQLiteRunStore(database_path)
    snapshot = RunSnapshot(
        run_id="corrupt-row",
        state=RunStatus.RUNNING,
        created_at=now,
        started_at=now,
        finished_at=None,
        cancellation_requested=False,
        partial=False,
        warnings=(),
        error_code=None,
        error_message=None,
        artifact_available=False,
        artifact_media_type=None,
        artifact_filename=None,
    )
    event = RunEvent(
        run_id=snapshot.run_id,
        sequence=1,
        type=RunEventType.STEP_STARTED,
        occurred_at=now,
        status=RunStatus.RUNNING,
        node_id="generate",
        node_kind="agent",
        node_status="running",
    )
    await store.save(snapshot, events=(event,))

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE runs SET warnings_json = ? WHERE run_id = ?",
            ("{}", snapshot.run_id),
        )
    with pytest.raises(PersistenceError, match="stored run snapshot is invalid"):
        await store.get(snapshot.run_id)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE runs SET warnings_json = '[]' WHERE run_id = ?",
            (snapshot.run_id,),
        )
        connection.execute(
            "UPDATE run_events SET node_kind = 'shell' WHERE run_id = ?",
            (snapshot.run_id,),
        )
    assert await store.get(snapshot.run_id) == snapshot
    with pytest.raises(PersistenceError, match="stored run event is invalid"):
        await store.get_events(snapshot.run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "error_match"),
    [
        ({"run_id": "other-run"}, "does not match"),
        ({"sequence": 0}, "sequence must be positive"),
        ({"warning_count": -1}, "warning_count"),
        ({"artifact_available": True}, "require filename and media type"),
        ({"node_kind": "shell"}, "node_kind is invalid"),
        ({"node_status": "unknown"}, "node_status is invalid"),
        ({"attempt": 0}, "attempt is outside"),
        ({"delay_seconds": float("nan")}, "delay_seconds is outside"),
        ({"delay_seconds": float("inf")}, "delay_seconds is outside"),
        ({"duration_ms": float("nan")}, "duration_ms is outside"),
        ({"duration_ms": float("inf")}, "duration_ms is outside"),
    ],
)
async def test_sqlite_store_rejects_invalid_event_metadata(
    tmp_path: Path,
    changes: dict[str, object],
    error_match: str,
) -> None:
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    snapshot = RunSnapshot(
        run_id="invalid-event",
        state=RunStatus.RUNNING,
        created_at=now,
        started_at=now,
        finished_at=None,
        cancellation_requested=False,
        partial=False,
        warnings=(),
        error_code=None,
        error_message=None,
        artifact_available=False,
        artifact_media_type=None,
        artifact_filename=None,
    )
    event = replace(
        RunEvent(
            run_id=snapshot.run_id,
            sequence=1,
            type=RunEventType.STEP_STARTED,
            occurred_at=now,
            status=RunStatus.RUNNING,
            node_id="generate",
            node_kind="agent",
            node_status="running",
        ),
        **changes,
    )

    with pytest.raises(ValueError, match=error_match):
        await store.save(snapshot, events=(event,))
    assert await store.count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "error_match"),
    [
        ({"created_at": datetime(2026, 9, 4)}, "timezone-aware"),
        (
            {"metadata": RunMetadata(provider="https://secret.example")},
            "must not contain a URL",
        ),
        ({"artifact_available": True}, "require filename and media type"),
        (
            {"artifact_filename": "result.txt", "artifact_media_type": "text/plain"},
            "must not include artifact metadata",
        ),
    ],
)
async def test_sqlite_store_rejects_invalid_snapshot_metadata(
    tmp_path: Path,
    changes: dict[str, object],
    error_match: str,
) -> None:
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    snapshot = replace(
        RunSnapshot(
            run_id="invalid-snapshot",
            state=RunStatus.RUNNING,
            created_at=now,
            started_at=now,
            finished_at=None,
            cancellation_requested=False,
            partial=False,
            warnings=(),
            error_code=None,
            error_message=None,
            artifact_available=False,
            artifact_media_type=None,
            artifact_filename=None,
        ),
        **changes,
    )

    with pytest.raises(ValueError, match=error_match):
        await store.save(snapshot)
    assert await store.count() == 0


@pytest.mark.asyncio
async def test_durable_ttl_prunes_history_and_artifacts_and_frees_capacity(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    database_path = tmp_path / "runs.sqlite3"
    artifact_root = tmp_path / "artifacts"

    async def executor(payload: str, context: JobContext) -> JobExecutionResult:
        context.raise_if_cancelled()
        return JobExecutionResult(
            artifact=RunArtifact(
                payload.encode(),
                media_type="text/plain",
                filename="result.txt",
            )
        )

    first_artifacts = FilesystemArtifactStore(artifact_root)
    first = InMemoryJobManager(
        executor,
        worker_count=1,
        max_records=1,
        ttl_seconds=60,
        clock=lambda: now,
        run_store=SQLiteRunStore(database_path),
        artifact_store=first_artifacts,
    )
    await first.start()
    original = await first.submit("first")
    await _wait_for_terminal(first, original.run_id)
    original_blob = first_artifacts.path_for(original.run_id)
    await first.close()

    second_artifacts = FilesystemArtifactStore(artifact_root)
    reopened = InMemoryJobManager(
        executor,
        worker_count=1,
        max_records=1,
        ttl_seconds=60,
        clock=lambda: now,
        run_store=SQLiteRunStore(database_path),
        artifact_store=second_artifacts,
    )
    await reopened.start()
    try:
        with pytest.raises(RunStoreFullError):
            await reopened.submit("too-soon")

        now = now.replace(minute=now.minute + 2)
        replacement = await reopened.submit("replacement")
        with pytest.raises(RunNotFoundError):
            await reopened.get(original.run_id)
        with pytest.raises(ArtifactNotReadyError):
            await second_artifacts.get(original.run_id)
        assert not original_blob.exists()
        assert not (artifact_root / "refs" / f"{original.run_id}.json").exists()
        await _wait_for_terminal(reopened, replacement.run_id)
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_success_persistence_failure_rolls_back_before_marking_failed(
    tmp_path: Path,
) -> None:
    class FailOnceOnSuccessStore(SQLiteRunStore):
        failed = False

        async def save(
            self,
            snapshot: RunSnapshot,
            *,
            events: tuple[RunEvent, ...] = (),
        ) -> None:
            if snapshot.state is RunStatus.SUCCEEDED and not self.failed:
                self.failed = True
                raise PersistenceError("injected terminal write failure")
            await super().save(snapshot, events=events)

    async def executor(payload: str, context: JobContext) -> JobExecutionResult:
        del payload
        context.raise_if_cancelled()
        return JobExecutionResult(
            artifact=RunArtifact(
                b"must be removed",
                media_type="text/plain",
                filename="result.txt",
            )
        )

    database_path = tmp_path / "runs.sqlite3"
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    manager = InMemoryJobManager(
        executor,
        worker_count=1,
        run_store=FailOnceOnSuccessStore(database_path),
        artifact_store=artifact_store,
    )
    await manager.start()
    try:
        submitted = await manager.submit("payload")
        terminal = await _wait_for_terminal(manager, submitted.run_id)
        events = await manager.get_events(submitted.run_id)

        assert terminal.state is RunStatus.FAILED
        assert manager._records[submitted.run_id].state is RunStatus.FAILED
        assert [event.type for event in events] == [
            RunEventType.RUN_QUEUED,
            RunEventType.RUN_STARTED,
            RunEventType.RUN_FAILED,
        ]
        with pytest.raises(ArtifactNotReadyError):
            await artifact_store.get(submitted.run_id)
    finally:
        await manager.close()

    reopened = SQLiteRunStore(database_path)
    assert (await reopened.get(submitted.run_id)).state is RunStatus.FAILED


@pytest.mark.asyncio
async def test_started_persistence_failure_fails_run_without_killing_worker(
    tmp_path: Path,
) -> None:
    class FailOnceOnStartedStore(SQLiteRunStore):
        failed = False

        async def save(
            self,
            snapshot: RunSnapshot,
            *,
            events: tuple[RunEvent, ...] = (),
        ) -> None:
            if snapshot.state is RunStatus.RUNNING and not self.failed:
                self.failed = True
                raise PersistenceError("injected started write failure")
            await super().save(snapshot, events=events)

    async def executor(payload: str, context: JobContext) -> JobExecutionResult:
        context.raise_if_cancelled()
        return JobExecutionResult(artifact=RunArtifact(payload.encode()))

    store = FailOnceOnStartedStore(tmp_path / "runs.sqlite3")
    manager = InMemoryJobManager(executor, worker_count=1, run_store=store)
    await manager.start()
    try:
        first = await manager.submit("first")
        first_terminal = await _wait_for_terminal(manager, first.run_id)
        second = await manager.submit("second")
        second_terminal = await _wait_for_terminal(manager, second.run_id)

        assert first_terminal.state is RunStatus.FAILED
        assert [event.type for event in await manager.get_events(first.run_id)] == [
            RunEventType.RUN_QUEUED,
            RunEventType.RUN_FAILED,
        ]
        assert second_terminal.state is RunStatus.SUCCEEDED
        assert all(not worker.done() for worker in manager._workers)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_transient_prune_failure_does_not_stop_reaper(tmp_path: Path) -> None:
    class FailOnceOnPruneStore(SQLiteRunStore):
        armed = False
        attempts = 0

        async def delete_expired(
            self,
            *,
            finished_before: datetime,
        ) -> tuple[str, ...]:
            if self.armed:
                self.attempts += 1
                if self.attempts == 1:
                    raise PersistenceError("injected prune failure")
            return await super().delete_expired(finished_before=finished_before)

    async def executor(payload: str, context: JobContext) -> JobExecutionResult:
        context.raise_if_cancelled()
        return JobExecutionResult(artifact=RunArtifact(payload.encode()))

    store = FailOnceOnPruneStore(tmp_path / "runs.sqlite3")
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    manager = InMemoryJobManager(
        executor,
        worker_count=1,
        ttl_seconds=0.1,
        run_store=store,
        artifact_store=artifacts,
    )
    await manager.start()
    try:
        submitted = await manager.submit("result")
        await _wait_for_terminal(manager, submitted.run_id)
        artifact_path = artifacts.path_for(submitted.run_id)
        store.armed = True
        for _ in range(100):
            if store.attempts >= 2 and await store.count() == 0:
                break
            await asyncio.sleep(0.01)

        assert store.attempts >= 2
        assert await store.count() == 0
        assert manager._reaper is not None
        assert not manager._reaper.done()
        assert not artifact_path.exists()
    finally:
        await manager.close()
