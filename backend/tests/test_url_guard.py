"""커넥터 endpoint SSRF 가드 (connector-hub#3).

AgentToolbox 에서 옮겨 온 **보안 경계**라 테스트도 함께 옮긴다. 구현만 옮기고 테스트를
새로 쓰면 "옮기는 김에" 빠진 차단을 아무도 못 잡는다.

DNS 를 타는 검사는 이름 해석을 monkeypatch 해 네트워크 없이 돌린다.
"""

from __future__ import annotations

import ipaddress

import pytest

from core.mcp import url_guard
from core.mcp.url_guard import (
    ConnectorUrlNotAllowedError,
    _host_matches,
    _is_blocked_address,
    allowed_hosts,
    assert_url_allowed,
    check_url_without_dns,
    normalize_endpoint_url,
)
from core.settings import load_settings


@pytest.fixture
def hosts(monkeypatch):
    """`CONNECTOR_ALLOWED_HOSTS` 를 설정한다(설정 캐시까지 비운다)."""

    def _apply(value: str | None) -> None:
        if value is None:
            monkeypatch.delenv("CONNECTOR_ALLOWED_HOSTS", raising=False)
        else:
            monkeypatch.setenv("CONNECTOR_ALLOWED_HOSTS", value)
        load_settings.cache_clear()

    return _apply


@pytest.fixture
def resolves_to(monkeypatch):
    """호스트가 주어진 주소들로 해석되게 고정한다."""

    def _apply(*addresses: str) -> None:
        async def _fake(host: str, port: int) -> list[str]:
            return list(addresses)

        monkeypatch.setattr(url_guard, "_resolve_all", _fake)

    return _apply


# --- 형식 검사 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "not-a-url",
        # `startswith("http://")` 검사를 통과하던 host 없는 URL.
        "http://",
        "https:///path-only",
    ],
)
def test_normalize_rejects_non_http_or_hostless(raw: str) -> None:
    with pytest.raises(ConnectorUrlNotAllowedError):
        normalize_endpoint_url(raw)


def test_normalize_keeps_valid_url_and_strips_whitespace() -> None:
    assert (
        normalize_endpoint_url("  https://mcp.example.com/mcp  ") == "https://mcp.example.com/mcp"
    )


# --- allowlist -----------------------------------------------------------------


def test_allowed_hosts_parses_comma_separated(hosts) -> None:
    hosts(" mcp.example.com , .internal.corp ,, ")
    assert allowed_hosts() == ("mcp.example.com", ".internal.corp")


def test_allowed_hosts_empty_when_unset(hosts) -> None:
    hosts(None)
    assert allowed_hosts() == ()


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("mcp.example.com", True),
        ("MCP.EXAMPLE.COM", True),  # 대소문자 무시
        ("mcp.example.com.", True),  # 후행 점(FQDN) 무시
        ("other.example.com", False),
        # 하위 도메인 패턴은 정확한 경계로만 맞아야 한다.
        ("a.internal.corp", True),
        ("internal.corp", False),
        # 접미사 문자열 비교만 하면 통과해 버리는 대표 우회.
        ("evilinternal.corp", False),
    ],
)
def test_host_matches_boundaries(host: str, expected: bool) -> None:
    assert _host_matches(host, ("mcp.example.com", ".internal.corp")) is expected


def test_rejects_host_outside_allowlist(hosts) -> None:
    hosts("mcp.example.com")
    with pytest.raises(ConnectorUrlNotAllowedError):
        check_url_without_dns("https://attacker.example.net/mcp")


def test_allows_any_host_when_allowlist_empty(hosts) -> None:
    # 기본값으로 잠그면 이관해 온 커넥터가 일제히 죽으므로 allowlist 는 opt-in 이다.
    hosts(None)
    assert check_url_without_dns("https://anything.example.net/mcp")


# --- 주소 차단 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",  # 클라우드 metadata endpoint
        "fe80::1",
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
        "0.0.0.0",  # unspecified
    ],
)
def test_blocks_addresses_no_mcp_server_lives_on(address: str) -> None:
    """정상 용도가 없는 주소만 막는다 — 여기 목록이 차단의 전부다."""
    assert _is_blocked_address(ipaddress.ip_address(address)) is True


@pytest.mark.parametrize(
    "address",
    [
        "8.8.8.8",  # 인터넷 공개 MCP
        "2001:4860:4860::8888",
        "10.0.0.5",  # 사내 서버
        "172.16.3.4",
        "192.168.1.10",  # 개인 PC
        "fd00::1",
        "127.0.0.1",  # 로컬 개발 MCP
        "::1",
        "::ffff:127.0.0.1",  # IPv4-mapped — 매핑을 풀어도 loopback 이라 허용
    ],
)
def test_allows_where_mcp_servers_actually_run(address: str) -> None:
    """사내 서버·개인 PC·개발자 노트북 — MCP 서버가 실제로 뜨는 곳은 전부 통과한다.

    이걸 막으면 사내 자산 등록이라는 기능 목적 자체가 성립하지 않는다.
    """
    assert _is_blocked_address(ipaddress.ip_address(address)) is False


async def test_blocks_metadata_endpoint(hosts, resolves_to) -> None:
    hosts(None)
    resolves_to("169.254.169.254")
    with pytest.raises(ConnectorUrlNotAllowedError):
        await assert_url_allowed("http://169.254.169.254/latest/meta-data/")


async def test_blocks_when_any_record_is_internal(hosts, resolves_to) -> None:
    # 여러 A 레코드 중 하나만 걸려도 거부해야 한다 — 첫 레코드만 보면 우회가 통한다.
    hosts(None)
    resolves_to("93.184.216.34", "169.254.169.254")
    with pytest.raises(ConnectorUrlNotAllowedError):
        await assert_url_allowed("https://split-horizon.example.com/mcp")


async def test_passes_for_public_host(hosts, resolves_to) -> None:
    hosts(None)
    resolves_to("93.184.216.34")
    await assert_url_allowed("https://mcp.example.com/mcp")


async def test_rejects_unresolvable_host(hosts, monkeypatch) -> None:
    hosts(None)

    async def _boom(host: str, port: int) -> list[str]:
        raise ConnectorUrlNotAllowedError(f"cannot resolve host: {host}")

    monkeypatch.setattr(url_guard, "_resolve_all", _boom)
    with pytest.raises(ConnectorUrlNotAllowedError):
        await assert_url_allowed("https://nx.example.invalid/mcp")


async def test_allowlist_is_checked_before_dns(hosts, monkeypatch) -> None:
    # allowlist 밖이면 DNS 를 아예 타지 않아야 한다(미해석 호스트로 resolver 를 못 흔들게).
    hosts("mcp.example.com")

    async def _never(host: str, port: int) -> list[str]:  # pragma: no cover - 호출되면 실패
        raise AssertionError("allowlist 를 통과하기 전에 DNS 를 조회했다")

    monkeypatch.setattr(url_guard, "_resolve_all", _never)
    with pytest.raises(ConnectorUrlNotAllowedError):
        await assert_url_allowed("https://attacker.example.net/mcp")


async def test_localhost_mcp_server_is_allowed(hosts, resolves_to) -> None:
    """로컬에서 띄운 MCP 서버가 설정 없이 그대로 등록돼야 한다."""
    hosts(None)
    resolves_to("127.0.0.1")
    await assert_url_allowed("http://localhost:3002/mcp")


async def test_internal_server_is_allowed(hosts, resolves_to) -> None:
    """사내 서버(사설 IP)도 설정 없이 통과한다."""
    hosts(None)
    resolves_to("10.20.30.40")
    await assert_url_allowed("https://yyy.internal.corp/mcp")


# --- 리다이렉트 방어 -------------------------------------------------------------


async def test_guarded_transport_blocks_internal_redirect_target(hosts, monkeypatch) -> None:
    """리다이렉트 목적지도 같은 검사를 받아야 한다.

    `follow_redirects=True` 라 최초 URL 만 검사하면 외부 호스트가 302 로 내부 주소를 던지는
    경로가 그대로 열린다. transport 계층에서 요청마다 걸리는지 직접 확인한다.
    """
    import httpx2

    hosts(None)

    async def _fake(host: str, port: int) -> list[str]:
        return ["169.254.169.254"] if host == "internal.example.com" else ["93.184.216.34"]

    monkeypatch.setattr(url_guard, "_resolve_all", _fake)

    transport = url_guard.guarded_transport()
    request = httpx2.Request("GET", "https://internal.example.com/mcp")
    with pytest.raises(ConnectorUrlNotAllowedError):
        await transport.handle_async_request(request)
