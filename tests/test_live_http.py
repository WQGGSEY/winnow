from __future__ import annotations

import socket
import threading

import pytest

from research_harness.acquisition.http_policy import (
    HttpPolicyError,
    HttpRequest,
    ResponseLimitError,
)
from research_harness.acquisition.live_http import (
    CredentialProfileUnavailable,
    SocketDnsResolver,
    StdlibOneHopTransport,
    _PinnedHTTPConnection,
)


class FixedCredentials:
    def headers_for(self, profile_name: str) -> tuple[tuple[str, str], ...]:
        assert profile_name == "official-api"
        return (("Authorization", "Bearer do-not-record"),)


class FakeSocket:
    def getpeername(self) -> tuple[str, int]:
        return ("93.184.216.34", 443)


class FakeResponse:
    status = 200

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.read_amounts: list[int | None] = []

    def getheaders(self) -> list[tuple[str, str]]:
        return [
            ("Content-Type", "application/json"),
            ("Set-Cookie", "session=do-not-record"),
            ("Link", "</license>; rel=\"license\""),
            ("Link", "</next>; rel=\"next\""),
        ]

    def read(self, amount: int | None = None) -> bytes:
        self.read_amounts.append(amount)
        return self.body if amount is None else self.body[:amount]


class FakeConnection:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.sock = FakeSocket()
        self.request_args: tuple[object, ...] | None = None
        self.closed = False

    def request(
        self,
        method: str,
        url: str,
        body: object = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.request_args = (method, url, body, headers)

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


def test_live_transport_keeps_secrets_inside_the_adapter() -> None:
    raw = FakeResponse(b'{"value":7}')
    connection = FakeConnection(raw)
    factory_args: list[tuple[object, ...]] = []

    def factory(
        scheme: str,
        hostname: str,
        port: int,
        approved_ip: str,
        timeout: float,
    ) -> FakeConnection:
        factory_args.append((scheme, hostname, port, approved_ip, timeout))
        return connection

    request = HttpRequest(
        "https://api.example/data",
        credential_profile_name="official-api",
        approved_peer_ips=("93.184.216.34",),
        max_response_bytes=100,
        timeout_seconds=4.0,
    )
    response = StdlibOneHopTransport(
        credential_provider=FixedCredentials(),
        connection_factory=factory,
    ).send(request)

    assert factory_args == [
        ("https", "api.example", 443, "93.184.216.34", 4.0)
    ]
    assert connection.request_args == (
        "GET",
        "/data",
        None,
        {
            "accept-encoding": "identity",
            "authorization": "Bearer do-not-record",
        },
    )
    assert raw.read_amounts == [101]
    assert response.body == b'{"value":7}'
    assert response.peer_ip == "93.184.216.34"
    assert response.header("set-cookie") is None
    assert response.header("link") == (
        '</license>; rel="license", </next>; rel="next"'
    )
    assert "do-not-record" not in repr(request)
    assert "do-not-record" not in repr(response)
    assert connection.closed


def test_live_transport_stops_after_the_response_limit() -> None:
    raw = FakeResponse(b"12345")
    connection = FakeConnection(raw)
    transport = StdlibOneHopTransport(
        connection_factory=lambda *_: connection,
    )

    with pytest.raises(ResponseLimitError) as raised:
        transport.send(
            HttpRequest(
                "https://api.example/data",
                approved_peer_ips=("93.184.216.34",),
                max_response_bytes=3,
            )
        )

    assert raw.read_amounts == [4]
    assert raised.value.consumed_bytes == 4
    assert connection.closed


def test_unconfigured_credential_profile_fails_closed() -> None:
    connection = FakeConnection(FakeResponse(b"{}"))
    transport = StdlibOneHopTransport(connection_factory=lambda *_: connection)

    with pytest.raises(CredentialProfileUnavailable, match="not configured"):
        transport.send(
            HttpRequest(
                "https://api.example/data",
                credential_profile_name="missing",
                approved_peer_ips=("93.184.216.34",),
            )
        )


def test_live_transport_requires_core_approved_peer_ips() -> None:
    connection = FakeConnection(FakeResponse(b"{}"))
    transport = StdlibOneHopTransport(connection_factory=lambda *_: connection)

    with pytest.raises(HttpPolicyError, match="approved peer"):
        transport.send(HttpRequest("https://api.example/data"))


def test_socket_resolver_returns_unique_ip_literals() -> None:
    def lookup(*args: object, **kwargs: object) -> list[tuple[object, ...]]:
        assert args == ("example.com", 443)
        assert kwargs == {"type": socket.SOCK_STREAM, "proto": socket.IPPROTO_TCP}
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0),
            ),
        ]

    assert SocketDnsResolver(lookup).resolve("example.com", 443, 1.0) == (
        "93.184.216.34",
        "2606:2800:220:1:248:1893:25c8:1946",
    )


def test_socket_resolver_enforces_its_deadline() -> None:
    def lookup(*args: object, **kwargs: object) -> list[tuple[object, ...]]:
        threading.Event().wait(0.1)
        return []

    with pytest.raises(TimeoutError, match="DNS resolution timed out"):
        SocketDnsResolver(lookup).resolve("example.com", 443, 0.01)


def test_default_http_connection_dials_only_the_approved_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    def connect(*args: object) -> FakeSocket:
        calls.append(args)
        return FakeSocket()

    monkeypatch.setattr(socket, "create_connection", connect)
    connection = _PinnedHTTPConnection(
        "public.example",
        80,
        approved_ip="93.184.216.34",
        timeout=3.0,
    )

    connection.connect()

    assert calls == [(('93.184.216.34', 80), 3.0, None)]
