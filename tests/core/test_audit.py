from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from agent_core.audit import (
    AuditEventType,
    AuditLogger,
    AuditMetadataError,
    AuditPayloadError,
    AuditRecord,
    InMemoryAuditSink,
)
from agent_core.contracts import RunStatus


def test_default_audit_keeps_metadata_and_sha256_but_not_raw_payload() -> None:
    sink = InMemoryAuditSink()
    logger = AuditLogger(sink)
    raw = "sensitive prompt and response"

    record = logger.record(
        run_id="run-1",
        event_type=AuditEventType.PROVIDER_REQUESTED,
        payload=raw,
        subject_id="agent-1",
        status=RunStatus.RUNNING,
        metadata={"provider.name": "fake"},
    )

    assert record.payload_sha256 == hashlib.sha256(raw.encode()).hexdigest()
    assert record.payload_size_bytes == len(raw.encode())
    assert record.metadata_dict == {
        "audit.hash_algorithm": "sha256",
        "audit.payload_type": "text",
        "provider.name": "fake",
    }
    assert raw not in record.model_dump_json()
    assert sink.records == (record,)


def test_in_memory_audit_sink_is_bounded() -> None:
    sink = InMemoryAuditSink(max_records=2)
    logger = AuditLogger(sink)
    for index in range(3):
        logger.record(
            run_id=f"run-{index}",
            event_type=AuditEventType.RUN_STARTED,
        )

    assert [item.run_id for item in sink.records] == ["run-1", "run-2"]
    with pytest.raises(ValueError, match="positive"):
        InMemoryAuditSink(max_records=0)


def test_canonical_json_hash_is_stable_across_mapping_order() -> None:
    logger = AuditLogger()
    first = logger.record(
        run_id="run-1",
        event_type=AuditEventType.NODE_COMPLETED,
        payload={"b": 2, "a": 1},
    )
    second = logger.record(
        run_id="run-1",
        event_type=AuditEventType.NODE_COMPLETED,
        payload={"a": 1, "b": 2},
    )

    assert first.payload_sha256 == second.payload_sha256


def test_audit_rejects_raw_content_metadata_and_unsupported_payloads() -> None:
    logger = AuditLogger()
    with pytest.raises(AuditMetadataError, match="forbidden"):
        logger.record(
            run_id="run-1",
            event_type=AuditEventType.RUN_STARTED,
            metadata={"request.prompt": "do not retain me"},
        )
    with pytest.raises(AuditPayloadError):
        logger.record(
            run_id="run-1",
            event_type=AuditEventType.RUN_STARTED,
            payload=object(),
        )


def test_audit_contract_is_frozen_and_forbids_extra_raw_fields() -> None:
    record = AuditLogger().record(
        run_id="run-1",
        event_type=AuditEventType.RUN_COMPLETED,
        status=RunStatus.SUCCEEDED,
    )
    with pytest.raises(ValidationError):
        record.status = RunStatus.FAILED  # ty: ignore[invalid-assignment]  # Verify frozen-model rejection.

    values = record.model_dump()
    values["raw_payload"] = "secret"
    with pytest.raises(ValidationError):
        AuditRecord.model_validate(values)
