"""커넥터 endpoint URL SSRF 가드.

AgentToolbox `core/connector/url_guard.py` 에서 **동작을 바꾸지 않고** 옮겨 왔다(연혁:
AgentToolbox #2360). 보안 경계라 "옮기면서 정리" 를 하지 않는다 — 정리한 줄 하나가
차단을 한 겹 걷어내는 일이 이 부류에서 실제로 일어난다. 옮기며 바뀐 것은 설정을 읽는
곳(`core.settings`)과 `load_settings` 가 캐시된다는 점뿐이다.

사용자가 임의 URL 을 넣으면 서버가 그 주소로 직접 접속한다(preview·등록·liveness cron).

**사내 자산 등록이 이 기능의 목적이라 사설 주소·loopback 은 막지 않는다.** MCP 서버는
사내 서버·개인 PC·개발자 노트북에 뜨고 그 주소는 전부 사설이거나 loopback 이다. 그래서
"내부망은 위험" 식의 광범위한 차단은 기능 자체를 부정하게 된다 — 등급을 나눠 설정으로
고르게 해 봤지만(구 `CONNECTOR_ADDRESS_POLICY`) 환경마다 다른 값을 골라야 해서 설정 사고만
늘었다. 지금은 **정상 용도가 없는 주소만** 무조건 막는다.

**두 겹으로 막는다:**

1. **호스트 allowlist** — `CONNECTOR_ALLOWED_HOSTS` 에 지정한 호스트만 허용. **비어 있으면
   이 겹은 통과한다** — 기본값으로 잠그면 이관해 온 커넥터가 일제히 죽기 때문이다.
   호스트를 제한해야 할 때 쓰는 유일한 레버다.
2. **주소 차단** — 호스트를 해석한 **모든** A/AAAA 레코드가 link-local(`169.254.169.254`
   클라우드 metadata)·multicast·reserved·unspecified 가 아니어야 한다. 일부만 검사하면
   여러 A 레코드 중 하나를 그런 주소로 심는 우회가 통한다.

**리다이렉트도 같은 검사를 받는다.** `follow_redirects=True` 라 스키마·접속 전 검사만으로는
외부 호스트가 302 로 던지는 내부 주소를 못 막는다. `guarded_transport()` 를 httpx 클라이언트에
끼우면 최초 요청과 모든 리다이렉트 목적지가 동일한 검사를 통과해야 한다.

**남는 위험(문서화):** 인증 사용자가 실패 사유로 내부망 host:port 생존 여부를 알아내는
포트 스캔은 가능하다 — 그래서 API 로 나가는 사유는 `client.py` 가 코드로 뭉갠다. 커넥터는
MCP 핸드셰이크만 하므로(`initialize`→`list_tools`) 응답 본문은 읽히지 않는다. DNS
rebinding(검사 시점과 실제 연결 시점 사이에 응답이 바뀌는 것)도 막지 못하는데, 사설 주소가
이미 허용이라 그것으로 추가로 닿을 수 있는 곳은 위 차단 목록뿐이다.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from core.settings import load_settings

if TYPE_CHECKING:
    from collections.abc import Iterable

# 허용 scheme — stdio 커맨드·file:// 등을 차단한다.
_ALLOWED_SCHEMES: tuple[str, ...] = ("http", "https")


class ConnectorUrlNotAllowedError(ValueError):
    """endpoint URL 이 형식·allowlist·주소 검사 중 하나를 통과하지 못했다.

    사유 문자열은 로그와 내부 처리용이다. 사용자에게는 `client.py` 가 코드로 바꿔
    내보낸다 — 어떤 내부 IP 로 해석됐는지는 여기서도 문자열에 넣지 않는다.
    """


def normalize_endpoint_url(raw: str) -> str:
    """URL 형식만 검사하고 정규화한다(DNS 조회 없음 — Pydantic validator 용).

    scheme 이 http(s) 이고 host 가 있어야 한다. 문자열 `startswith("http://")` 검사는
    `http://` 로 시작하기만 하면 통과해 host 가 빈 URL 도 받아들인다.
    """
    trimmed = (raw or "").strip()
    if not trimmed:
        raise ConnectorUrlNotAllowedError("endpoint URL is empty")

    try:
        parsed = urlparse(trimmed)
    except ValueError as exc:  # 대괄호 불일치 IPv6 리터럴 등
        raise ConnectorUrlNotAllowedError("endpoint URL is malformed") from exc

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ConnectorUrlNotAllowedError("must be an http(s) URL")

    try:
        host = parsed.hostname
    except ValueError as exc:
        raise ConnectorUrlNotAllowedError("endpoint URL is malformed") from exc
    if not host:
        raise ConnectorUrlNotAllowedError("endpoint URL has no host")

    return trimmed


def allowed_hosts() -> tuple[str, ...]:
    """`CONNECTOR_ALLOWED_HOSTS` 를 파싱한다(쉼표 구분, 소문자 정규화).

    빈 튜플이면 allowlist 미구성 — 호스트 검사를 건너뛴다(주소 검사는 계속 적용).
    """
    raw = load_settings().connector_allowed_hosts
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def _host_matches(host: str, patterns: Iterable[str]) -> bool:
    """호스트가 allowlist 항목과 맞는지 — 정확 일치 또는 `.example.com` 형태의 하위 도메인.

    `.example.com` 은 `mcp.example.com` 에 맞고 `example.com` 자체·`evilexample.com` 에는
    맞지 않는다(접미사 문자열 비교만 하면 `evilexample.com` 이 통과한다).
    """
    h = host.lower().rstrip(".")
    for pattern in patterns:
        if pattern.startswith("."):
            if h.endswith(pattern):
                return True
        elif h == pattern:
            return True
    return False


def _is_blocked_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """**MCP 서버가 뜰 수 없는 주소**인가 — 그것만 거부한다.

    사설 대역·loopback 은 허용한다. MCP 서버는 사내 서버·개인 PC·개발자 노트북에 뜨고
    그 주소는 전부 사설이거나 loopback 이다. 그걸 막으면 이 기능 자체가 성립하지 않는다.

    남기는 차단은 정상 용도가 **없는** 것들뿐이라 설정으로 열 이유가 없다:
    link-local(`169.254.169.254` 클라우드 metadata)·multicast(그룹 주소)·reserved·
    unspecified.

    loopback 을 먼저 통과시키는 이유는 `::1` 이 `is_reserved` 에도 걸리기 때문이다.
    IPv4-mapped IPv6(`::ffff:127.0.0.1`)는 매핑을 풀어 원래 v4 주소로 판정한다.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped

    if ip.is_loopback:
        return False

    return bool(
        ip.is_link_local  # 169.254/16 — 클라우드 metadata endpoint
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _resolve_all(host: str, port: int) -> list[str]:
    """호스트의 모든 A/AAAA 를 해석한다. 실패하면 도달 불가로 간주해 거부한다."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ConnectorUrlNotAllowedError(f"cannot resolve host: {host}") from exc
    return [str(info[4][0]) for info in infos]


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def check_url_without_dns(raw: str) -> str:
    """형식 + 호스트 allowlist 만 검사하고 정규화한다(DNS 조회 없음).

    요청 스레드에서 DNS 를 기다리지 않고 422 로 즉시 되돌릴 수 있는 것만 본다. 주소 검사는
    접속 직전 `assert_url_allowed` 가 맡는다.
    """
    normalized = normalize_endpoint_url(raw)
    patterns = allowed_hosts()
    if patterns:
        host = urlparse(normalized).hostname or ""
        if not _host_matches(host, patterns):
            raise ConnectorUrlNotAllowedError(f"host is not allowed: {host}")
    return normalized


async def assert_url_allowed(raw: str) -> None:
    """endpoint URL 전체 검사 — 형식 → allowlist → 해석된 모든 주소.

    접속 직전(`_open_session` 진입)과 리다이렉트마다(`guarded_transport`) 호출된다.
    통과하면 조용히 반환하고, 걸리면 ConnectorUrlNotAllowedError 를 던진다.
    """
    normalized = check_url_without_dns(raw)
    parsed = urlparse(normalized)
    host = parsed.hostname or ""

    try:
        port = parsed.port or _default_port(parsed.scheme)
    except ValueError as exc:  # 범위 밖 포트 문자열
        raise ConnectorUrlNotAllowedError("endpoint URL has an invalid port") from exc

    for address in await _resolve_all(host, port):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:  # pragma: no cover - getaddrinfo 가 비-IP 를 줄 일은 없다
            raise ConnectorUrlNotAllowedError(f"host is not allowed: {host}") from None
        if _is_blocked_address(ip):
            # 어떤 내부 주소로 해석됐는지는 알리지 않는다 — 그 자체가 내부망 정보다.
            raise ConnectorUrlNotAllowedError(f"host resolves to a blocked address: {host}")


# 가드 transport 클래스 메모 — httpx2 는 지연 import 라 클래스도 최초 호출 때 만든다.
_GUARDED_TRANSPORT_CLS: Any = None


def _guarded_transport_cls() -> Any:
    """가드 transport 클래스를 한 번만 만들어 재사용한다(httpx2 import 는 지연).

    client.py 와 같은 이유로 httpx2 를 모듈 최상단에서 import 하지 않는다 — 대신 클래스를
    메모해 클라이언트를 만들 때마다 새 클래스 객체가 생기지 않게 한다.
    """
    global _GUARDED_TRANSPORT_CLS
    if _GUARDED_TRANSPORT_CLS is None:
        import httpx2

        class _GuardedTransport(httpx2.AsyncHTTPTransport):
            async def handle_async_request(self, request: Any) -> Any:
                await assert_url_allowed(str(request.url))
                return await super().handle_async_request(request)

        _GUARDED_TRANSPORT_CLS = _GuardedTransport
    return _GUARDED_TRANSPORT_CLS


def guarded_transport() -> Any:
    """모든 요청 URL 을 `assert_url_allowed` 로 검사하는 httpx AsyncHTTPTransport.

    리다이렉트 방어의 핵심 — httpx 는 리다이렉트를 따라갈 때도 이 transport 를 거치므로
    최초 요청뿐 아니라 목적지마다 재검사가 걸린다. 스키마·접속 전 검사만으로는 외부
    호스트가 302 로 내부 주소를 던지는 경로를 막을 수 없다.

    `trust_env=False` 를 transport 에도 직접 건다. 명시적 transport 를 넘기면 클라이언트의
    `trust_env` 가 transport 까지 내려가지 않는데(기본값이 True), 커넥터 아웃바운드는 사내
    프록시·env 를 타지 않는다는 것이 이 경로의 전제다(client.py 참조).
    """
    return _guarded_transport_cls()(trust_env=False)
