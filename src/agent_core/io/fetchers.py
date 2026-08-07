from __future__ import annotations

from pathlib import Path

import httpx


async def fetch_input_bytes(uri: str) -> bytes:
    """Fetch raw input bytes from an HTTP/HTTPS URL or local file path."""
    if uri.startswith(("http://", "https://")):
        async with httpx.AsyncClient() as client:
            resp = await client.get(uri, follow_redirects=True)
            resp.raise_for_status()
            return resp.content
    else:
        file_path = uri.removeprefix("file://")
        path = Path(file_path)
        return path.read_bytes()
