from __future__ import annotations

import http.client
import ipaddress
import socket
import threading
from collections.abc import Callable
from queue import Empty, Queue
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from .http_policy import (
    Header,
    HttpPolicyError,
    HttpRequest,
    HttpResponse,
    ResponseLimitError,
    canonical_http_url,
)


_CREDENTIAL_HEADERS = frozenset({"authorization", "x-api-key"})
_RESPONSE_HEADERS = frozenset(
    {
        "content-length",
        "content-type",
        "etag",
        "last-modified",
        "license",
        "link",
        "location",
        "retry-after",
    }
)


class CredentialProfileUnavailable(HttpPolicyError):
    pass


class CredentialProvider(Protocol):
    def headers_for(self, profile_name: str) -> tuple[Header, ...]: ...


class DenyCredentialProvider:
    def headers_for(self, profile_name: str) -> tuple[Header, ...]:
        raise CredentialProfileUnavailable(
            f"credential profile {profile_name!r} is not configured"
        )


class _SocketLike(Protocol):
    def getpeername(self) -> tuple[object, ...]: ...


class _ResponseLike(Protocol):
    status: int

    def getheaders(self) -> list[tuple[str, str]]: ...

    def read(self, amount: int | None = None) -> bytes: ...


class _ConnectionLike(Protocol):
    sock: _SocketLike | None

    def request(
        self,
        method: str,
        url: str,
        body: object = None,
        headers: dict[str, str] | None = None,
    ) -> None: ...

    def getresponse(self) -> _ResponseLike: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[str, str, int, str, float], _ConnectionLike]
DnsLookup = Callable[..., list[tuple[object, ...]]]
_DNS_SLOTS = threading.BoundedSemaphore(4)


def _stdlib_connection(
    scheme: str,
    hostname: str,
    port: int,
    approved_ip: str,
    timeout: float,
) -> _ConnectionLike:
    if scheme == "https":
        return _PinnedHTTPSConnection(
            hostname,
            port,
            approved_ip=approved_ip,
            timeout=timeout,
        )
    return _PinnedHTTPConnection(
        hostname,
        port,
        approved_ip=approved_ip,
        timeout=timeout,
    )


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(
        self,
        hostname: str,
        port: int,
        *,
        approved_ip: str,
        timeout: float,
    ) -> None:
        super().__init__(hostname, port, timeout=timeout)
        self._approved_ip = approved_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._approved_ip, self.port),
            self.timeout,
            self.source_address,
        )
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        hostname: str,
        port: int,
        *,
        approved_ip: str,
        timeout: float,
    ) -> None:
        super().__init__(hostname, port, timeout=timeout)
        self._approved_ip = approved_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._approved_ip, self.port),
            self.timeout,
            self.source_address,
        )
        server_hostname = self.host
        if self._tunnel_host:
            self._tunnel()
            server_hostname = self._tunnel_host
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=server_hostname,
        )


class SocketDnsResolver:
    def __init__(self, lookup: DnsLookup = socket.getaddrinfo) -> None:
        self._lookup = lookup

    def resolve(
        self,
        hostname: str,
        port: int,
        timeout_seconds: float,
    ) -> tuple[str, ...]:
        if timeout_seconds <= 0 or not _DNS_SLOTS.acquire(blocking=False):
            raise TimeoutError(f"DNS resolution timed out for {hostname}")
        result: Queue[object] = Queue(maxsize=1)

        def run_lookup() -> None:
            try:
                result.put(
                    self._lookup(
                        hostname,
                        port,
                        type=socket.SOCK_STREAM,
                        proto=socket.IPPROTO_TCP,
                    )
                )
            except Exception as exc:
                result.put(exc)
            finally:
                _DNS_SLOTS.release()

        threading.Thread(target=run_lookup, daemon=True).start()
        try:
            resolved = result.get(timeout=timeout_seconds)
        except Empty as exc:
            raise TimeoutError(f"DNS resolution timed out for {hostname}") from exc
        if isinstance(resolved, Exception):
            raise OSError(f"DNS resolution failed for {hostname}") from resolved
        answers = resolved
        addresses: list[str] = []
        for answer in answers:
            sockaddr = answer[4]
            if not isinstance(sockaddr, tuple) or not sockaddr:
                continue
            try:
                address = ipaddress.ip_address(str(sockaddr[0])).compressed
            except ValueError:
                continue
            if address not in addresses:
                addresses.append(address)
        return tuple(addresses)


class StdlibOneHopTransport:
    def __init__(
        self,
        *,
        credential_provider: CredentialProvider | None = None,
        connection_factory: ConnectionFactory = _stdlib_connection,
    ) -> None:
        self._credentials = credential_provider or DenyCredentialProvider()
        self._connection_factory = connection_factory

    def send(self, request: HttpRequest) -> HttpResponse:
        canonical = canonical_http_url(request.url)
        parts = urlsplit(canonical)
        assert parts.hostname is not None
        if not request.approved_peer_ips:
            raise HttpPolicyError("live HTTP requires approved peer IPs")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        headers = {name: value for name, value in request.headers}
        headers["accept-encoding"] = "identity"
        if request.credential_profile_name is not None:
            if parts.scheme != "https":
                raise HttpPolicyError("credentials require HTTPS")
            headers.update(
                _credential_headers(
                    self._credentials.headers_for(request.credential_profile_name)
                )
            )
        target = urlunsplit(("", "", parts.path, parts.query, ""))
        last_error: OSError | None = None
        per_address_timeout = float(request.timeout_seconds) / len(
            request.approved_peer_ips
        )
        for approved_ip in request.approved_peer_ips:
            connection = self._connection_factory(
                parts.scheme,
                parts.hostname,
                port,
                approved_ip,
                per_address_timeout,
            )
            try:
                return _read_one_hop(
                    connection,
                    target=target,
                    headers=headers,
                    max_response_bytes=request.max_response_bytes,
                )
            except OSError as exc:
                last_error = exc
            finally:
                connection.close()
        assert last_error is not None
        raise last_error


def _read_one_hop(
    connection: _ConnectionLike,
    *,
    target: str,
    headers: dict[str, str],
    max_response_bytes: int,
) -> HttpResponse:
    connection.request("GET", target, headers=headers)
    if connection.sock is None:
        raise OSError("HTTP connection did not expose its connected peer")
    peer = connection.sock.getpeername()
    if not isinstance(peer, tuple) or not peer:
        raise OSError("HTTP connection returned an invalid peer")
    peer_ip = ipaddress.ip_address(str(peer[0])).compressed
    response = connection.getresponse()
    body = response.read(max_response_bytes + 1)
    if len(body) > max_response_bytes:
        raise ResponseLimitError(
            "HTTP response exceeds its byte limit",
            consumed_bytes=len(body),
        )
    return HttpResponse(
        status=response.status,
        peer_ip=peer_ip,
        headers=_response_headers(response.getheaders()),
        body=body,
    )


def _credential_headers(headers: tuple[Header, ...]) -> dict[str, str]:
    if not isinstance(headers, tuple) or not headers:
        raise CredentialProfileUnavailable("credential profile returned no headers")
    normalized: dict[str, str] = {}
    for item in headers:
        if not isinstance(item, tuple) or len(item) != 2:
            raise CredentialProfileUnavailable("credential profile headers are malformed")
        name, value = item
        lowered = name.lower()
        if (
            lowered not in _CREDENTIAL_HEADERS
            or lowered in normalized
            or not isinstance(value, str)
            or not value
            or any(character in value for character in "\r\n\0")
        ):
            raise CredentialProfileUnavailable(
                "credential profile returned an unsafe header"
            )
        normalized[lowered] = value
    return normalized


def _response_headers(headers: list[tuple[str, str]]) -> tuple[Header, ...]:
    combined: dict[str, list[str]] = {}
    for name, value in headers:
        lowered = name.lower()
        if (
            lowered not in _RESPONSE_HEADERS
            or not isinstance(value, str)
            or any(character in value for character in "\r\n\0")
        ):
            continue
        combined.setdefault(lowered, []).append(value)
    return tuple(
        (name, ", ".join(values))
        for name, values in sorted(combined.items())
    )


__all__ = [
    "CredentialProfileUnavailable",
    "CredentialProvider",
    "DenyCredentialProvider",
    "SocketDnsResolver",
    "StdlibOneHopTransport",
]
