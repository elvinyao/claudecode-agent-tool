from __future__ import annotations

from pathlib import Path

import httpx


async def publish_output_bytes(uri: str, data: bytes) -> str:
    """Publish output bytes to an HTTP/HTTPS endpoint or local file path."""
    if uri.startswith(("http://", "https://")):
        async with httpx.AsyncClient() as client:
            resp = await client.put(uri, content=data)
            resp.raise_for_status()
            return f"HTTP {resp.status_code}"
    else:
        file_path = uri.removeprefix("file://")
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)
