from __future__ import annotations

from typing import Any

import httpx
import pytest

from agent_core.io import HttpsIoClient, HttpsIoError, HttpsIoPolicy


async def public_resolver(host: str, port: int) -> tuple[str, ...]:
    assert host == "example.com"
    assert port == 443
    return ("93.184.216.34",)


def client_for(
    handler: Any,
    *,
    max_input_bytes: int = 1024,
    max_output_bytes: int = 1024,
    resolver=public_resolver,
) -> HttpsIoClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, follow_redirects=False)
    policy = HttpsIoPolicy(
        allowed_servers={"example.com:443"},
        max_input_bytes=max_input_bytes,
        max_output_bytes=max_output_bytes,
    )
    return HttpsIoClient(policy, resolver=resolver, client=http_client)


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://example.com/input.json", "invalid_scheme"),
        ("file:///etc/passwd", "invalid_scheme"),
        ("https://user:secret@example.com/input.json", "userinfo_forbidden"),
        ("https://example.com/input.json#fragment", "fragment_forbidden"),
        ("https://other.example/input.json", "server_not_allowed"),
    ],
)
@pytest.mark.asyncio
async def test_destination_policy_rejects_unsafe_url_shapes(url: str, code: str) -> None:
    client = client_for(lambda request: httpx.Response(200, content=b"{}"))

    with pytest.raises(HttpsIoError) as error:
        await client.fetch(url)

    assert error.value.code == code
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_destination_rejects_private_or_mixed_dns_answers_before_http() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, content=b"{}")

    async def unsafe_resolver(host: str, port: int) -> tuple[str, ...]:
        return ("93.184.216.34", "169.254.169.254")

    client = client_for(handler, resolver=unsafe_resolver)

    with pytest.raises(HttpsIoError) as error:
        await client.fetch("https://example.com/input.json")

    assert error.value.code == "unsafe_address"
    assert called is False


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "::1", "10.0.0.1", "192.168.1.1", "169.254.169.254"],
)
@pytest.mark.asyncio
async def test_literal_non_global_addresses_are_rejected(address: str) -> None:
    server = f"[{address}]:443" if ":" in address else f"{address}:443"
    policy = HttpsIoPolicy(allowed_servers={server})
    client = HttpsIoClient(policy)

    with pytest.raises(HttpsIoError) as error:
        await client.fetch(f"https://{server}/metadata")

    assert error.value.code == "unsafe_address"


@pytest.mark.asyncio
async def test_fetch_is_https_get_and_returns_bounded_bytes() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["host"] = request.headers["host"]
        seen["sni"] = request.extensions["sni_hostname"]
        return httpx.Response(200, content=b'{"safe":true}')

    client = client_for(handler)

    content = await client.fetch("https://example.com/input.json?signature=hidden")

    assert content == b'{"safe":true}'
    assert seen == {
        "method": "GET",
        "url": "https://93.184.216.34/input.json?signature=hidden",
        "host": "example.com",
        "sni": "example.com",
    }


@pytest.mark.asyncio
async def test_validated_dns_answer_is_pinned_into_the_actual_connection_url() -> None:
    calls = 0
    seen_url = ""

    async def changing_resolver(host: str, port: int) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        return ("93.184.216.34",) if calls == 1 else ("127.0.0.1",)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_url
        seen_url = str(request.url)
        return httpx.Response(200, content=b"{}")

    client = client_for(handler, resolver=changing_resolver)

    assert await client.fetch("https://example.com/input") == b"{}"
    assert calls == 1
    assert seen_url == "https://93.184.216.34/input"


@pytest.mark.asyncio
async def test_fetch_rejects_redirect_and_redacts_signed_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/private"})

    client = client_for(handler)
    url = "https://example.com/input?signature=TOP-SECRET"

    with pytest.raises(HttpsIoError) as error:
        await client.fetch(url)

    assert error.value.code == "redirect_forbidden"
    assert "TOP-SECRET" not in str(error.value)


@pytest.mark.asyncio
async def test_fetch_rejects_declared_or_streamed_oversize_body() -> None:
    client = client_for(
        lambda request: httpx.Response(200, content=b"12345"),
        max_input_bytes=4,
    )

    with pytest.raises(HttpsIoError) as error:
        await client.fetch("https://example.com/input")

    assert error.value.code == "input_too_large"


@pytest.mark.asyncio
async def test_publish_uses_put_media_type_and_enforces_output_limit() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = request.content
        return httpx.Response(204)

    client = client_for(handler, max_output_bytes=4)
    receipt = await client.publish(
        "https://example.com/report",
        b"html",
        media_type="text/html; charset=utf-8",
    )

    assert receipt.status_code == 204
    assert receipt.server == "example.com:443"
    assert seen == {
        "method": "PUT",
        "content_type": "text/html; charset=utf-8",
        "body": b"html",
    }

    with pytest.raises(HttpsIoError) as error:
        await client.publish(
            "https://example.com/report",
            b"12345",
            media_type="text/plain",
        )
    assert error.value.code == "output_too_large"
