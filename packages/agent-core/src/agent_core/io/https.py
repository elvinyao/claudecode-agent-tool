"""Bounded HTTPS-only transport with SSRF-resistant destination policy.

The exact-server allowlist is the primary trust boundary. DNS address checks
are defense in depth: every resolved address must be globally routable before
httpx is allowed to connect. Redirects are deliberately disabled so a trusted
server cannot redirect a request into a less-trusted network boundary.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

_METADATA_ADDRESSES = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec2::254"),
}


class HttpsIoError(RuntimeError):
    """A transport error whose public text never contains a requested URL."""

    def __init__(self, code: str, public_message: str) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message


def _canonical_host(host: str) -> str:
    value = host.rstrip(".")
    if not value:
        raise HttpsIoError("invalid_url", "HTTPS URL must include a host")
    try:
        return value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise HttpsIoError("invalid_url", "HTTPS URL host is invalid") from exc


def _server_name(host: str, port: int) -> str:
    rendered_host = f"[{host}]" if ":" in host else host
    return f"{rendered_host}:{port}"


def _parse_server(value: str) -> str:
    raw = value.strip()
    if not raw or "/" in raw or "@" in raw or "?" in raw or "#" in raw:
        raise ValueError("allowed server must be an exact host or host:port")
    try:
        parsed = urlsplit(f"//{raw}")
        host = _canonical_host(parsed.hostname or "")
        port = parsed.port or 443
    except (HttpsIoError, ValueError) as exc:
        raise ValueError("allowed server must be an exact host or host:port") from exc
    if not 1 <= port <= 65535:
        raise ValueError("allowed server port is invalid")
    return _server_name(host, port)


class HttpsIoPolicy(BaseModel):
    """Fail-closed HTTPS transport limits supplied by the composition root."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_servers: frozenset[str] = Field(default_factory=frozenset)
    max_input_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    max_output_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    read_timeout_seconds: float = Field(default=30.0, gt=0)
    write_timeout_seconds: float = Field(default=30.0, gt=0)
    pool_timeout_seconds: float = Field(default=5.0, gt=0)
    total_timeout_seconds: float = Field(default=45.0, gt=0)
    max_connections: int = Field(default=20, ge=1, le=1_000)
    max_keepalive_connections: int = Field(default=10, ge=0, le=1_000)

    @field_validator("allowed_servers", mode="before")
    @classmethod
    def normalize_servers(cls, value: object) -> frozenset[str]:
        if value is None:
            return frozenset()
        if isinstance(value, str):
            values = [value]
        else:
            try:
                values = list(value)  # type: ignore[arg-type]
            except TypeError as exc:
                raise ValueError("allowed_servers must be a collection") from exc
        return frozenset(_parse_server(str(item)) for item in values)

    @field_validator("max_keepalive_connections")
    @classmethod
    def keepalive_not_above_total(cls, value: int, info) -> int:
        maximum = info.data.get("max_connections")
        if maximum is not None and value > maximum:
            raise ValueError("max_keepalive_connections cannot exceed max_connections")
        return value


@dataclass(frozen=True, slots=True)
class ResolvedHost:
    """A syntactically valid HTTPS destination before or after DNS checks."""

    url: str
    host: str
    port: int
    server: str
    address: str | None = None

    @property
    def connect_url(self) -> str:
        """Return a URL pinned to the already validated DNS address."""

        if self.address is None:
            return self.url
        parsed = urlsplit(self.url)
        return urlunsplit(
            (
                parsed.scheme,
                _server_name(self.address, self.port),
                parsed.path,
                parsed.query,
                "",
            )
        )

    @property
    def host_header(self) -> str:
        rendered_host = f"[{self.host}]" if ":" in self.host else self.host
        return rendered_host if self.port == 443 else f"{rendered_host}:{self.port}"


@dataclass(frozen=True, slots=True)
class PublishReceipt:
    """Non-sensitive acknowledgement from an HTTPS PUT."""

    status_code: int
    server: str


class HostResolver(Protocol):
    async def __call__(self, host: str, port: int) -> Sequence[str]: ...


def _parsed_https_url(url: str) -> tuple[SplitResult, ResolvedHost]:
    try:
        parsed = urlsplit(url)
        port = parsed.port or 443
    except ValueError as exc:
        raise HttpsIoError("invalid_url", "HTTPS URL is invalid") from exc
    if parsed.scheme.lower() != "https":
        raise HttpsIoError("invalid_scheme", "Only HTTPS URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise HttpsIoError("userinfo_forbidden", "HTTPS URL user information is forbidden")
    if parsed.fragment:
        raise HttpsIoError("fragment_forbidden", "HTTPS URL fragments are forbidden")
    host = _canonical_host(parsed.hostname or "")
    if not 1 <= port <= 65535:
        raise HttpsIoError("invalid_url", "HTTPS URL port is invalid")
    destination = ResolvedHost(
        url=url,
        host=host,
        port=port,
        server=_server_name(host, port),
    )
    return parsed, destination


def validate_https_url_syntax(url: str) -> str:
    """Validate only URL syntax; policy and DNS checks happen in the client."""

    _parsed_https_url(url)
    return url


def _safe_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address in _METADATA_ADDRESSES:
        return False
    return bool(
        address.is_global
        and not address.is_loopback
        and not address.is_private
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


async def _default_resolver(host: str, port: int) -> Sequence[str]:
    loop = asyncio.get_running_loop()
    try:
        records = await loop.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as exc:
        raise HttpsIoError("dns_failed", "HTTPS destination could not be resolved") from exc
    return tuple(sorted({str(record[4][0]) for record in records}))


class HttpsIoClient:
    """Fetch JSON bytes with GET and publish artifacts with PUT."""

    def __init__(
        self,
        policy: HttpsIoPolicy,
        *,
        resolver: HostResolver | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.policy = policy
        self._resolver = resolver or _default_resolver
        self._client = client
        self._owns_client = client is None
        self._client_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                timeout = httpx.Timeout(
                    connect=self.policy.connect_timeout_seconds,
                    read=self.policy.read_timeout_seconds,
                    write=self.policy.write_timeout_seconds,
                    pool=self.policy.pool_timeout_seconds,
                )
                limits = httpx.Limits(
                    max_connections=self.policy.max_connections,
                    max_keepalive_connections=self.policy.max_keepalive_connections,
                )
                self._client = httpx.AsyncClient(
                    follow_redirects=False,
                    limits=limits,
                    timeout=timeout,
                    trust_env=False,
                )
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def validate_destination(self, url: str) -> ResolvedHost:
        """Enforce exact server policy and reject every non-global DNS answer."""

        _parsed, destination = _parsed_https_url(url)
        if destination.server not in self.policy.allowed_servers:
            raise HttpsIoError("server_not_allowed", "HTTPS destination is not allowed")

        try:
            literal = ipaddress.ip_address(destination.host)
        except ValueError:
            addresses = await self._resolver(destination.host, destination.port)
        else:
            addresses = (str(literal),)
        if not addresses:
            raise HttpsIoError("dns_failed", "HTTPS destination could not be resolved")
        if any(not _safe_address(address) for address in addresses):
            raise HttpsIoError(
                "unsafe_address",
                "HTTPS destination resolves to a forbidden network address",
            )
        canonical_addresses = sorted(str(ipaddress.ip_address(item)) for item in addresses)
        return ResolvedHost(
            url=destination.url,
            host=destination.host,
            port=destination.port,
            server=destination.server,
            address=canonical_addresses[0],
        )

    def validate_allowed_server(self, url: str) -> ResolvedHost:
        """Perform non-network URL and exact-allowlist checks for API admission."""

        _parsed, destination = _parsed_https_url(url)
        if destination.server not in self.policy.allowed_servers:
            raise HttpsIoError("server_not_allowed", "HTTPS destination is not allowed")
        try:
            literal = ipaddress.ip_address(destination.host)
        except ValueError:
            return destination
        if not _safe_address(str(literal)):
            raise HttpsIoError(
                "unsafe_address",
                "HTTPS destination resolves to a forbidden network address",
            )
        return destination

    async def fetch(self, url: str) -> bytes:
        try:
            return await asyncio.wait_for(
                self._validated_fetch(url),
                timeout=self.policy.total_timeout_seconds,
            )
        except HttpsIoError:
            raise
        except asyncio.TimeoutError as exc:
            raise HttpsIoError("timeout", "HTTPS input request timed out") from exc
        except httpx.HTTPError as exc:
            raise HttpsIoError("request_failed", "HTTPS input request failed") from exc

    async def _validated_fetch(self, url: str) -> bytes:
        destination = await self.validate_destination(url)
        client = await self._get_client()
        return await self._fetch(client, destination)

    async def _fetch(self, client: httpx.AsyncClient, destination: ResolvedHost) -> bytes:
        async with client.stream(
            "GET",
            destination.connect_url,
            headers={
                "Accept": "application/json, application/octet-stream",
                "Connection": "close",
                "Host": destination.host_header,
            },
            follow_redirects=False,
            extensions={"sni_hostname": destination.host},
        ) as response:
            if 300 <= response.status_code < 400:
                raise HttpsIoError("redirect_forbidden", "HTTPS redirects are forbidden")
            if not 200 <= response.status_code < 300:
                raise HttpsIoError("upstream_status", "HTTPS input server rejected the request")
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except ValueError as exc:
                    raise HttpsIoError(
                        "invalid_content_length",
                        "HTTPS input returned an invalid content length",
                    ) from exc
                if declared_size > self.policy.max_input_bytes:
                    raise HttpsIoError("input_too_large", "HTTPS input exceeds the size limit")

            chunks: list[bytes] = []
            received = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > self.policy.max_input_bytes:
                    raise HttpsIoError("input_too_large", "HTTPS input exceeds the size limit")
                chunks.append(chunk)
            return b"".join(chunks)

    async def publish(self, url: str, data: bytes, *, media_type: str) -> PublishReceipt:
        if len(data) > self.policy.max_output_bytes:
            raise HttpsIoError("output_too_large", "Artifact exceeds the size limit")
        try:
            return await asyncio.wait_for(
                self._validated_publish(url, data, media_type=media_type),
                timeout=self.policy.total_timeout_seconds,
            )
        except HttpsIoError:
            raise
        except asyncio.TimeoutError as exc:
            raise HttpsIoError("timeout", "HTTPS output request timed out") from exc
        except httpx.HTTPError as exc:
            raise HttpsIoError("request_failed", "HTTPS output request failed") from exc

    async def _validated_publish(
        self,
        url: str,
        data: bytes,
        *,
        media_type: str,
    ) -> PublishReceipt:
        destination = await self.validate_destination(url)
        client = await self._get_client()
        return await self._publish(client, destination, data, media_type=media_type)

    async def _publish(
        self,
        client: httpx.AsyncClient,
        destination: ResolvedHost,
        data: bytes,
        *,
        media_type: str,
    ) -> PublishReceipt:
        async with client.stream(
            "PUT",
            destination.connect_url,
            content=data,
            headers={
                "Connection": "close",
                "Content-Type": media_type,
                "Host": destination.host_header,
            },
            follow_redirects=False,
            extensions={"sni_hostname": destination.host},
        ) as response:
            if 300 <= response.status_code < 400:
                raise HttpsIoError("redirect_forbidden", "HTTPS redirects are forbidden")
            if not 200 <= response.status_code < 300:
                raise HttpsIoError("upstream_status", "HTTPS output server rejected the request")
            return PublishReceipt(status_code=response.status_code, server=destination.server)


FetchCallable = Callable[[str], Awaitable[bytes]]
PublishCallable = Callable[[str, bytes, str], Awaitable[PublishReceipt]]


__all__ = [
    "FetchCallable",
    "HostResolver",
    "HttpsIoClient",
    "HttpsIoError",
    "HttpsIoPolicy",
    "PublishCallable",
    "PublishReceipt",
    "ResolvedHost",
    "validate_https_url_syntax",
]
