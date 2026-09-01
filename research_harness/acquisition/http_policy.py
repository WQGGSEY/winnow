from __future__ import annotations

import ipaddress
import math
import re
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import parse_qsl, quote, urljoin, urlsplit, urlunsplit


MAX_REDIRECTS = 5
ROBOTS_PARSE_LIMIT_BYTES = 512 * 1024

_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_PRODUCT_TOKEN_RE = re.compile(r"^[A-Za-z_-]+$")
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
    }
)
_SENSITIVE_QUERY_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "credential",
        "key",
        "password",
        "passwd",
        "secret",
        "sig",
        "signature",
        "token",
        "x_amz_credential",
        "x_amz_security_token",
        "x_amz_signature",
    }
)
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


class HttpPolicyError(ValueError):
    pass


class UnsafeUrlError(HttpPolicyError):
    pass


class UnsafeAddressError(HttpPolicyError):
    pass


Header = tuple[str, str]


def _validate_headers(headers: tuple[Header, ...]) -> None:
    if not isinstance(headers, tuple):
        raise HttpPolicyError("headers must be an immutable tuple")
    seen: set[str] = set()
    for item in headers:
        if not isinstance(item, tuple) or len(item) != 2:
            raise HttpPolicyError("each header must be an immutable name-value pair")
        name, value = item
        lowered = name.lower()
        if not _HEADER_NAME_RE.fullmatch(name) or lowered in seen:
            raise HttpPolicyError("header name is invalid or duplicated")
        if lowered in _SENSITIVE_HEADERS:
            raise HttpPolicyError("raw secret headers are forbidden")
        if not isinstance(value, str) or any(character in value for character in "\r\n\0"):
            raise HttpPolicyError("header value is invalid")
        seen.add(lowered)


def _normalize_percent_encoding(value: str, *, safe: str) -> str:
    encoded = quote(value, safe=safe + "%")
    output: list[str] = []
    index = 0
    while index < len(encoded):
        if encoded[index] != "%":
            output.append(encoded[index])
            index += 1
            continue
        escape = encoded[index : index + 3]
        if _PERCENT_ESCAPE_RE.fullmatch(escape) is None:
            raise UnsafeUrlError("URL contains an invalid percent escape")
        octet = int(escape[1:], 16)
        character = chr(octet)
        output.append(character if character in _UNRESERVED else f"%{octet:02X}")
        index += 3
    return "".join(output)


def _validate_query_has_no_secret(query: str) -> None:
    for name, _ in parse_qsl(query, keep_blank_values=True, strict_parsing=False):
        normalized = name.strip().lower().replace("-", "_")
        if normalized in _SENSITIVE_QUERY_NAMES or normalized.endswith(
            (
                "_auth",
                "_credential",
                "_key",
                "_password",
                "_secret",
                "_signature",
                "_token",
            )
        ):
            raise UnsafeUrlError("URL query contains a credential-like parameter")


def canonical_http_url(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise UnsafeUrlError("URL must be non-empty canonical text")
    if "\\" in value or any(ord(character) < 0x20 for character in value):
        raise UnsafeUrlError("URL contains an unsafe character")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        raise UnsafeUrlError("URL scheme must be lowercase http or https")
    if not parts.netloc or parts.hostname is None:
        raise UnsafeUrlError("URL requires an authority and host")
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrlError("URL userinfo is forbidden")
    if parts.fragment:
        raise UnsafeUrlError("URL fragments are forbidden")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("URL port is invalid") from exc
    try:
        parsed_ip = ipaddress.ip_address(parts.hostname)
    except ValueError:
        try:
            hostname = parts.hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise UnsafeUrlError("URL hostname is invalid") from exc
        if not hostname or hostname.endswith(".") or "_" in hostname:
            raise UnsafeUrlError("URL hostname is not canonical")
    else:
        hostname = parsed_ip.compressed
        if parsed_ip.version == 6:
            hostname = f"[{hostname}]"
    default_port = 80 if parts.scheme == "http" else 443
    authority = hostname if port in {None, default_port} else f"{hostname}:{port}"
    path = _normalize_percent_encoding(
        parts.path or "/",
        safe="/-._~!$&'()*+,;=:@",
    )
    decoded_segments = [
        re.sub(
            r"%([0-9A-Fa-f]{2})",
            lambda match: chr(int(match.group(1), 16)),
            segment,
        )
        for segment in path.split("/")
    ]
    if any(segment in {".", ".."} for segment in decoded_segments):
        raise UnsafeUrlError("URL dot segments are forbidden")
    query = _normalize_percent_encoding(
        parts.query,
        safe="-._~!$&'()*+,;=:@/?",
    )
    _validate_query_has_no_secret(query)
    return urlunsplit((parts.scheme, authority, path, query, ""))


@dataclass(frozen=True, slots=True)
class HttpRequest:
    url: str
    headers: tuple[Header, ...] = ()
    credential_profile_name: str | None = None
    max_response_bytes: int = 1024 * 1024
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if canonical_http_url(self.url) != self.url:
            raise UnsafeUrlError("request URL is not canonical")
        _validate_headers(self.headers)
        if self.credential_profile_name is not None and (
            not self.credential_profile_name.strip()
            or self.credential_profile_name != self.credential_profile_name.strip()
        ):
            raise HttpPolicyError("credential profile name is invalid")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or self.max_response_bytes < 0
        ):
            raise HttpPolicyError("response byte limit is invalid")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise HttpPolicyError("request timeout is invalid")


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    peer_ip: str
    headers: tuple[Header, ...]
    body: bytes

    def __post_init__(self) -> None:
        if isinstance(self.status, bool) or not 100 <= self.status <= 599:
            raise HttpPolicyError("HTTP status is invalid")
        try:
            ipaddress.ip_address(self.peer_ip)
        except ValueError as exc:
            raise UnsafeAddressError("response peer is not an IP address") from exc
        _validate_headers(self.headers)
        if not isinstance(self.body, bytes):
            raise HttpPolicyError("response body must be bytes")

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        return next((value for key, value in self.headers if key.lower() == lowered), None)


class OneHopTransport(Protocol):
    def send(self, request: HttpRequest) -> HttpResponse: ...


class DnsResolver(Protocol):
    def resolve(self, hostname: str, port: int) -> tuple[str, ...]: ...


def require_bounded_response(
    request: HttpRequest,
    response: HttpResponse,
) -> HttpResponse:
    if len(response.body) > request.max_response_bytes:
        raise HttpPolicyError("transport exceeded the response byte limit")
    return response


@dataclass(frozen=True, slots=True)
class ResolvedEndpoint:
    url: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


def require_public_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise UnsafeAddressError("address is not an IP literal") from exc
    if (
        not address.is_global
        or address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise UnsafeAddressError("address is not globally routable")
    return address.compressed


def resolve_public_endpoint(url: str, resolver: DnsResolver) -> ResolvedEndpoint:
    canonical = canonical_http_url(url)
    parts = urlsplit(canonical)
    assert parts.hostname is not None
    port = parts.port or (80 if parts.scheme == "http" else 443)
    try:
        literal = ipaddress.ip_address(parts.hostname)
    except ValueError:
        raw_addresses = resolver.resolve(parts.hostname, port)
    else:
        raw_addresses = (literal.compressed,)
    if not isinstance(raw_addresses, tuple) or not raw_addresses:
        raise UnsafeAddressError("DNS resolution returned no addresses")
    addresses = tuple(dict.fromkeys(require_public_ip(item) for item in raw_addresses))
    return ResolvedEndpoint(canonical, parts.hostname, port, addresses)


def validate_public_peer(peer_ip: str, endpoint: ResolvedEndpoint) -> str:
    peer = require_public_ip(peer_ip)
    if peer not in endpoint.addresses:
        raise UnsafeAddressError("response peer was not in the approved DNS result")
    return peer


def resolve_redirect(current_url: str, location: str) -> str:
    if not isinstance(location, str) or not location or any(
        character in location for character in "\r\n\0"
    ):
        raise UnsafeUrlError("redirect Location is invalid")
    return canonical_http_url(urljoin(canonical_http_url(current_url), location))


@dataclass(frozen=True, slots=True)
class RobotsRule:
    allow: bool
    pattern: str


@dataclass(frozen=True, slots=True)
class RobotsGroup:
    user_agents: tuple[str, ...]
    rules: tuple[RobotsRule, ...]


@dataclass(frozen=True, slots=True)
class RobotsPolicy:
    groups: tuple[RobotsGroup, ...]


@dataclass(frozen=True, slots=True)
class RobotsDecision:
    status: Literal["allowed", "denied", "unavailable"]
    matched_rule: RobotsRule | None = None

    @property
    def allowed(self) -> bool:
        return self.status != "denied"


def parse_robots(
    body: bytes,
    *,
    max_bytes: int = ROBOTS_PARSE_LIMIT_BYTES,
) -> RobotsPolicy:
    if not isinstance(body, bytes):
        raise HttpPolicyError("robots body must be bytes")
    if max_bytes < 500 * 1024:
        raise HttpPolicyError("robots parsing limit must be at least 500 KiB")
    groups: list[RobotsGroup] = []
    agents: list[str] = []
    rules: list[RobotsRule] = []
    saw_rule = False

    def finish_group() -> None:
        nonlocal agents, rules, saw_rule
        if agents:
            groups.append(RobotsGroup(tuple(agents), tuple(rules)))
        agents = []
        rules = []
        saw_rule = False

    for raw_line in body[:max_bytes].splitlines():
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            continue
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip().lower()
        value = raw_value.strip()
        if key == "user-agent":
            if not value or (value != "*" and _PRODUCT_TOKEN_RE.fullmatch(value) is None):
                continue
            if agents and saw_rule:
                finish_group()
            agents.append(value)
            continue
        if key not in {"allow", "disallow"} or not agents:
            continue
        saw_rule = True
        if value:
            rules.append(RobotsRule(key == "allow", value))
    finish_group()
    return RobotsPolicy(tuple(groups))


def _matching_groups(policy: RobotsPolicy, user_agent: str) -> tuple[RobotsGroup, ...]:
    lowered = user_agent.lower()
    specific = [
        group
        for group in policy.groups
        for agent in group.user_agents
        if agent != "*" and agent.lower() == lowered
    ]
    if specific:
        return tuple(dict.fromkeys(specific))
    return tuple(group for group in policy.groups if "*" in group.user_agents)


def _normalize_match_value(value: str, *, pattern: bool) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "%" and _PERCENT_ESCAPE_RE.fullmatch(value[index : index + 3]):
            octet = int(value[index + 1 : index + 3], 16)
            character = chr(octet)
            output.append(character if character in _UNRESERVED else f"%{octet:02X}")
            index += 3
            continue
        character = value[index]
        if ord(character) > 127:
            output.extend(f"%{octet:02X}" for octet in character.encode("utf-8"))
        elif not pattern and character in {"*", "$"}:
            output.append(f"%{ord(character):02X}")
        else:
            output.append(character)
        index += 1
    return "".join(output)


def _rule_match(rule: RobotsRule, target: str) -> tuple[bool, int]:
    pattern = _normalize_match_value(rule.pattern, pattern=True)
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    expression = "^" + ".*".join(re.escape(part) for part in pattern.split("*"))
    if anchored:
        expression += "$"
    matched = re.match(expression, target) is not None
    octets = len(re.sub(r"%[0-9A-F]{2}", "x", pattern.replace("*", "")).encode("ascii"))
    return matched, octets


def evaluate_robots(
    policy: RobotsPolicy,
    *,
    user_agent: str,
    target_url: str,
) -> RobotsDecision:
    if not _PRODUCT_TOKEN_RE.fullmatch(user_agent):
        raise HttpPolicyError("robots product token is invalid")
    canonical = canonical_http_url(target_url)
    parts = urlsplit(canonical)
    if parts.path == "/robots.txt":
        return RobotsDecision("allowed")
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    normalized_target = _normalize_match_value(target, pattern=False)
    matches: list[tuple[int, RobotsRule]] = []
    for group in _matching_groups(policy, user_agent):
        for rule in group.rules:
            matched, length = _rule_match(rule, normalized_target)
            if matched:
                matches.append((length, rule))
    if not matches:
        return RobotsDecision("allowed")
    longest = max(length for length, _ in matches)
    winners = [rule for length, rule in matches if length == longest]
    winner = next((rule for rule in winners if rule.allow), winners[0])
    return RobotsDecision("allowed" if winner.allow else "denied", winner)


def robots_response_decision(
    response: HttpResponse,
    *,
    user_agent: str,
    target_url: str,
) -> RobotsDecision:
    if 200 <= response.status <= 299:
        return evaluate_robots(
            parse_robots(response.body),
            user_agent=user_agent,
            target_url=target_url,
        )
    if 400 <= response.status <= 499:
        return RobotsDecision("unavailable")
    return RobotsDecision("denied")


def robots_network_error_decision() -> RobotsDecision:
    return RobotsDecision("denied")


__all__ = [
    "DnsResolver",
    "HttpPolicyError",
    "HttpRequest",
    "HttpResponse",
    "MAX_REDIRECTS",
    "OneHopTransport",
    "ROBOTS_PARSE_LIMIT_BYTES",
    "ResolvedEndpoint",
    "RobotsDecision",
    "RobotsGroup",
    "RobotsPolicy",
    "RobotsRule",
    "UnsafeAddressError",
    "UnsafeUrlError",
    "canonical_http_url",
    "evaluate_robots",
    "parse_robots",
    "require_public_ip",
    "require_bounded_response",
    "resolve_public_endpoint",
    "resolve_redirect",
    "robots_network_error_decision",
    "robots_response_decision",
    "validate_public_peer",
]
