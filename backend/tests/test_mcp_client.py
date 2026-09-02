"""remote MCP 클라이언트 — transport 폴백과 실패 사유 (connector-hub#3).

`_open_session` 을 가짜로 갈아끼워 "어느 transport 로 몇 번 시도했는가" 를 관찰한다. 폴백
자체가 이 코드의 값이므로, 성공 경로보다 **폴백이 일어나야 하는 경우와 일어나면 안 되는
경우의 경계**를 핀으로 고정한다. AgentToolbox 에서 함께 옮겨 온 테스트다.

두 번째 축은 **사유 뭉개기**다. 예외 원문이 응답으로 새면 인증 사용자가 내부망을 훑을 수
있다 — 옮기면서 새로 넣은 규칙이라 여기서 고정한다.
"""

from __future__ import annotations

import contextlib
from typing import Any

import httpx2
import pytest

from core.mcp import client as mod
from core.mcp.url_guard import ConnectorUrlNotAllowedError


def _fake_session_opener(outcomes: dict[str, BaseException | None], calls: list[str]):
    """transport → 예외(또는 None=성공) 매핑대로 동작하는 가짜 `_open_session`."""

    @contextlib.asynccontextmanager
    async def _opener(endpoint_url: str, transport: str):
        calls.append(transport)
        exc = outcomes.get(transport)
        if exc is not None:
            raise exc
        yield object()

    return _opener


async def _run(monkeypatch, outcomes, requested: str) -> tuple[Any, list[str]]:
    calls: list[str] = []
    monkeypatch.setattr(mod, "_open_session", _fake_session_opener(outcomes, calls))

    async def _op(_session: Any) -> str:
        return "ok"

    result = await mod._run_with_fallback("https://x.example.com/mcp", requested, _op, timeout=5.0)
    return result, calls


# --- 폴백 순서 -----------------------------------------------------------------


def test_transport_candidates_puts_requested_first() -> None:
    """요청값을 먼저 시도한다 — 폴백은 어디까지나 2순위다."""
    assert mod._transport_candidates("sse") == ["sse", "streamable_http"]
    assert mod._transport_candidates("streamable_http") == ["streamable_http", "sse"]


def test_transport_candidates_unknown_value_is_not_expanded() -> None:
    """미지원 transport(stdio 등)에 폴백을 붙이지 않는다 — 조용히 다른 걸 쓰면 안 된다."""
    assert mod._transport_candidates("stdio") == ["stdio"]


def test_transport_vocabulary_matches_the_database() -> None:
    """DB CHECK 와 같은 어휘여야 폴백 되기록이 제약에 부딪히지 않는다(마이그레이션 0002)."""
    assert set(mod.TRANSPORTS) == {"streamable_http", "sse"}


# --- 폴백 판정 -----------------------------------------------------------------


def test_network_failures_do_not_trigger_fallback() -> None:
    """연결 불가·타임아웃은 endpoint 가 죽은 것 — 반대편을 또 시도하면 대기만 2배가 된다."""
    assert mod._means_wrong_transport(httpx2.ConnectError("refused")) is False
    assert mod._means_wrong_transport(httpx2.ConnectTimeout("slow")) is False
    assert mod._means_wrong_transport(TimeoutError()) is False
    assert mod._means_wrong_transport(OSError("unreachable")) is False


def test_protocol_rejections_trigger_fallback() -> None:
    """서버가 응답은 했지만 이 transport 를 거절한 경우 — 반대편에 가치가 있다."""
    assert mod._means_wrong_transport(httpx2.RemoteProtocolError("bad frame")) is True


def test_exception_group_is_unwrapped_before_judging() -> None:
    """MCP SDK 는 anyio TaskGroup 을 쓴다 — 실제 원인이 ExceptionGroup 에 싸여 올라온다.

    안 풀면 그룹이 httpx 예외의 서브클래스가 아니라 네트워크 판정을 전부 빠져나가고, 죽은
    endpoint 에 반대편 transport 까지 시도해 사용자 대기가 2배가 된다.
    """
    wrapped = BaseExceptionGroup(
        "unhandled errors in a TaskGroup", [httpx2.ConnectError("refused")]
    )
    assert mod._means_wrong_transport(wrapped) is False


def test_nested_exception_groups_are_unwrapped() -> None:
    nested = BaseExceptionGroup(
        "outer", [BaseExceptionGroup("inner", [httpx2.ConnectTimeout("slow")])]
    )
    assert mod._means_wrong_transport(nested) is False


def test_wrapped_protocol_rejection_still_triggers_fallback() -> None:
    """언랩이 폴백을 죽이면 안 된다 — 그룹에 싸인 프로토콜 거절은 여전히 폴백 대상이다."""
    wrapped = BaseExceptionGroup("boom", [httpx2.RemoteProtocolError("405")])
    assert mod._means_wrong_transport(wrapped) is True


def test_mixed_group_with_network_failure_does_not_fall_back() -> None:
    """leaf 하나라도 네트워크 실패면 도달 자체가 안 된 것 — transport 를 바꿔도 같다."""
    mixed = BaseExceptionGroup(
        "boom", [httpx2.RemoteProtocolError("405"), httpx2.ConnectError("refused")]
    )
    assert mod._means_wrong_transport(mixed) is False


async def test_falls_back_to_sse_when_streamable_rejected(monkeypatch) -> None:
    """대표 실패 사례: legacy SSE 서버에 streamable_http 로 POST → 405 → sse 로 성공."""
    result, calls = await _run(
        monkeypatch,
        {"streamable_http": httpx2.RemoteProtocolError("405"), "sse": None},
        "streamable_http",
    )
    assert calls == ["streamable_http", "sse"]
    assert result == ("ok", "sse")  # 성공한 transport 를 함께 돌려준다(DB 되기록용)


async def test_falls_back_to_streamable_when_sse_rejected(monkeypatch) -> None:
    result, calls = await _run(
        monkeypatch, {"sse": httpx2.RemoteProtocolError("400"), "streamable_http": None}, "sse"
    )
    assert calls == ["sse", "streamable_http"]
    assert result == ("ok", "streamable_http")


async def test_no_fallback_on_connect_error(monkeypatch) -> None:
    """죽은 endpoint 는 한 번만 시도하고 끝낸다 — 사용자 대기가 2배가 되지 않게."""
    calls: list[str] = []
    monkeypatch.setattr(
        mod,
        "_open_session",
        _fake_session_opener({"streamable_http": httpx2.ConnectError("refused")}, calls),
    )

    async def _op(_session: Any) -> str:  # pragma: no cover - 도달하지 않는다
        return "ok"

    with pytest.raises(mod.ConnectorUnreachableError):
        await mod._run_with_fallback(
            "https://dead.example.com/", "streamable_http", _op, timeout=5.0
        )
    assert calls == ["streamable_http"]


async def test_requested_transport_wins_without_extra_attempt(monkeypatch) -> None:
    result, calls = await _run(monkeypatch, {"sse": None}, "sse")
    assert calls == ["sse"]
    assert result == ("ok", "sse")


async def test_failure_after_handshake_does_not_fall_back(monkeypatch) -> None:
    """핸드셰이크가 끝난 뒤(list_tools 중) 난 실패는 transport 문제가 아니다."""
    calls: list[str] = []
    monkeypatch.setattr(mod, "_open_session", _fake_session_opener({"sse": None}, calls))

    async def _op(_session: Any) -> str:
        raise httpx2.RemoteProtocolError("list_tools 실패")  # 폴백 대상 예외 타입이지만…

    with pytest.raises(mod.ConnectorUnreachableError):
        await mod._run_with_fallback("https://x.example.com/sse", "sse", _op, timeout=5.0)
    assert calls == ["sse"]  # …핸드셰이크 이후라 재시도하지 않는다


# --- 실패 사유 뭉개기 ------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (httpx2.ConnectError("refused"), "unreachable"),
        (OSError("no route"), "unreachable"),
        (httpx2.ConnectTimeout("slow"), "timeout"),
        (TimeoutError(), "timeout"),
        (httpx2.RemoteProtocolError("405"), "protocol_error"),
        (ConnectorUrlNotAllowedError("host resolves to a blocked address: x"), "url_not_allowed"),
    ],
)
def test_classify_failure_maps_to_stable_codes(exc: BaseException, code: str) -> None:
    assert mod.classify_failure(exc).code == code


def test_classify_failure_unwraps_groups() -> None:
    wrapped = BaseExceptionGroup("boom", [httpx2.ConnectError("refused")])
    assert mod.classify_failure(wrapped).code == "unreachable"


def test_timeout_wins_over_network_classification() -> None:
    """httpx 의 TimeoutException 은 NetworkError 와 형제다 — 순서를 바꾸면 둘이 뭉개진다."""
    assert mod.classify_failure(httpx2.ReadTimeout("")).code == "timeout"


def test_failure_message_never_carries_the_exception_text() -> None:
    """사용자에게 나가는 문구에 내부 주소·호스트명이 섞이면 안 된다.

    이것이 원본에서 바꾼 지점이다. 원본은 예외 문자열을 그대로 돌려줬고, 그 문자열에는
    리다이렉트 목적지가 실려 나올 수 있었다.
    """
    exc = httpx2.ConnectError("failed to connect to 10.1.2.3:9000 (mcp-internal.corp)")
    failure = mod.classify_failure(exc)
    assert "10.1.2.3" not in failure.message
    assert "mcp-internal" not in failure.message
    assert failure.message == mod._FAILURE_MESSAGES["unreachable"]


async def test_unreachable_error_keeps_detail_for_logs_only(monkeypatch) -> None:
    """원문은 버리지 않는다 — 로그에는 남아야 운영자가 원인을 안다."""
    calls: list[str] = []
    monkeypatch.setattr(
        mod,
        "_open_session",
        _fake_session_opener(
            {"streamable_http": httpx2.ConnectError("refused by 10.1.2.3")}, calls
        ),
    )

    async def _op(_session: Any) -> str:  # pragma: no cover
        return "ok"

    with pytest.raises(mod.ConnectorUnreachableError) as excinfo:
        await mod._run_with_fallback("https://dead.example.com/", "streamable_http", _op, timeout=5)

    assert "10.1.2.3" in excinfo.value.detail  # 로그용
    assert "10.1.2.3" not in excinfo.value.failure.message  # 사용자용


def test_describe_failure_surfaces_cause_instead_of_taskgroup_text() -> None:
    """로그 문자열이 "TaskGroup (1 sub-exception)" 로 끝나면 원인을 못 찾는다."""
    wrapped = BaseExceptionGroup("unhandled errors in a TaskGroup", [httpx2.ReadTimeout("")])
    described = mod.describe_failure(wrapped)
    assert "TaskGroup" not in described
    assert described == "ReadTimeout"  # str() 이 비어도 타입 이름은 남는다


def test_describe_failure_dedupes_repeated_leaves() -> None:
    group = BaseExceptionGroup(
        "boom", [httpx2.ConnectError("refused"), httpx2.ConnectError("refused")]
    )
    assert mod.describe_failure(group) == "ConnectError: refused"


# --- liveness ------------------------------------------------------------------


async def test_liveness_reports_transport_that_worked(monkeypatch) -> None:
    """check_liveness 도 같은 폴백을 쓴다 — 없으면 살아있는 커넥터가 계속 unhealthy 로 쌓인다."""
    calls: list[str] = []
    monkeypatch.setattr(
        mod,
        "_open_session",
        _fake_session_opener(
            {"streamable_http": httpx2.RemoteProtocolError("405"), "sse": None}, calls
        ),
    )
    result = await mod.check_liveness("https://x.example.com/sse", "streamable_http", timeout=5.0)
    assert result.healthy is True
    assert result.transport == "sse"
    assert result.failure is None
    assert calls == ["streamable_http", "sse"]


async def test_liveness_failure_yields_no_transport_but_a_reason(monkeypatch) -> None:
    """전부 실패하면 되기록할 transport 는 없고 사유만 남는다."""
    calls: list[str] = []
    monkeypatch.setattr(
        mod,
        "_open_session",
        _fake_session_opener(
            {
                "streamable_http": httpx2.RemoteProtocolError("405"),
                "sse": httpx2.RemoteProtocolError("400"),
            },
            calls,
        ),
    )
    result = await mod.check_liveness("https://dead.example.com/", "streamable_http", timeout=5.0)
    assert result.healthy is False
    assert result.transport is None
    assert result.failure is not None and result.failure.code == "protocol_error"
    assert calls == ["streamable_http", "sse"]


# --- 아웃바운드 동시성 상한 ------------------------------------------------------


async def test_outbound_gate_limits_concurrent_sessions(monkeypatch) -> None:
    """세션 하나가 소켓과 DNS 스레드를 잡는다 — API 경로에도 상한이 걸려야 한다."""
    import asyncio

    from core.settings import load_settings

    monkeypatch.setenv("CONNECTOR_OUTBOUND_CONCURRENCY", "2")
    load_settings.cache_clear()

    in_flight = 0
    peak = 0

    @contextlib.asynccontextmanager
    async def _slow_opener(endpoint_url: str, transport: str):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.02)
            yield object()
        finally:
            in_flight -= 1

    monkeypatch.setattr(mod, "_open_session", _slow_opener)
    monkeypatch.setattr(mod, "_list_tools", lambda _s: _ok([]))

    await asyncio.gather(
        *(mod.fetch_tools("https://x.example.com/mcp", "sse", timeout=5.0) for _ in range(6))
    )
    assert peak <= 2


async def _ok(value):
    return value
