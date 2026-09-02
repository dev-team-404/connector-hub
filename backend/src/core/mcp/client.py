"""remote MCP 서버 라이브 접속 클라이언트.

AgentToolbox `core/connector/client.py` 에서 옮겨 왔다(연혁: AgentToolbox #2360). 공식
`mcp` SDK 로 remote(sse·streamable_http) MCP 서버에 접속해 `initialize` 핸드셰이크 후
tools 를 나열하거나 살아있는지만 확인한다. stdio 는 지원하지 않는다.

**옮기며 바꾼 것은 둘이다.**

1. **실패 사유를 코드로 뭉갠다.** 원본은 예외 문자열을 그대로 사용자에게 돌려줬다. 그
   문자열에는 리다이렉트 목적지·해석된 주소·사내 호스트명이 섞여 들어올 수 있고, 인증
   사용자가 그것으로 내부망을 훑을 수 있다(connector-hub#3 의 "endpoint 오류 원문과 내부
   주소를 사용자에게 그대로 노출하지 않는다"). 원문은 로그에만 남긴다.
2. **프로세스 전역 아웃바운드 동시성 상한을 둔다.** 원본은 워커 쪽에만 상한이 있어 API
   경로(미리보기·수동 새로고침)는 동시 요청 수만큼 MCP 세션이 열렸다. 세션 하나가
   소켓·DNS 스레드풀을 잡으므로 API 도 같은 상한 아래 둔다.

transport 폴백·ExceptionGroup 언랩·타임아웃 값 같은 **판정 로직은 그대로 둔다.** 이 부분은
실제 legacy 서버에서 겪은 사례로 굳어진 것이라, 옮기면서 다듬으면 그 사례가 되돌아온다.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any, Literal

from core.mcp.url_guard import (
    ConnectorUrlNotAllowedError,
    assert_url_allowed,
    guarded_transport,
)
from core.settings import load_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable


logger = logging.getLogger(__name__)

# remote 접속 총 타임아웃(초). initialize + list_tools 합산 상한. 사용자 대기 화면(상세 탭)
# 이라 짧게 잡는다.
DEFAULT_TIMEOUT_SEC = 10.0

#: 지원 transport 전체. 폴백은 이 목록 안에서만 일어난다(stdio 미지원).
#: 값은 DB `connectors.transport` 도메인과 **같은 어휘**다 — 두 어휘를 두면 폴백 결과를
#: 되기록할 때 번역이 하나 빠져 CHECK 위반이 된다(마이그레이션 0002 가 맞춘 이유).
TRANSPORTS: tuple[str, ...] = ("streamable_http", "sse")

#: 사용자에게 나가는 실패 사유. **여기 있는 문자열이 전부다** — 예외 원문은 섞지 않는다.
FailureCode = Literal[
    "timeout", "unreachable", "protocol_error", "url_not_allowed", "unsupported", "unavailable"
]

_FAILURE_MESSAGES: dict[str, str] = {
    "timeout": "endpoint 가 제한 시간 안에 응답하지 않았다",
    "unreachable": "endpoint 에 접속하지 못했다",
    "protocol_error": "endpoint 가 MCP 핸드셰이크에 응답하지 않았다",
    "url_not_allowed": "endpoint 주소가 허용되지 않는다",
    "unsupported": "지원하지 않는 transport 다",
    "unavailable": "지금은 endpoint 를 확인할 수 없다",
}


@dataclass(frozen=True)
class ProbeFailure:
    """사용자에게 보여도 되는 실패 사유. 코드로 판정하고 문구는 표시용이다."""

    code: FailureCode
    message: str

    @staticmethod
    def of(code: FailureCode) -> ProbeFailure:
        return ProbeFailure(code=code, message=_FAILURE_MESSAGES[code])


class ConnectorUnreachableError(Exception):
    """endpoint 도달·핸드셰이크 실패.

    `failure` 는 사용자에게 나가고 `detail` 은 로그에만 남는다. 둘을 한 문자열로 합치지
    않는 것이 이 클래스의 존재 이유다 — 합치면 어느 시점엔가 detail 이 응답에 실린다.
    """

    def __init__(self, failure: ProbeFailure, *, detail: str = "") -> None:
        super().__init__(failure.message)
        self.failure = failure
        self.detail = detail or failure.message


@dataclass(frozen=True)
class LiveTool:
    name: str
    description: str | None
    input_schema: dict[str, object] | None
    # MCP `annotations.readOnlyHint` — 이 도구가 상태를 바꾸지 않는지에 대한 **서버의 선언**.
    # 스펙상 선택 필드라 안 주는 서버가 많다. 그 경우 None 으로 두고 화면에서도 배지를 달지
    # 않는다 — 미선언을 write 로 단정하면 안전한 도구를 위험해 보이게 만든다.
    read_only: bool | None = None


@dataclass(frozen=True)
class ToolsFetch:
    """tools 나열 결과 + **실제로 성공한 transport**.

    폴백(_transport_candidates)이 동작하면 `transport` 는 호출자가 요청한 값과 다를 수
    있다. 호출부는 이 값을 DB 에 되기록해 다음 요청이 첫 시도에 맞히게 한다.
    """

    tools: list[LiveTool]
    transport: str


@dataclass(frozen=True)
class Liveness:
    """liveness 결과 + 성공한 transport(실패면 None) + 실패 사유(성공이면 None)."""

    healthy: bool
    transport: str | None
    failure: ProbeFailure | None = None


# ---- 아웃바운드 동시성 상한 -------------------------------------------------------------

#: 이벤트 루프별 semaphore. 루프를 키로 두는 이유는 `asyncio.Semaphore` 가 처음 대기한
#: 루프에 묶이기 때문이다 — 전역 하나로 두면 테스트(TestClient 는 블록마다 새 루프)와
#: 워커 재기동에서 "bound to a different event loop" 로 터진다.
_GATES: weakref.WeakKeyDictionary[Any, tuple[int, asyncio.Semaphore]] = weakref.WeakKeyDictionary()


def _outbound_gate() -> asyncio.Semaphore:
    """프로세스(정확히는 이벤트 루프) 전역 아웃바운드 상한.

    상한을 넘은 요청은 **거절하지 않고 기다린다.** 미리보기·새로고침은 사람이 누른 것이라
    거절보다 잠깐 느린 편이 낫고, 개별 시도에는 이미 타임아웃이 걸려 있어 무한정 쌓이지
    않는다. 워커의 배치 semaphore 와는 별개로 겹쳐 걸린다(둘 중 좁은 쪽이 실효 상한).
    """
    loop = asyncio.get_running_loop()
    limit = load_settings().connector_outbound_concurrency
    entry = _GATES.get(loop)
    if entry is None or entry[0] != limit:
        gate = asyncio.Semaphore(limit)
        _GATES[loop] = (limit, gate)
        return gate
    return entry[1]


# ---- 폴백 판정 ---------------------------------------------------------------------------


def _transport_candidates(transport: str) -> list[str]:
    """요청 transport 를 먼저, 나머지를 뒤에 둔 시도 순서.

    MCP 스펙은 endpoint **경로**를 규정하지 않는다 — `/sse`·`/mcp` 는 관례일 뿐이라 URL 만
    보고 transport 를 확정할 수 없다(루트 경로로 서비스하는 서버가 실제로 존재한다). 그래서
    URL 문자열 추측 대신 스펙의 backwards-compatibility 절이 권하는 **실제 시도 후 폴백**을
    쓴다: 요청값으로 먼저 붙어 보고, 서버가 그 transport 를 거절하면 반대편으로 한 번 더.
    """
    if transport not in TRANSPORTS:
        return [transport]
    return [transport, *(t for t in TRANSPORTS if t != transport)]


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    """ExceptionGroup 을 평평한 leaf 목록으로 푼다(그룹이 아니면 자기 자신 하나).

    MCP SDK 의 두 transport 는 내부에서 anyio TaskGroup 을 돌린다. 그 안에서 난 예외는
    호출부에 `ExceptionGroup` 으로 올라오는데, 이 그룹은 httpx 예외의 서브클래스가 아니고
    `str()` 도 "unhandled errors in a TaskGroup (1 sub-exception)" 라 원인이 통째로 가려진다.
    실제 원인을 보려면 반드시 풀어야 한다 — 중첩 그룹도 있으므로 재귀로 내려간다.
    """
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for sub in exc.exceptions:
            leaves.extend(_leaf_exceptions(sub))
        return leaves
    return [exc]


def _network_failure_types() -> Any:
    import httpx2

    return (
        httpx2.NetworkError
        | httpx2.TimeoutException
        | TimeoutError
        | OSError
        | ConnectorUrlNotAllowedError
    )


def _means_wrong_transport(exc: BaseException) -> bool:
    """ "서버는 응답했지만 이 transport 로는 대화가 안 된다" 인가 — 폴백 가치 판정.

    True 인 대표 사례: legacy SSE 서버에 streamable_http 로 POST 하면 405 가 오고, SDK 가
    그 HTTP 4xx(JSON-RPC 본문 없음)를 오류로 매핑한다. 반대 방향은 HTTPStatusError·
    RemoteProtocolError·SSEError 등으로 나타난다.

    False 로 두는 것(= 폴백 안 함): 연결 자체가 안 되거나 타임아웃인 경우. 죽은 endpoint 에
    반대편 transport 를 한 번 더 시도해봐야 같은 결과이고 사용자 대기만 2배가 된다. URL 가드
    거부도 마찬가지다 — transport 를 바꿔도 같은 주소라 같은 판정이 나온다.

    **ExceptionGroup 을 풀고 본다.** 안 풀면 SDK TaskGroup 이 감싼 ConnectError·ReadTimeout
    이 위 어느 타입에도 안 걸려 "transport 가 틀렸다" 로 오판되고, 죽은 endpoint 에 반대편
    까지 시도해 대기가 2배가 된다. leaf 중 **하나라도** 네트워크 실패면 폴백하지 않는다 —
    도달 자체가 안 된 것이므로 transport 를 바꿔도 결과가 같다.
    """
    network_failure = _network_failure_types()
    return not any(isinstance(leaf, network_failure) for leaf in _leaf_exceptions(exc))


def classify_failure(exc: BaseException) -> ProbeFailure:
    """예외 → 사용자에게 나갈 사유 코드. **원문을 섞지 않는다.**

    판정도 ExceptionGroup 을 풀고 본다. 우선순위는 좁은 쪽부터다 — 가드 거부 > 타임아웃 >
    네트워크 > 그 외(프로토콜). 타임아웃을 네트워크보다 먼저 보는 이유는 httpx 의
    `TimeoutException` 이 `NetworkError` 와 형제라 순서를 바꾸면 둘이 뭉개지기 때문이다.
    """
    import httpx2

    leaves = _leaf_exceptions(exc)
    if any(isinstance(leaf, ConnectorUrlNotAllowedError) for leaf in leaves):
        return ProbeFailure.of("url_not_allowed")
    if any(isinstance(leaf, httpx2.TimeoutException | TimeoutError) for leaf in leaves):
        return ProbeFailure.of("timeout")
    if any(isinstance(leaf, httpx2.NetworkError | OSError) for leaf in leaves):
        return ProbeFailure.of("unreachable")
    return ProbeFailure.of("protocol_error")


def describe_failure(exc: BaseException) -> str:
    """**로그 전용** 상세 — ExceptionGroup 을 풀어 원인을 드러낸다.

    타입 이름을 함께 붙인다. httpx 의 타임아웃 예외는 `str()` 이 비어 있는 경우가 많아
    메시지만 쓰면 빈 문자열이 된다. 이 문자열은 응답에 싣지 않는다(모듈 docstring 1번).
    """
    seen: dict[str, None] = {}
    for leaf in _leaf_exceptions(exc):
        text = str(leaf).strip()
        seen.setdefault(f"{type(leaf).__name__}: {text}" if text else type(leaf).__name__, None)
    return "; ".join(seen) or exc.__class__.__name__


def tools_to_cache(tools: list[LiveTool]) -> list[dict[str, object]]:
    """LiveTool 목록을 DB `connector_tools_cache.tools` JSONB 형태로 직렬화한다.

    input_schema 는 endpoint 가 준 원본 JSON Schema 를 통째로 보존한다 — 평탄화하면
    파라미터 구조가 손실되고 되돌릴 방법이 없다.
    """
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
            "read_only": t.read_only,
        }
        for t in tools
    ]


async def _run_with_fallback[T](
    endpoint_url: str,
    transport: str,
    op: Callable[[Any], Awaitable[T]],
    *,
    timeout: float,
) -> tuple[T, str]:
    """transport 폴백을 적용해 세션을 열고 `op(session)` 을 실행한다 → (결과, 성공 transport).

    타임아웃은 **시도마다** 건다. 폴백이 일어나는 경우는 첫 시도가 4xx 로 즉시 끝난 상황이라
    (거절은 빠르다) 총 대기가 timeout 을 크게 넘지 않는다. 반대로 느려서 죽은 endpoint 는
    _means_wrong_transport 가 False 라 폴백 없이 한 번만 기다린다.
    """
    candidates = _transport_candidates(transport)
    last_exc: BaseException | None = None
    for idx, candidate in enumerate(candidates):
        # 핸드셰이크 이후(op 실행 중) 난 실패는 transport 문제가 아니다 — 폴백하지 않는다.
        state = {"handshake": False}

        async def _inner(c: str = candidate, s: dict[str, bool] = state) -> T:
            async with _open_session(endpoint_url, c) as session:
                s["handshake"] = True
                return await op(session)

        try:
            return await asyncio.wait_for(_inner(), timeout=timeout), candidate
        except ConnectorUnreachableError:
            raise
        except TimeoutError as exc:
            raise ConnectorUnreachableError(
                ProbeFailure.of("timeout"), detail=f"timed out after {timeout:.0f}s"
            ) from exc
        except Exception as exc:
            last_exc = exc
            has_next = idx + 1 < len(candidates)
            if has_next and not state["handshake"] and _means_wrong_transport(exc):
                logger.info(
                    "connector transport fallback url=%s %s→%s: %r",
                    endpoint_url,
                    candidate,
                    candidates[idx + 1],
                    exc,
                )
                continue
            break

    assert last_exc is not None  # 루프는 반드시 return 하거나 예외를 남긴다
    detail = describe_failure(last_exc)
    logger.info("connector session failed url=%s: %s", endpoint_url, detail)
    raise ConnectorUnreachableError(classify_failure(last_exc), detail=detail) from last_exc


def _streamable_timeout() -> Any:
    """streamable_http 클라이언트 타임아웃 — **sse 가 SDK 에서 받는 값과 동일하게** 맞춘다.

    `sse_client` 는 기본 `timeout=5.0`·`sse_read_timeout=300.0` 을 `Timeout(5.0, read=300.0)`
    으로 팩토리에 직접 넣는다. streamable_http 는 완성된 클라이언트를 넘기는 인터페이스라
    그 주입을 못 받으므로 여기서 같은 값을 만들어 준다.

    **connect 를 MCP 기본 30s 로 두면 안 된다.** 호출부 `asyncio.wait_for`(liveness 10s)가
    먼저 끊기므로 응답 없는 주소(방화벽 drop 등)에서 sse 는 5s 에 끝나는데 streamable_http
    만 10s 를 꽉 채운다 — liveness cron 이 죽은 커넥터 하나당 쓰는 시간이 2배가 된다.
    read 300s 만 크게 두면 된다. 그쪽은 느린 응답을 기다리는 몫이고 실제 상한은 wait_for 다.
    """
    import httpx2
    from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT

    return httpx2.Timeout(5.0, read=MCP_DEFAULT_SSE_READ_TIMEOUT)


def _no_proxy_http_client_factory() -> Any:
    """`trust_env=False` httpx 팩토리 — 사내 프록시 env(HTTP(S)_PROXY)를 우회한다.

    **아웃바운드는 프록시를 타지 않는다.** 커넥터 endpoint 는 사내 사설망·개인 PC 라
    사외 프록시를 거치면 오히려 닿지 않는다. 대신 프록시 단 egress 통제도 못 받으므로
    transport 에 SSRF 가드를 끼워 리다이렉트 목적지까지 직접 검사한다.

    **두 transport 가 모두 이 팩토리를 거쳐야 한다.** streamable_http 는 팩토리가 아니라
    완성된 클라이언트를 받는데, 직접 `httpx2.AsyncClient()` 를 만들면 httpx 기본 타임아웃
    (전 항목 5s)이 걸린다 — SDK 가 `http_client` 를 받은 경우 `create_mcp_http_client()` 를
    건너뛰기 때문이다. 그러면 sse(read 300s)와 달리 streamable_http 만 5s 만에 ReadTimeout
    으로 끊긴다.
    """
    import httpx2
    from mcp.shared._httpx_utils import (
        MCP_DEFAULT_SSE_READ_TIMEOUT,
        MCP_DEFAULT_TIMEOUT,
    )

    def factory(
        headers: dict[str, str] | None = None,
        timeout: Any = None,
        auth: Any = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "follow_redirects": True,
            "trust_env": False,
            "transport": guarded_transport(),
        }
        kwargs["timeout"] = (
            timeout
            if timeout is not None
            else httpx2.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)
        )
        if headers is not None:
            kwargs["headers"] = headers
        if auth is not None:
            kwargs["auth"] = auth
        return httpx2.AsyncClient(**kwargs)

    return factory


@asynccontextmanager
async def _open_session(endpoint_url: str, transport: str) -> AsyncIterator[Any]:
    """transport 별 remote MCP 세션을 열고 `initialize` 까지 마친 ClientSession 을 내준다.

    fetch_tools·check_liveness 가 이 한 곳으로 세션 생성·핸드셰이크를 공유한다.

    SSRF 가드가 두 겹으로 걸린다: 여기서 접속 전에 한 번(빠른 거부 — MCP SDK 가 예외를 감싸
    삼키기 전에 명확한 사유를 남긴다), 그리고 guarded_transport 가 리다이렉트 목적지마다.
    """
    from mcp import ClientSession

    try:
        await assert_url_allowed(endpoint_url)
    except ConnectorUrlNotAllowedError as exc:
        raise ConnectorUnreachableError(
            ProbeFailure.of("url_not_allowed"), detail=str(exc)
        ) from exc

    if transport == "sse":
        from mcp.client.sse import sse_client

        # sse_client 는 httpx_client_factory((headers,timeout,auth)->AsyncClient) 를 받는다.
        factory = _no_proxy_http_client_factory()
        async with (
            sse_client(endpoint_url, httpx_client_factory=factory) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session
        return

    if transport == "streamable_http":
        from mcp.client.streamable_http import streamable_http_client

        # streamable_http 는 팩토리가 아니라 http_client 인스턴스를 받는다(sse 와 인터페이스가
        # 다르다). 그래도 클라이언트는 sse 와 **같은 팩토리·같은 타임아웃**으로 만든다.
        # streamable_http 는 3-tuple (read, write, get_session_id) 를 반환 — sse(2-tuple)와
        # 언팩 arity 가 달라 통째로 받아 인덱싱한다.
        factory = _no_proxy_http_client_factory()
        async with (
            factory(timeout=_streamable_timeout()) as http_client,
            streamable_http_client(endpoint_url, http_client=http_client) as conn,
            ClientSession(conn[0], conn[1]) as session,
        ):
            await session.initialize()
            yield session
        return

    raise ConnectorUnreachableError(
        ProbeFailure.of("unsupported"), detail=f"unsupported transport: {transport}"
    )


async def _list_tools(session: Any) -> list[LiveTool]:
    """열린 세션에서 tools 를 나열해 LiveTool 로 정규화한다(_run_with_fallback 의 op)."""
    result = await session.list_tools()
    tools: list[LiveTool] = []
    for tool in result.tools:
        # mcp SDK 2.0 은 snake_case(`input_schema`)다. 1.x 의 `inputSchema` 도 함께 받아
        # SDK 메이저 차이로 파라미터가 조용히 사라지지 않게 한다(getattr 기본값이 None 이라
        # 이름이 어긋나면 에러 없이 전 도구의 스키마가 비어 버린다).
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
        annotations = getattr(tool, "annotations", None)
        read_only = getattr(annotations, "read_only_hint", None) if annotations else None
        tools.append(
            LiveTool(
                name=tool.name,
                description=getattr(tool, "description", None),
                input_schema=schema if isinstance(schema, dict) else None,
                read_only=read_only if isinstance(read_only, bool) else None,
            )
        )
    return tools


async def fetch_tools(
    endpoint_url: str, transport: str, *, timeout: float = DEFAULT_TIMEOUT_SEC
) -> ToolsFetch:
    """remote MCP 서버에 접속해 tools 를 나열한다 → (tools, 성공한 transport).

    transport 는 **첫 시도 순서**를 정할 뿐이고, 서버가 그것을 거절하면 반대편으로 자동
    폴백한다. 도달·핸드셰이크·나열 실패는 모두 ConnectorUnreachableError 로 변환한다.
    """
    if find_spec("mcp") is None:  # pragma: no cover - 의존성 미설치 환경 방어
        raise ConnectorUnreachableError(
            ProbeFailure.of("unavailable"), detail="mcp SDK not installed"
        )

    async with _outbound_gate():
        tools, used = await _run_with_fallback(
            endpoint_url, transport, _list_tools, timeout=timeout
        )
    return ToolsFetch(tools=tools, transport=used)


async def check_liveness(
    endpoint_url: str, transport: str, *, timeout: float = DEFAULT_TIMEOUT_SEC
) -> Liveness:
    """remote MCP 서버가 살아있는지 확인한다 — `initialize` 핸드셰이크만(list_tools 불요).

    fetch_tools 와 같은 transport 폴백을 쓴다. 폴백이 없으면 등록 당시 transport 가 틀린
    커넥터가 **살아 있는데도** 계속 unhealthy 로 쌓인다.

    실패는 예외 대신 `Liveness(healthy=False, ...)` 로 수렴한다 — liveness cron 이 한
    커넥터 실패로 배치 전체를 멈추지 않게 한다.
    """
    if find_spec("mcp") is None:  # pragma: no cover - 의존성 미설치 환경 방어
        return Liveness(healthy=False, transport=None, failure=ProbeFailure.of("unavailable"))

    async def _probe(_session: Any) -> bool:
        return True

    try:
        async with _outbound_gate():
            _, used = await _run_with_fallback(endpoint_url, transport, _probe, timeout=timeout)
    except ConnectorUnreachableError as exc:
        logger.info("connector liveness failed url=%s: %s", endpoint_url, exc.detail)
        return Liveness(healthy=False, transport=None, failure=exc.failure)
    except Exception as exc:  # pragma: no cover - _run_with_fallback 가 전부 변환한다
        logger.info("connector liveness failed url=%s: %r", endpoint_url, exc)
        return Liveness(healthy=False, transport=None, failure=classify_failure(exc))
    return Liveness(healthy=True, transport=used)
