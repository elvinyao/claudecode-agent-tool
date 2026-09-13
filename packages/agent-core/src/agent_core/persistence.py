"""Local durable run history backed by SQLite and atomic artifact files.

Only explicit, transport-safe run metadata is written to SQLite. Submitted
payloads, input bodies, prompts, and source/sink URLs are deliberately absent
from this module's storage contracts and schema.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from contextlib import suppress
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from agent_core.contracts import RunStatus
from agent_core.jobs import (
    TERMINAL_STATES,
    ArtifactIntegrityError,
    ArtifactNotReadyError,
    ArtifactReference,
    ArtifactTooLargeError,
    JobManagerError,
    RunArtifact,
    RunEvent,
    RunEventType,
    RunMetadata,
    RunNotFoundError,
    RunSnapshot,
)
from agent_core.uploads import validate_storage_filename, validate_storage_media_type

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_INTERRUPTED_MESSAGE = "Run execution was interrupted before completion"
_RUN_STORE_SCHEMA_VERSION = 2
_ARTIFACT_SCHEMA_VERSION = 1


class PersistenceError(JobManagerError):
    """A local durable-history operation failed."""


class SQLiteRunStore:
    """SQLite implementation of the metadata-only :class:`RunStore` boundary.

    A fresh connection is used for every operation, which keeps the instance
    safe to call through ``asyncio.to_thread`` and makes reopen semantics
    explicit. Snapshot updates and their lifecycle events share one transaction.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        busy_timeout_seconds: float = 5.0,
        max_events_per_run: int = 256,
    ) -> None:
        if busy_timeout_seconds <= 0:
            raise ValueError("busy_timeout_seconds must be positive")
        if max_events_per_run <= 0:
            raise ValueError("max_events_per_run must be positive")
        self.path = Path(path)
        if not self.path.name:
            raise ValueError("SQLite path must name a database file")
        self._busy_timeout_seconds = busy_timeout_seconds
        self._max_events_per_run = max_events_per_run
        self._initialize_lock = Lock()
        self._initialized = False

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def close(self) -> None:
        """No-op: operations do not retain SQLite connections."""

    async def save(
        self,
        snapshot: RunSnapshot,
        *,
        events: tuple[RunEvent, ...] = (),
    ) -> None:
        await asyncio.to_thread(self._save_sync, snapshot, events)

    async def get(self, run_id: str) -> RunSnapshot:
        return await asyncio.to_thread(self._get_sync, run_id)

    async def list_runs(
        self,
        *,
        states: frozenset[RunStatus] | None = None,
        plugin_id: str | None = None,
        provider: str | None = None,
        parent_run_id: str | None = None,
        limit: int = 50,
    ) -> tuple[RunSnapshot, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if states is not None and not states:
            return ()
        return await asyncio.to_thread(
            self._list_runs_sync,
            states,
            plugin_id,
            provider,
            parent_run_id,
            limit,
        )

    async def get_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[RunEvent, ...]:
        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        return await asyncio.to_thread(self._get_events_sync, run_id, after_sequence)

    async def recover_interrupted(self, *, occurred_at: datetime) -> int:
        return await asyncio.to_thread(self._recover_interrupted_sync, occurred_at)

    async def delete_expired(
        self,
        *,
        finished_before: datetime,
    ) -> tuple[str, ...]:
        return await asyncio.to_thread(self._delete_expired_sync, finished_before)

    async def delete(self, run_id: str) -> None:
        await asyncio.to_thread(self._delete_sync, run_id)

    async def count(self) -> int:
        return await asyncio.to_thread(self._count_sync)

    def _initialize_sync(self) -> None:
        with self._initialize_lock:
            if self._initialized:
                return
            try:
                parent = self.path.parent
                parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if parent.is_symlink():
                    raise PersistenceError("run-history directory must not be a symbolic link")
                os.chmod(parent, 0o700)
                if self.path.is_symlink():
                    raise PersistenceError("run-history database must not be a symbolic link")
                connection = self._open_connection()
                try:
                    schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if schema_version > _RUN_STORE_SCHEMA_VERSION:
                        raise PersistenceError(
                            "SQLite run history uses a newer unsupported schema version"
                        )
                    connection.execute("PRAGMA journal_mode = WAL")
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS runs (
                            run_id TEXT PRIMARY KEY,
                            state TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            started_at TEXT,
                            finished_at TEXT,
                            cancellation_requested INTEGER NOT NULL,
                            partial INTEGER NOT NULL,
                            warnings_json TEXT NOT NULL,
                            error_code TEXT,
                            error_message TEXT,
                            artifact_available INTEGER NOT NULL,
                            artifact_media_type TEXT,
                            artifact_filename TEXT,
                            plugin_id TEXT,
                            plugin_version TEXT,
                            provider TEXT,
                            model TEXT,
                            input_filename TEXT,
                            input_media_type TEXT,
                            input_sha256 TEXT,
                            source_upload_id TEXT,
                            parent_run_id TEXT,
                            last_event_sequence INTEGER NOT NULL DEFAULT 0
                        );

                        CREATE TABLE IF NOT EXISTS run_events (
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
                            node_id TEXT,
                            node_kind TEXT,
                            node_status TEXT,
                            attempt INTEGER,
                            max_attempts INTEGER,
                            delay_seconds REAL,
                            batch_size INTEGER,
                            accepted_count INTEGER,
                            duration_ms REAL,
                            PRIMARY KEY (run_id, sequence),
                            FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                        );

                        CREATE INDEX IF NOT EXISTS runs_created_at_idx
                        ON runs(created_at DESC, run_id DESC);
                        CREATE INDEX IF NOT EXISTS runs_plugin_id_idx
                        ON runs(plugin_id, created_at DESC);
                        CREATE INDEX IF NOT EXISTS runs_provider_idx
                        ON runs(provider, created_at DESC);
                        CREATE INDEX IF NOT EXISTS runs_parent_run_id_idx
                        ON runs(parent_run_id, created_at DESC);
                        """
                    )
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        self._migrate_event_columns_sync(connection)
                        run_ids = connection.execute("SELECT run_id FROM runs").fetchall()
                        for row in run_ids:
                            self._prune_events_sync(connection, str(row["run_id"]))
                        if schema_version < _RUN_STORE_SCHEMA_VERSION:
                            connection.execute(f"PRAGMA user_version = {_RUN_STORE_SCHEMA_VERSION}")
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                finally:
                    connection.close()
                if self.path.exists():
                    if self.path.is_symlink():
                        raise PersistenceError("run-history database must not be a symbolic link")
                    os.chmod(self.path, 0o600)
            except PersistenceError:
                raise
            except (OSError, sqlite3.Error) as exc:
                raise PersistenceError("unable to initialize SQLite run history") from exc
            self._initialized = True

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=self._busy_timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_seconds * 1000)}")
        return connection

    def _connection(self) -> sqlite3.Connection:
        self._initialize_sync()
        return self._open_connection()

    def _save_sync(
        self,
        snapshot: RunSnapshot,
        events: tuple[RunEvent, ...],
    ) -> None:
        _validate_snapshot(snapshot)
        for event in events:
            _validate_event(event, expected_run_id=snapshot.run_id)
        last_sequence = max((event.sequence for event in events), default=0)
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, state, created_at, started_at, finished_at,
                    cancellation_requested, partial, warnings_json,
                    error_code, error_message, artifact_available,
                    artifact_media_type, artifact_filename, plugin_id,
                    plugin_version, provider, model, input_filename,
                    input_media_type, input_sha256, source_upload_id,
                    parent_run_id, last_event_sequence
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(run_id) DO UPDATE SET
                    state = excluded.state,
                    created_at = excluded.created_at,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    cancellation_requested = excluded.cancellation_requested,
                    partial = excluded.partial,
                    warnings_json = excluded.warnings_json,
                    error_code = excluded.error_code,
                    error_message = excluded.error_message,
                    artifact_available = excluded.artifact_available,
                    artifact_media_type = excluded.artifact_media_type,
                    artifact_filename = excluded.artifact_filename,
                    plugin_id = excluded.plugin_id,
                    plugin_version = excluded.plugin_version,
                    provider = excluded.provider,
                    model = excluded.model,
                    input_filename = excluded.input_filename,
                    input_media_type = excluded.input_media_type,
                    input_sha256 = excluded.input_sha256,
                    source_upload_id = excluded.source_upload_id,
                    parent_run_id = excluded.parent_run_id,
                    last_event_sequence = MAX(
                        runs.last_event_sequence,
                        excluded.last_event_sequence
                    )
                """,
                _snapshot_values(snapshot, last_sequence=last_sequence),
            )
            for event in events:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO run_events (
                        run_id, sequence, event_type, occurred_at, status,
                        partial, warning_count, error_code, artifact_available,
                        artifact_filename, artifact_media_type, artifact_size_bytes,
                        node_id, node_kind, node_status, attempt, max_attempts,
                        delay_seconds, batch_size, accepted_count, duration_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _event_values(event),
                )
            if events:
                self._prune_events_sync(connection, snapshot.run_id)
            connection.commit()
        except (OSError, sqlite3.Error) as exc:
            connection.rollback()
            raise PersistenceError("unable to save run history") from exc
        finally:
            connection.close()

    def _get_sync(self, run_id: str) -> RunSnapshot:
        _validate_run_id(run_id)
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise PersistenceError("unable to read run history") from exc
        finally:
            connection.close()
        if row is None:
            raise RunNotFoundError("run was not found")
        return _snapshot_from_row(row)

    def _list_runs_sync(
        self,
        states: frozenset[RunStatus] | None,
        plugin_id: str | None,
        provider: str | None,
        parent_run_id: str | None,
        limit: int,
    ) -> tuple[RunSnapshot, ...]:
        clauses: list[str] = []
        parameters: list[str | int] = []
        if states is not None:
            ordered_states = sorted(state.value for state in states)
            placeholders = ", ".join("?" for _ in ordered_states)
            clauses.append(f"state IN ({placeholders})")
            parameters.extend(ordered_states)
        for column, value in (
            ("plugin_id", plugin_id),
            ("provider", provider),
            ("parent_run_id", parent_run_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        connection = self._connection()
        try:
            rows = connection.execute(
                f"SELECT * FROM runs{where} "  # noqa: S608 - fixed column names only
                "ORDER BY created_at DESC, run_id DESC LIMIT ?",
                parameters,
            ).fetchall()
        except sqlite3.Error as exc:
            raise PersistenceError("unable to list run history") from exc
        finally:
            connection.close()
        return tuple(_snapshot_from_row(row) for row in rows)

    def _get_events_sync(
        self,
        run_id: str,
        after_sequence: int,
    ) -> tuple[RunEvent, ...]:
        _validate_run_id(run_id)
        connection = self._connection()
        try:
            exists = connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if exists is None:
                raise RunNotFoundError("run was not found")
            rows = connection.execute(
                """
                SELECT * FROM run_events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (run_id, after_sequence),
            ).fetchall()
        except RunNotFoundError:
            raise
        except sqlite3.Error as exc:
            raise PersistenceError("unable to read run events") from exc
        finally:
            connection.close()
        return tuple(_event_from_row(row) for row in rows)

    def _recover_interrupted_sync(self, occurred_at: datetime) -> int:
        encoded_time = _encode_datetime(occurred_at)
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT run_id, last_event_sequence
                FROM runs
                WHERE state IN (?, ?)
                ORDER BY created_at ASC, run_id ASC
                """,
                (RunStatus.QUEUED.value, RunStatus.RUNNING.value),
            ).fetchall()
            for row in rows:
                run_id = str(row["run_id"])
                maximum = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM run_events WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
                sequence = max(int(row["last_event_sequence"]), int(maximum)) + 1
                connection.execute(
                    """
                    UPDATE runs SET
                        state = ?, finished_at = ?, partial = 0,
                        warnings_json = '[]', error_code = ?, error_message = ?,
                        artifact_available = 0, artifact_media_type = NULL,
                        artifact_filename = NULL, last_event_sequence = ?
                    WHERE run_id = ?
                    """,
                    (
                        RunStatus.FAILED.value,
                        encoded_time,
                        "worker_interrupted",
                        _INTERRUPTED_MESSAGE,
                        sequence,
                        run_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO run_events (
                        run_id, sequence, event_type, occurred_at, status,
                        partial, warning_count, error_code, artifact_available,
                        artifact_filename, artifact_media_type, artifact_size_bytes
                    ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, 0, NULL, NULL, NULL)
                    """,
                    (
                        run_id,
                        sequence,
                        RunEventType.RUN_FAILED.value,
                        encoded_time,
                        RunStatus.FAILED.value,
                        "worker_interrupted",
                    ),
                )
                self._prune_events_sync(connection, run_id)
            connection.commit()
            return len(rows)
        except (OSError, sqlite3.Error) as exc:
            connection.rollback()
            raise PersistenceError("unable to recover interrupted runs") from exc
        finally:
            connection.close()

    def _delete_expired_sync(self, finished_before: datetime) -> tuple[str, ...]:
        encoded_cutoff = _encode_datetime(finished_before)
        terminal_states = tuple(sorted(state.value for state in TERMINAL_STATES))
        placeholders = ", ".join("?" for _ in terminal_states)
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"""
                SELECT run_id FROM runs
                WHERE state IN ({placeholders})
                  AND finished_at IS NOT NULL
                  AND finished_at <= ?
                ORDER BY finished_at ASC, run_id ASC
                """,  # noqa: S608 - placeholders are generated, not user-controlled
                (*terminal_states, encoded_cutoff),
            ).fetchall()
            run_ids = tuple(str(row["run_id"]) for row in rows)
            connection.executemany(
                "DELETE FROM runs WHERE run_id = ?",
                ((run_id,) for run_id in run_ids),
            )
            connection.commit()
            return run_ids
        except (OSError, sqlite3.Error) as exc:
            connection.rollback()
            raise PersistenceError("unable to prune expired run history") from exc
        finally:
            connection.close()

    def _delete_sync(self, run_id: str) -> None:
        _validate_run_id(run_id)
        connection = self._connection()
        try:
            connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        except sqlite3.Error as exc:
            raise PersistenceError("unable to delete run history") from exc
        finally:
            connection.close()

    def _prune_events_sync(
        self,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> None:
        connection.execute(
            """
            DELETE FROM run_events
            WHERE run_id = ?
              AND sequence NOT IN (
                  SELECT sequence FROM run_events
                  WHERE run_id = ?
                  ORDER BY sequence DESC
                  LIMIT ?
              )
            """,
            (run_id, run_id, self._max_events_per_run),
        )

    @staticmethod
    def _migrate_event_columns_sync(connection: sqlite3.Connection) -> None:
        existing = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(run_events)").fetchall()
        }
        definitions = {
            "node_id": "TEXT",
            "node_kind": "TEXT",
            "node_status": "TEXT",
            "attempt": "INTEGER",
            "max_attempts": "INTEGER",
            "delay_seconds": "REAL",
            "batch_size": "INTEGER",
            "accepted_count": "INTEGER",
            "duration_ms": "REAL",
        }
        for name, definition in definitions.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE run_events ADD COLUMN {name} {definition}")

    def _count_sync(self) -> int:
        connection = self._connection()
        try:
            return int(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        except sqlite3.Error as exc:
            raise PersistenceError("unable to count run history") from exc
        finally:
            connection.close()


class FilesystemArtifactStore:
    """Atomic artifact storage with a SHA-256-verified reference manifest."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_artifact_bytes: int | None = None,
    ) -> None:
        if max_artifact_bytes is not None and max_artifact_bytes <= 0:
            raise ValueError("max_artifact_bytes must be positive")
        self.root = Path(root)
        self._blob_root = self.root / "blobs"
        self._reference_root = self.root / "refs"
        self._max_artifact_bytes = max_artifact_bytes
        self._initialize_lock = Lock()
        self._initialized = False

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def close(self) -> None:
        """No-op: operations do not retain file descriptors."""

    async def put(self, run_id: str, artifact: RunArtifact) -> ArtifactReference:
        return await asyncio.to_thread(self._put_sync, run_id, artifact)

    async def get(self, run_id: str) -> RunArtifact:
        return await asyncio.to_thread(self._get_sync, run_id)

    async def get_reference(self, run_id: str) -> ArtifactReference:
        return await asyncio.to_thread(self._get_reference_sync, run_id)

    async def delete(self, run_id: str) -> None:
        await asyncio.to_thread(self._delete_sync, run_id)

    def path_for(self, run_id: str) -> Path:
        """Return the current content path, primarily for local administration."""

        reference, blob_name = self._load_manifest_sync(run_id)
        del reference
        return self._blob_root / blob_name

    def _initialize_sync(self) -> None:
        with self._initialize_lock:
            if self._initialized:
                return
            try:
                for directory in (self.root, self._blob_root, self._reference_root):
                    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if directory.is_symlink():
                        raise PersistenceError(
                            "artifact storage directories must not be symbolic links"
                        )
                    os.chmod(directory, 0o700)
            except PersistenceError:
                raise
            except OSError as exc:
                raise PersistenceError("unable to initialize artifact storage") from exc
            self._initialized = True

    def _put_sync(self, run_id: str, artifact: RunArtifact) -> ArtifactReference:
        _validate_run_id(run_id)
        if not isinstance(artifact, RunArtifact):
            raise TypeError("artifact store only accepts RunArtifact instances")
        if not isinstance(artifact.content, bytes):
            raise TypeError("artifact content must be bytes")
        validate_storage_filename(artifact.filename)
        validate_storage_media_type(artifact.media_type)
        if (
            self._max_artifact_bytes is not None
            and len(artifact.content) > self._max_artifact_bytes
        ):
            raise ArtifactTooLargeError("artifact exceeds configured size limit")
        self._initialize_sync()
        digest = sha256(artifact.content).hexdigest()
        blob_name = f"{run_id}.{digest}.blob"
        reference = ArtifactReference(
            run_id=run_id,
            media_type=artifact.media_type,
            filename=artifact.filename,
            size_bytes=len(artifact.content),
            sha256=digest,
        )
        manifest = {
            "schema_version": _ARTIFACT_SCHEMA_VERSION,
            "run_id": run_id,
            "filename": artifact.filename,
            "media_type": artifact.media_type,
            "size_bytes": len(artifact.content),
            "sha256": digest,
            "blob": blob_name,
        }
        encoded_manifest = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            _atomic_write(self._blob_root / blob_name, artifact.content)
            _atomic_write(self._reference_path(run_id), encoded_manifest)
        except OSError as exc:
            raise PersistenceError("unable to write artifact") from exc
        return reference

    def _get_sync(self, run_id: str) -> RunArtifact:
        reference, blob_name = self._load_manifest_sync(run_id)
        blob_path = self._blob_root / blob_name
        try:
            if blob_path.is_symlink() or not blob_path.is_file():
                raise ArtifactIntegrityError("artifact content file is unavailable")
            content = blob_path.read_bytes()
        except ArtifactIntegrityError:
            raise
        except OSError as exc:
            raise PersistenceError("unable to read artifact") from exc
        if len(content) != reference.size_bytes or sha256(content).hexdigest() != reference.sha256:
            raise ArtifactIntegrityError("artifact content failed SHA-256 verification")
        return RunArtifact(
            content=content,
            media_type=reference.media_type,
            filename=reference.filename,
        )

    def _get_reference_sync(self, run_id: str) -> ArtifactReference:
        reference, _ = self._load_manifest_sync(run_id)
        return reference

    def _load_manifest_sync(self, run_id: str) -> tuple[ArtifactReference, str]:
        _validate_run_id(run_id)
        self._initialize_sync()
        path = self._reference_path(run_id)
        try:
            if path.is_symlink():
                raise ArtifactIntegrityError("artifact reference must not be a symbolic link")
            encoded = path.read_bytes()
        except FileNotFoundError as exc:
            raise ArtifactNotReadyError("artifact is not available") from exc
        except ArtifactIntegrityError:
            raise
        except OSError as exc:
            raise PersistenceError("unable to read artifact reference") from exc
        try:
            manifest = json.loads(encoded)
            if not isinstance(manifest, dict):
                raise ValueError
            reference = ArtifactReference(
                run_id=str(manifest["run_id"]),
                media_type=str(manifest["media_type"]),
                filename=str(manifest["filename"]),
                size_bytes=int(manifest["size_bytes"]),
                sha256=str(manifest["sha256"]),
            )
            blob_name = str(manifest["blob"])
            schema_version = int(manifest["schema_version"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ArtifactIntegrityError("artifact reference is invalid") from exc
        if schema_version != _ARTIFACT_SCHEMA_VERSION or reference.run_id != run_id:
            raise ArtifactIntegrityError("artifact reference identity is invalid")
        try:
            validate_storage_filename(reference.filename)
            validate_storage_media_type(reference.media_type)
        except ValueError as exc:
            raise ArtifactIntegrityError("artifact reference metadata is invalid") from exc
        if (
            reference.size_bytes < 0
            or _SHA256.fullmatch(reference.sha256) is None
            or blob_name != f"{run_id}.{reference.sha256}.blob"
        ):
            raise ArtifactIntegrityError("artifact reference digest is invalid")
        return reference, blob_name

    def _delete_sync(self, run_id: str) -> None:
        _validate_run_id(run_id)
        self._initialize_sync()
        reference_path = self._reference_path(run_id)
        try:
            _, blob_name = self._load_manifest_sync(run_id)
        except ArtifactNotReadyError:
            return
        blob_path = self._blob_root / blob_name
        try:
            blob_path.unlink(missing_ok=True)
            _fsync_directory(self._blob_root)
            reference_path.unlink(missing_ok=True)
            _fsync_directory(self._reference_root)
        except OSError as exc:
            raise PersistenceError("unable to delete artifact") from exc

    def _reference_path(self, run_id: str) -> Path:
        return self._reference_root / f"{run_id}.json"


def _validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run_id must be a safe framework identifier")
    return run_id


def _validate_snapshot(snapshot: RunSnapshot) -> None:
    if not isinstance(snapshot, RunSnapshot):
        raise TypeError("run store only accepts RunSnapshot instances")
    _validate_run_id(snapshot.run_id)
    _encode_datetime(snapshot.created_at)
    if snapshot.started_at is not None:
        _encode_datetime(snapshot.started_at)
    if snapshot.finished_at is not None:
        _encode_datetime(snapshot.finished_at)
    metadata = snapshot.metadata
    for name, value, maximum in (
        ("plugin_id", metadata.plugin_id, 64),
        ("plugin_version", metadata.plugin_version, 64),
        ("provider", metadata.provider, 64),
        ("model", metadata.model, 128),
    ):
        _validate_optional_text(name, value, maximum=maximum, reject_url=True)
    if metadata.input_filename is not None:
        validate_storage_filename(metadata.input_filename)
    if metadata.input_media_type is not None:
        validate_storage_media_type(metadata.input_media_type)
    if metadata.input_sha256 is not None and _SHA256.fullmatch(metadata.input_sha256) is None:
        raise ValueError("input_sha256 must be a lowercase SHA-256 digest")
    for name, value in (
        ("source_upload_id", metadata.source_upload_id),
        ("parent_run_id", metadata.parent_run_id),
    ):
        if value is not None and _IDENTIFIER.fullmatch(value) is None:
            raise ValueError(f"{name} must be a framework identifier")
    for warning in snapshot.warnings:
        _validate_optional_text("warning", warning, maximum=2048, reject_url=False)
    _validate_optional_text("error_code", snapshot.error_code, maximum=128, reject_url=True)
    _validate_optional_text(
        "error_message",
        snapshot.error_message,
        maximum=2048,
        reject_url=False,
    )
    if snapshot.artifact_available:
        if snapshot.artifact_filename is None or snapshot.artifact_media_type is None:
            raise ValueError("available artifacts require filename and media type")
        validate_storage_filename(snapshot.artifact_filename)
        validate_storage_media_type(snapshot.artifact_media_type)
    elif snapshot.artifact_filename is not None or snapshot.artifact_media_type is not None:
        raise ValueError("unavailable artifacts must not include artifact metadata")


def _validate_event(event: RunEvent, *, expected_run_id: str) -> None:
    if not isinstance(event, RunEvent):
        raise TypeError("run store only accepts RunEvent instances")
    if event.run_id != expected_run_id:
        raise ValueError("event run_id does not match snapshot")
    if event.sequence <= 0:
        raise ValueError("event sequence must be positive")
    if event.warning_count < 0:
        raise ValueError("event warning_count must not be negative")
    _encode_datetime(event.occurred_at)
    _validate_optional_text("event error_code", event.error_code, maximum=128, reject_url=True)
    if event.artifact_available:
        if event.artifact_filename is None or event.artifact_media_type is None:
            raise ValueError("artifact events require filename and media type")
        if event.artifact_size_bytes is None or event.artifact_size_bytes < 0:
            raise ValueError("artifact events require a non-negative size")
        validate_storage_filename(event.artifact_filename)
        validate_storage_media_type(event.artifact_media_type)
    for name, value in (
        ("event node_id", event.node_id),
        ("event error_code", event.error_code),
    ):
        if value is not None and _IDENTIFIER.fullmatch(value) is None:
            raise ValueError(f"{name} must be a framework identifier")
    if event.node_kind is not None and event.node_kind not in {
        "transform",
        "agent",
        "action",
    }:
        raise ValueError("event node_kind is invalid")
    if event.node_status is not None and event.node_status not in {
        "running",
        "succeeded",
        "skipped",
        "cancelled",
        "failed",
    }:
        raise ValueError("event node_status is invalid")
    for name, value, minimum, maximum in (
        ("attempt", event.attempt, 1, 10),
        ("max_attempts", event.max_attempts, 1, 10),
        ("batch_size", event.batch_size, 0, 1_000_000),
        ("accepted_count", event.accepted_count, 0, 1_000_000),
    ):
        if value is not None and (value < minimum or value > maximum):
            raise ValueError(f"event {name} is outside its safe range")
    for name, value, maximum in (
        ("delay_seconds", event.delay_seconds, 3600.0),
        ("duration_ms", event.duration_ms, None),
    ):
        if value is not None and (
            not isfinite(value) or value < 0 or (maximum is not None and value > maximum)
        ):
            raise ValueError(f"event {name} is outside its safe range")


def _validate_optional_text(
    name: str,
    value: str | None,
    *,
    maximum: int,
    reject_url: bool,
) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise ValueError(f"{name} must be bounded, trimmed single-line text")
    if reject_url and "://" in value:
        raise ValueError(f"{name} must not contain a URL")


def _snapshot_values(snapshot: RunSnapshot, *, last_sequence: int) -> tuple[Any, ...]:
    metadata = snapshot.metadata
    return (
        snapshot.run_id,
        snapshot.state.value,
        _encode_datetime(snapshot.created_at),
        _encode_optional_datetime(snapshot.started_at),
        _encode_optional_datetime(snapshot.finished_at),
        int(snapshot.cancellation_requested),
        int(snapshot.partial),
        json.dumps(list(snapshot.warnings), ensure_ascii=False, separators=(",", ":")),
        snapshot.error_code,
        snapshot.error_message,
        int(snapshot.artifact_available),
        snapshot.artifact_media_type,
        snapshot.artifact_filename,
        metadata.plugin_id,
        metadata.plugin_version,
        metadata.provider,
        metadata.model,
        metadata.input_filename,
        metadata.input_media_type,
        metadata.input_sha256,
        metadata.source_upload_id,
        metadata.parent_run_id,
        last_sequence,
    )


def _event_values(event: RunEvent) -> tuple[Any, ...]:
    return (
        event.run_id,
        event.sequence,
        event.type.value,
        _encode_datetime(event.occurred_at),
        event.status.value,
        int(event.partial),
        event.warning_count,
        event.error_code,
        int(event.artifact_available),
        event.artifact_filename,
        event.artifact_media_type,
        event.artifact_size_bytes,
        event.node_id,
        event.node_kind,
        event.node_status,
        event.attempt,
        event.max_attempts,
        event.delay_seconds,
        event.batch_size,
        event.accepted_count,
        event.duration_ms,
    )


def _snapshot_from_row(row: sqlite3.Row) -> RunSnapshot:
    try:
        warnings = json.loads(str(row["warnings_json"]))
        if not isinstance(warnings, list) or not all(
            isinstance(warning, str) for warning in warnings
        ):
            raise ValueError
        snapshot = RunSnapshot(
            run_id=str(row["run_id"]),
            state=RunStatus(str(row["state"])),
            created_at=_decode_datetime(str(row["created_at"])),
            started_at=_decode_optional_datetime(row["started_at"]),
            finished_at=_decode_optional_datetime(row["finished_at"]),
            cancellation_requested=bool(row["cancellation_requested"]),
            partial=bool(row["partial"]),
            warnings=tuple(warnings),
            error_code=_optional_string(row["error_code"]),
            error_message=_optional_string(row["error_message"]),
            artifact_available=bool(row["artifact_available"]),
            artifact_media_type=_optional_string(row["artifact_media_type"]),
            artifact_filename=_optional_string(row["artifact_filename"]),
            metadata=RunMetadata(
                plugin_id=_optional_string(row["plugin_id"]),
                plugin_version=_optional_string(row["plugin_version"]),
                provider=_optional_string(row["provider"]),
                model=_optional_string(row["model"]),
                input_filename=_optional_string(row["input_filename"]),
                input_media_type=_optional_string(row["input_media_type"]),
                input_sha256=_optional_string(row["input_sha256"]),
                source_upload_id=_optional_string(row["source_upload_id"]),
                parent_run_id=_optional_string(row["parent_run_id"]),
            ),
        )
        _validate_snapshot(snapshot)
        return snapshot
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PersistenceError("stored run snapshot is invalid") from exc


def _event_from_row(row: sqlite3.Row) -> RunEvent:
    try:
        event = RunEvent(
            run_id=str(row["run_id"]),
            sequence=int(row["sequence"]),
            type=RunEventType(str(row["event_type"])),
            occurred_at=_decode_datetime(str(row["occurred_at"])),
            status=RunStatus(str(row["status"])),
            partial=bool(row["partial"]),
            warning_count=int(row["warning_count"]),
            error_code=_optional_string(row["error_code"]),
            artifact_available=bool(row["artifact_available"]),
            artifact_filename=_optional_string(row["artifact_filename"]),
            artifact_media_type=_optional_string(row["artifact_media_type"]),
            artifact_size_bytes=(
                None if row["artifact_size_bytes"] is None else int(row["artifact_size_bytes"])
            ),
            node_id=_optional_string(row["node_id"]),
            node_kind=_optional_string(row["node_kind"]),
            node_status=_optional_string(row["node_status"]),
            attempt=None if row["attempt"] is None else int(row["attempt"]),
            max_attempts=(None if row["max_attempts"] is None else int(row["max_attempts"])),
            delay_seconds=(None if row["delay_seconds"] is None else float(row["delay_seconds"])),
            batch_size=None if row["batch_size"] is None else int(row["batch_size"]),
            accepted_count=(None if row["accepted_count"] is None else int(row["accepted_count"])),
            duration_ms=(None if row["duration_ms"] is None else float(row["duration_ms"])),
        )
        _validate_event(event, expected_run_id=event.run_id)
        return event
    except (KeyError, TypeError, ValueError) as exc:
        raise PersistenceError("stored run event is invalid") from exc


def _encode_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("persisted datetimes must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _encode_optional_datetime(value: datetime | None) -> str | None:
    return None if value is None else _encode_datetime(value)


def _decode_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored datetime is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _decode_optional_datetime(value: Any) -> datetime | None:
    return None if value is None else _decode_datetime(str(value))


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "FilesystemArtifactStore",
    "PersistenceError",
    "SQLiteRunStore",
]
