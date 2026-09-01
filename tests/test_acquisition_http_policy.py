from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from research_harness.acquisition.http_policy import (
    MAX_REDIRECTS,
    ROBOTS_PARSE_LIMIT_BYTES,
    HttpPolicyError,
    HttpRequest,
    HttpResponse,
    OneHopTransport,
    UnsafeAddressError,
    UnsafeUrlError,
    canonical_http_url,
    evaluate_robots,
    parse_robots,
    require_bounded_response,
    require_public_ip,
    resolve_public_endpoint,
    resolve_redirect,
    robots_network_error_decision,
    robots_response_decision,
    validate_public_peer,
)


class ScriptedTransport:
    def __init__(self, *responses: HttpResponse) -> None:
        self.responses = list(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class ScriptedResolver:
    def __init__(self, answers: dict[tuple[str, int], tuple[str, ...]]) -> None:
        self.answers = answers
        self.queries: list[tuple[str, int]] = []

    def resolve(
        self,
        hostname: str,
        port: int,
        timeout_seconds: float,
    ) -> tuple[str, ...]:
        self.queries.append((hostname, port))
        return self.answers[(hostname, port)]


def test_one_hop_types_are_immutable_and_scriptable() -> None:
    response = HttpResponse(
        status=200,
        peer_ip="8.8.8.8",
        headers=(("content-type", "text/plain"),),
        body=b"ok",
    )
    transport: OneHopTransport = ScriptedTransport(response)
    request = HttpRequest(
        url="https://example.com/",
        headers=(("user-agent", "ResearchHarnessBot"),),
        credential_profile_name="public-api",
    )

    assert transport.send(request) == response
    assert response.header("Content-Type") == "text/plain"
    with pytest.raises(FrozenInstanceError):
        request.url = "https://elsewhere.example/"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/data",
        "https://user:secret@example.com/data",
        "https://example.com/data#fragment",
        "https://example.com/a/../private",
        "https://example.com/data?access_token=secret",
        "https://example.com/data?X-Amz-Signature=secret",
        "https://example.com/data?x-api-key=secret",
        "https://example.com/data?subscription-key=secret",
        "https://example.com/data?X-Amz-Credential=AKIA",
        "https://example.com\\@127.0.0.1/data",
    ],
)
def test_url_policy_rejects_ambiguous_or_secret_bearing_urls(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        canonical_http_url(url)


def test_url_policy_returns_one_canonical_http_form() -> None:
    assert canonical_http_url("HTTPS://ExAmPle.COM:443/%7edata?q=%62") == (
        "https://example.com/~data?q=b"
    )
    assert canonical_http_url("https://bücher.example/관측") == (
        "https://xn--bcher-kva.example/%EA%B4%80%EC%B8%A1"
    )


@pytest.mark.parametrize(
    "header",
    [
        ("Authorization", "Bearer secret"),
        ("Cookie", "session=secret"),
        ("Proxy-Authorization", "secret"),
        ("X-API-Key", "secret"),
        ("Set-Cookie", "session=secret"),
    ],
)
def test_typed_http_messages_never_retain_raw_secret_headers(header: tuple[str, str]) -> None:
    with pytest.raises(HttpPolicyError, match="secret headers"):
        HttpRequest("https://example.com/", (header,))
    with pytest.raises(HttpPolicyError, match="secret headers"):
        HttpResponse(200, "8.8.8.8", (header,), b"")


@pytest.mark.parametrize(
    ("max_response_bytes", "timeout_seconds"),
    [
        (-1, 1.0),
        (1, 0.0),
        (1, -1.0),
        (1, float("nan")),
        (1, float("inf")),
    ],
)
def test_request_requires_physical_response_bounds(
    max_response_bytes: int,
    timeout_seconds: float,
) -> None:
    with pytest.raises(HttpPolicyError):
        HttpRequest(
            "https://example.com/",
            max_response_bytes=max_response_bytes,
            timeout_seconds=timeout_seconds,
        )


def test_core_rejects_a_transport_that_ignores_the_response_limit() -> None:
    request = HttpRequest("https://example.com/", max_response_bytes=2)
    response = HttpResponse(200, "8.8.8.8", (), b"three")

    with pytest.raises(HttpPolicyError, match="byte limit"):
        require_bounded_response(request, response)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.1.1",
        "224.0.0.1",
        "240.0.0.1",
        "0.0.0.0",
        "::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "::",
    ],
)
def test_non_public_ipv4_and_ipv6_addresses_are_rejected(address: str) -> None:
    with pytest.raises(UnsafeAddressError, match="globally routable"):
        require_public_ip(address)


def test_dns_and_connected_peer_must_both_match_public_resolution() -> None:
    resolver = ScriptedResolver(
        {("api.example", 443): ("8.8.8.8", "2606:4700:4700::1111")}
    )

    endpoint = resolve_public_endpoint("https://api.example/data", resolver)

    assert resolver.queries == [("api.example", 443)]
    assert validate_public_peer("8.8.8.8", endpoint) == "8.8.8.8"
    with pytest.raises(UnsafeAddressError, match="approved DNS"):
        validate_public_peer("1.1.1.1", endpoint)
    with pytest.raises(UnsafeAddressError, match="globally routable"):
        validate_public_peer("127.0.0.1", endpoint)


def test_one_private_dns_answer_rejects_the_whole_endpoint() -> None:
    resolver = ScriptedResolver(
        {("mixed.example", 443): ("8.8.8.8", "192.168.1.10")}
    )

    with pytest.raises(UnsafeAddressError, match="globally routable"):
        resolve_public_endpoint("https://mixed.example/", resolver)


def test_request_rejects_non_public_or_noncanonical_approved_peers() -> None:
    with pytest.raises(UnsafeAddressError):
        HttpRequest(
            "https://example.com/",
            approved_peer_ips=("127.0.0.1",),
        )
    with pytest.raises(HttpPolicyError, match="canonical"):
        HttpRequest(
            "https://example.com/",
            approved_peer_ips=("8.8.8.8", "8.8.8.8"),
        )


def test_redirects_resolve_relative_and_cross_origin_locations() -> None:
    assert MAX_REDIRECTS == 5
    assert resolve_redirect("https://example.com/a/b", "../data?q=1") == (
        "https://example.com/data?q=1"
    )
    assert resolve_redirect("https://example.com/a", "HTTPS://OTHER.EXAMPLE:443/x") == (
        "https://other.example/x"
    )


@pytest.mark.parametrize(
    "location",
    [
        "https://user:secret@other.example/data",
        "//other.example/data?token=secret",
        "https://other.example/data#fragment",
        "\r\nhttps://other.example/data",
    ],
)
def test_redirects_reapply_the_full_url_policy(location: str) -> None:
    with pytest.raises(UnsafeUrlError):
        resolve_redirect("https://example.com/start", location)


def test_https_redirect_cannot_downgrade_to_plain_http() -> None:
    with pytest.raises(UnsafeUrlError, match="downgrade"):
        resolve_redirect("https://example.com/start", "http://example.com/final")


RFC_SIMPLE = b"""\
User-Agent: *
Disallow: *.gif$
Disallow: /example/
Allow: /publications/

User-Agent: foobot
Disallow:/
Allow:/example/page.html
Allow:/example/allowed.gif

User-Agent: barbot
User-Agent: bazbot
Disallow: /example/page.html

User-Agent: quxbot
"""


@pytest.mark.parametrize(
    ("agent", "path", "allowed"),
    [
        ("otherbot", "/image.gif", False),
        ("otherbot", "/example/page.html", False),
        ("otherbot", "/publications/index.html", True),
        ("foobot", "/example/page.html", True),
        ("foobot", "/example/allowed.gif", True),
        ("foobot", "/elsewhere", False),
        ("barbot", "/example/page.html", False),
        ("bazbot", "/elsewhere", True),
        ("quxbot", "/anything", True),
    ],
)
def test_rfc_9309_simple_example(agent: str, path: str, allowed: bool) -> None:
    decision = evaluate_robots(
        parse_robots(RFC_SIMPLE),
        user_agent=agent,
        target_url=f"https://example.com{path}",
    )

    assert decision.allowed is allowed


def test_longest_rule_and_allow_tie_win() -> None:
    policy = parse_robots(
        b"""\
User-Agent: foobot
Allow: /example/page/
Disallow: /example/page/disallowed.gif
Disallow: /same
Allow: /same
"""
    )

    longest = evaluate_robots(
        policy,
        user_agent="foobot",
        target_url="https://example.com/example/page/disallowed.gif",
    )
    tie = evaluate_robots(
        policy,
        user_agent="foobot",
        target_url="https://example.com/same",
    )

    assert longest.status == "denied"
    assert tie.status == "allowed"


def test_exact_user_agent_groups_merge_case_insensitively() -> None:
    policy = parse_robots(
        b"""\
User-Agent: bot
Disallow: /
User-Agent: researchbot
Allow: /public
User-Agent: ResearchBot
Disallow: /public/private
"""
    )

    public = evaluate_robots(
        policy,
        user_agent="ResearchBot",
        target_url="https://example.com/public/index",
    )
    private = evaluate_robots(
        policy,
        user_agent="ResearchBot",
        target_url="https://example.com/public/private/data",
    )

    assert public.allowed
    assert not private.allowed


def test_product_token_does_not_substring_match_a_robots_group() -> None:
    policy = parse_robots(b"User-agent: bot\nDisallow: /\n")

    decision = evaluate_robots(
        policy,
        user_agent="ResearchBot",
        target_url="https://example.com/data",
    )

    assert decision.status == "allowed"


@pytest.mark.parametrize(
    ("rule", "path"),
    [
        ("/foo/bar/%E3%83%84", "/foo/bar/ツ"),
        ("/foo/bar/%62%61%7A", "/foo/bar/baz"),
        ("/path/file-with-a-%2A.html", "/path/file-with-a-*.html"),
        ("/path/foo-%24", "/path/foo-$"),
    ],
)
def test_percent_encoded_and_utf8_octets_match(rule: str, path: str) -> None:
    policy = parse_robots(f"User-Agent: bot\nDisallow: {rule}\n".encode())

    decision = evaluate_robots(
        policy,
        user_agent="bot",
        target_url=canonical_http_url(f"https://example.com{path}"),
    )

    assert decision.status == "denied"


def test_robots_parser_keeps_parseable_rules_and_enforces_a_safe_minimum_limit() -> None:
    policy = parse_robots(
        b"User-Agent: bot\n\xff\nDisallow: /private\nSitemap: /map.xml\n"
    )

    assert not evaluate_robots(
        policy,
        user_agent="bot",
        target_url="https://example.com/private",
    ).allowed
    assert ROBOTS_PARSE_LIMIT_BYTES >= 500 * 1024
    with pytest.raises(HttpPolicyError, match="500 KiB"):
        parse_robots(b"", max_bytes=499 * 1024)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(200, "denied"), (404, "unavailable"), (503, "denied")],
)
def test_robots_http_status_policy(status: int, expected: str) -> None:
    response = HttpResponse(
        status,
        "8.8.8.8",
        (("content-type", "text/plain"),),
        b"User-Agent: bot\nDisallow: /private\n",
    )

    decision = robots_response_decision(
        response,
        user_agent="bot",
        target_url="https://example.com/private",
    )

    assert decision.status == expected


def test_robots_network_error_is_complete_disallow() -> None:
    assert robots_network_error_decision().status == "denied"
    assert not robots_network_error_decision().allowed
