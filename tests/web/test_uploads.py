from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_core.uploads import (
    InMemoryUploadStore,
    UploadNotFoundError,
    UploadStoreFullError,
    UploadTooLargeError,
)


@pytest.mark.asyncio
async def test_upload_store_roundtrip_stats_delete_and_expiry() -> None:
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)

    def clock() -> datetime:
        return now

    store = InMemoryUploadStore(
        max_records=2,
        max_upload_bytes=8,
        max_total_bytes=12,
        ttl_seconds=60,
        clock=clock,
    )
    first = await store.put(b"hello", filename="a.md", media_type="text/markdown")
    record = await store.get(first.upload_id)
    stats = await store.stats()

    assert record.content == b"hello"
    assert first.sha256 == record.sha256
    assert first.size_bytes == 5
    assert stats["records"] == 1
    assert stats["bytes"] == 5

    await store.delete(first.upload_id)
    with pytest.raises(UploadNotFoundError):
        await store.get(first.upload_id)

    expiring = await store.put(b"old", filename="old.txt", media_type="text/plain")
    now += timedelta(seconds=60)
    with pytest.raises(UploadNotFoundError):
        await store.get(expiring.upload_id)
    assert (await store.stats())["bytes"] == 0


@pytest.mark.asyncio
async def test_upload_store_enforces_item_record_and_total_capacity() -> None:
    store = InMemoryUploadStore(
        max_records=2,
        max_upload_bytes=5,
        max_total_bytes=6,
    )

    with pytest.raises(UploadTooLargeError):
        await store.put(b"123456", filename="big", media_type="text/plain")

    await store.put(b"1234", filename="one", media_type="text/plain")
    with pytest.raises(UploadStoreFullError):
        await store.put(b"123", filename="two", media_type="text/plain")

    await store.put(b"12", filename="two", media_type="text/plain")
    with pytest.raises(UploadStoreFullError):
        await store.put(b"", filename="three", media_type="text/plain")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_records": 0}, "max_records"),
        ({"max_upload_bytes": 0}, "max_upload_bytes"),
        ({"max_total_bytes": 0}, "max_total_bytes"),
        ({"max_upload_bytes": 2, "max_total_bytes": 1}, "must not exceed"),
        ({"ttl_seconds": 0}, "ttl_seconds"),
    ],
)
def test_upload_store_rejects_invalid_limits(kwargs: dict[str, int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        InMemoryUploadStore(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "media_type"),
    [
        ("../secret", "text/plain"),
        ("input.txt", "text/plain\r\nX-Unsafe: yes"),
    ],
)
async def test_upload_store_rejects_unsafe_metadata(
    filename: str,
    media_type: str,
) -> None:
    store = InMemoryUploadStore()

    with pytest.raises(ValueError):
        await store.put(b"safe", filename=filename, media_type=media_type)
