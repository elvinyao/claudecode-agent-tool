from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_core.audit import (
    AuditEventType,
    AuditLogger,
    AuditRecord,
    JsonlAuditSink,
    default_audit_path,
)
from agent_core.contracts import RunStatus


def test_default_audit_path_uses_injected_platform_environment_and_home(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    xdg = tmp_path / "xdg-state"
    local = tmp_path / "local-app-data"

    assert default_audit_path(
        env={"XDG_STATE_HOME": str(xdg)}, home=home, platform="linux"
    ) == xdg / "agent_core" / "audit.jsonl"
    assert default_audit_path(
        env={"LOCALAPPDATA": str(local)}, home=home, platform="win32"
    ) == local / "agent_core" / "audit.jsonl"
    assert default_audit_path(env={}, home=home, platform="win32") == (
        home / "AppData" / "Local" / "agent_core" / "audit.jsonl"
    )
    assert default_audit_path(env={}, home=home, platform="darwin") == (
        home / "Library" / "Application Support" / "agent_core" / "audit.jsonl"
    )
    assert default_audit_path(env={}, home=home, platform="linux") == (
        home / ".agent_core" / "audit.jsonl"
    )


def test_default_audit_path_ignores_relative_state_environment(tmp_path: Path) -> None:
    assert default_audit_path(
        env={"XDG_STATE_HOME": "relative-state"}, home=tmp_path, platform="linux"
    ) == tmp_path / ".agent_core" / "audit.jsonl"


def test_jsonl_sink_writes_valid_records_with_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "private-audit" / "events.jsonl"
    sink = JsonlAuditSink(path)
    record = AuditLogger(sink).record(
        run_id="run-1",
        event_type=AuditEventType.RUN_COMPLETED,
        payload={"result": "not persisted"},
        subject_id="plugin-1",
        status=RunStatus.SUCCEEDED,
        metadata={"provider.name": "codex"},
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    persisted = AuditRecord.model_validate(json.loads(lines[0]))
    assert persisted == record

    if os.name != "nt":
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_jsonl_sink_appends_complete_lines_from_concurrent_threads(tmp_path: Path) -> None:
    path = tmp_path / "audit" / "events.jsonl"
    sink = JsonlAuditSink(path)
    logger = AuditLogger(sink)
    count = 80

    def emit(index: int) -> str:
        return logger.record(
            run_id=f"run-{index}",
            event_type=AuditEventType.NODE_COMPLETED,
            payload=f"raw-{index}",
            metadata={"node.index": index},
        ).event_id

    with ThreadPoolExecutor(max_workers=12) as executor:
        event_ids = set(executor.map(emit, range(count)))

    lines = path.read_text(encoding="utf-8").splitlines()
    records = [AuditRecord.model_validate_json(line) for line in lines]
    assert len(lines) == count
    assert len(event_ids) == count
    assert {record.event_id for record in records} == event_ids
    assert {record.run_id for record in records} == {f"run-{index}" for index in range(count)}


def test_jsonl_sink_never_persists_raw_payload_or_url_query(tmp_path: Path) -> None:
    path = tmp_path / "audit" / "events.jsonl"
    signed_url = "https://example.test/report?id=42&token=do-not-store#private"
    raw = "private prompt and private response: do-not-store"

    AuditLogger(JsonlAuditSink(path)).record(
        run_id="run-sensitive",
        event_type=AuditEventType.PROVIDER_COMPLETED,
        payload={"prompt": raw, "response": raw, "url": signed_url},
        metadata={"target.url": signed_url},
    )

    persisted_text = path.read_text(encoding="utf-8")
    assert raw not in persisted_text
    assert "do-not-store" not in persisted_text
    assert "?id=42" not in persisted_text
    assert "#private" not in persisted_text

    persisted = AuditRecord.model_validate_json(persisted_text)
    assert persisted.metadata_dict["target.url"] == "https://example.test/report"

    credential_url = "https://username:password@example.test/private?token=secret"
    AuditLogger(JsonlAuditSink(path)).record(
        run_id="run-userinfo",
        event_type=AuditEventType.RUN_STARTED,
        metadata={"target.url": credential_url},
    )
    updated = path.read_text(encoding="utf-8")
    assert "username" not in updated
    assert "password" not in updated
    assert "token=secret" not in updated


def test_jsonl_sink_flushes_and_fsyncs_each_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "audit" / "events.jsonl"
    sink = JsonlAuditSink(path)
    real_fsync = os.fsync
    descriptors: list[int] = []

    def recording_fsync(descriptor: int) -> None:
        descriptors.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    AuditLogger(sink).record(
        run_id="run-fsync",
        event_type=AuditEventType.RUN_STARTED,
    )

    assert len(descriptors) == 1
    assert path.read_text(encoding="utf-8").endswith("\n")
