"""tools 미리보기·캐시·수동 재검사 API (connector-hub#3).

**이 파일이 지키는 두 가지.** 하나는 캐시 규약 — 실패해도 마지막 정상값을 버리지 않고
낡았다는 것만 알린다. 다른 하나는 누설 방지 — 보이지 않는 카드의 존재와 endpoint 오류
원문이 응답으로 새지 않는다.

프로브는 전부 가짜다. 진짜 네트워크를 타면 테스트 결과가 그 환경의 resolver 에 달린다.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from core.mcp.client import ConnectorUnreachableError, Liveness, LiveTool, ProbeFailure, ToolsFetch
from tests.conftest_db import REQUIRES_DB, Viewer, _sync_url, make_client, truncate_connectors

pytestmark = REQUIRES_DB

OWNER = Viewer(sub="u_owner", team_codes=("team-a",))
OUTSIDER = Viewer(sub="u_out", team_codes=("team-z",))


@pytest.fixture(autouse=True)
def _clean():
    from api import rate_limit

    rate_limit.reset()
    truncate_connectors()
    yield
    truncate_connectors()
    rate_limit.reset()


def _tools(*names: str) -> ToolsFetch:
    return ToolsFetch(
        tools=[
            LiveTool(
                name=n, description=f"{n} 설명", input_schema={"type": "object"}, read_only=True
            )
            for n in names
        ],
        transport="streamable_http",
    )


def _fetch_ok(*names: str, transport: str = "streamable_http"):
    async def _fake(url: str, tr: str, *, timeout: float = 0) -> ToolsFetch:
        return ToolsFetch(tools=_tools(*names).tools, transport=transport)

    return _fake


def _fetch_fail(code: str = "unreachable", detail: str = "10.1.2.3 refused"):
    async def _fake(url: str, tr: str, *, timeout: float = 0) -> ToolsFetch:
        raise ConnectorUnreachableError(ProbeFailure.of(code), detail=detail)  # type: ignore[arg-type]

    return _fake


def _live(healthy: bool, transport: str | None = "streamable_http", code: str = "unreachable"):
    async def _fake(url: str, tr: str, *, timeout: float = 0) -> Liveness:
        if healthy:
            return Liveness(healthy=True, transport=transport)
        return Liveness(healthy=False, transport=None, failure=ProbeFailure.of(code))  # type: ignore[arg-type]

    return _fake


def _body(**over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "sample-connector",
        "short_description": "요약",
        "category": "productivity",
        "source_repo_url": "https://repo.test/mcp",
        "endpoint_url": "https://mcp.test/stream",
        "transport": "streamable_http",
        "scope_type": "team",
        "scope_id": "team-a",
        "tags": [],
        "visibility_teams": [],
    }
    body.update(over)
    return body


def _create(viewer: Viewer = OWNER, *, fetch: Any = None, **over: object) -> dict:
    with make_client(viewer, fetch_tools=fetch) as client:
        resp = client.post("/connectors", json=_body(**over))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _backdate(connector_id: str, column: str, table: str, interval: str) -> None:
    """시각을 과거로 민다 — stale 판정을 기다리지 않고 확인하려고."""
    with psycopg.connect(_sync_url(), autocommit=True) as conn:
        conn.execute(
            f"UPDATE {table} SET {column} = now() - INTERVAL '{interval}' WHERE connector_id = %s",
            (connector_id,),
        )


# ---- 미리보기 ----------------------------------------------------------------------------


def test_preview_returns_tools() -> None:
    with make_client(OWNER, fetch_tools=_fetch_ok("search", "fetch")) as client:
        resp = client.post(
            "/connectors/preview-tools",
            json={"endpoint_url": "https://mcp.test/stream", "transport": "streamable_http"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert [t["name"] for t in body["tools"]] == ["search", "fetch"]
    assert body["tools"][0]["input_schema"] == {"type": "object"}
    assert body["tools"][0]["read_only"] is True
    assert body["error"] is None


def test_preview_requires_login() -> None:
    """임의 주소로 서버를 내보내는 경로다 — 익명에게 열어 두지 않는다."""
    with make_client(None) as client:
        resp = client.post(
            "/connectors/preview-tools", json={"endpoint_url": "https://mcp.test/stream"}
        )
    assert resp.status_code == 401


def test_preview_failure_is_a_state_not_an_error() -> None:
    """남의 endpoint 가 안 뜬 것을 5xx 로 돌려주면 우리 서버가 고장 난 것처럼 보인다."""
    with make_client(OWNER, fetch_tools=_fetch_fail()) as client:
        resp = client.post(
            "/connectors/preview-tools", json={"endpoint_url": "https://mcp.test/stream"}
        )
    assert resp.status_code == 200
    assert resp.json()["tools"] == []
    assert resp.json()["error"]["code"] == "unreachable"


def test_preview_failure_does_not_leak_the_endpoint_error_text() -> None:
    """사유 원문에는 내부 주소가 실려 올 수 있다 — 응답에 그대로 나가면 포트 스캔이 된다."""
    with make_client(OWNER, fetch_tools=_fetch_fail(detail="connect 10.1.2.3:9000 refused")) as c:
        resp = c.post("/connectors/preview-tools", json={"endpoint_url": "https://mcp.test/s"})
    assert "10.1.2.3" not in resp.text


def test_preview_is_rate_limited(monkeypatch) -> None:
    monkeypatch.setenv("CONNECTOR_PROBE_RATE_LIMIT_PER_MIN", "2")
    with make_client(OWNER, fetch_tools=_fetch_ok("a")) as client:
        payload = {"endpoint_url": "https://mcp.test/stream"}
        codes = [
            client.post("/connectors/preview-tools", json=payload).status_code for _ in range(4)
        ]
    assert codes == [200, 200, 429, 429]


# ---- 등록 직후 캐시 ----------------------------------------------------------------------


def test_registration_caches_tools_from_the_server_not_the_client() -> None:
    """화면에 보이는 도구 목록은 서버가 직접 본 것이어야 한다.

    프론트가 미리보기 결과를 왕복 저장하게 하면 위변조된 목록이 그대로 카드에 실린다.
    """
    created = _create(fetch=_fetch_ok("search"))
    with make_client(OWNER) as client:
        resp = client.get(f"/connectors/{created['connector_id']}/tools")
    body = resp.json()
    assert [t["name"] for t in body["tools"]] == ["search"]
    assert body["cached"] is True
    assert body["fetched_at"] is not None


def test_registration_survives_an_unreachable_endpoint() -> None:
    """endpoint 가 죽어 있어도 등록 자체는 성공해야 한다 — 나중에 고칠 수 있어야 하므로."""
    created = _create(fetch=_fetch_fail())
    with make_client(OWNER) as client:
        resp = client.get(f"/connectors/{created['connector_id']}/tools")
    assert resp.status_code == 200
    assert resp.json()["tools"] == []
    assert resp.json()["error"]["code"] == "not_fetched"


def test_registration_records_the_transport_that_actually_worked() -> None:
    """폴백이 등록값과 다른 쪽으로 성공했으면 되기록한다 — 안 그러면 매번 폴백을 다시 거친다.

    201 응답 자체가 교정된 값을 담아야 한다. 담지 않으면 등록 화면이 방금 만든 카드를
    새로고침해야 제 값을 보게 된다.
    """
    created = _create(fetch=_fetch_ok("search", transport="sse"))
    assert created["transport"] == "sse"
    assert created["tools_fetched_at"] is not None
    with make_client(OWNER) as client:
        detail = client.get(f"/connectors/{created['connector_id']}").json()
    assert detail["transport"] == "sse"


# ---- 캐시 조회 · 새로고침 ----------------------------------------------------------------


def test_refresh_updates_the_cache() -> None:
    created = _create(fetch=_fetch_ok("old"))
    with make_client(OWNER, fetch_tools=_fetch_ok("new")) as client:
        body = client.get(f"/connectors/{created['connector_id']}/tools?refresh=true").json()
    assert [t["name"] for t in body["tools"]] == ["new"]


def test_failed_refresh_keeps_the_last_good_tools() -> None:
    """워커나 endpoint 가 죽어도 화면이 빈손이 되면 안 된다 — 마지막 정상값 + 사유 + stale."""
    created = _create(fetch=_fetch_ok("search"))
    with make_client(OWNER, fetch_tools=_fetch_fail()) as client:
        body = client.get(f"/connectors/{created['connector_id']}/tools?refresh=true").json()
    assert [t["name"] for t in body["tools"]] == ["search"]  # 지우지 않았다
    assert body["error"]["code"] == "stale_cache"
    assert body["fetched_at"] is not None  # 마지막으로 **성공한** 시각 그대로


def test_old_cache_is_marked_stale(monkeypatch) -> None:
    created = _create(fetch=_fetch_ok("search"))
    _backdate(created["connector_id"], "fetched_at", "connector_tools_cache", "2 days")
    with make_client(OWNER) as client:
        body = client.get(f"/connectors/{created['connector_id']}/tools").json()
    assert body["stale"] is True
    assert [t["name"] for t in body["tools"]] == ["search"]  # 여전히 보여 준다


def test_fresh_cache_is_not_stale() -> None:
    created = _create(fetch=_fetch_ok("search"))
    with make_client(OWNER) as client:
        assert client.get(f"/connectors/{created['connector_id']}/tools").json()["stale"] is False


def test_refresh_requires_login() -> None:
    """조회는 익명에게 열어 두되, 새로고침은 아니다 — 그쪽만 바깥으로 나간다."""
    created = _create(fetch=_fetch_ok("search"), scope_type="global", scope_id=None)
    with make_client(None) as client:
        assert client.get(f"/connectors/{created['connector_id']}/tools").status_code == 200
        assert (
            client.get(f"/connectors/{created['connector_id']}/tools?refresh=true").status_code
            == 401
        )


def test_tools_of_an_invisible_connector_are_a_plain_404() -> None:
    """도구 목록으로 비공개 카드의 존재가 새면 안 된다."""
    created = _create(fetch=_fetch_ok("search"))
    with make_client(OUTSIDER) as client:
        assert client.get(f"/connectors/{created['connector_id']}/tools").status_code == 404


# ---- 수동 재검사 -------------------------------------------------------------------------


def test_health_check_records_the_result() -> None:
    created = _create(fetch=_fetch_ok("search"))
    with make_client(OWNER, check_liveness=_live(True)) as client:
        resp = client.post(f"/connectors/{created['connector_id']}/health-check")
    assert resp.status_code == 200
    assert resp.json()["health_status"] == "healthy"
    assert resp.json()["last_checked_at"] is not None

    with make_client(OWNER) as client:
        assert (
            client.get(f"/connectors/{created['connector_id']}").json()["health_status"]
            == "healthy"
        )


def test_unreachable_endpoint_is_a_state_not_a_5xx() -> None:
    created = _create(fetch=_fetch_ok("search"))
    with make_client(OWNER, check_liveness=_live(False)) as client:
        resp = client.post(f"/connectors/{created['connector_id']}/health-check")
    assert resp.status_code == 200
    assert resp.json()["health_status"] == "unhealthy"
    assert resp.json()["error"]["code"] == "unreachable"


def test_a_dead_connector_stays_in_the_catalog() -> None:
    """자동 archive 를 넣지 않았음을 고정한다.

    목록에서 치우면 소유자에게는 "등록한 게 사라졌다" 로만 보이고, endpoint 가 되살아나도
    스스로 복귀하지 못한다. AgentToolbox 가 도입했다 철회한 동작이라 다시 파지 않는다.
    """
    created = _create(fetch=_fetch_ok("search"))
    with make_client(OWNER, check_liveness=_live(False)) as client:
        for _ in range(5):
            client.post(f"/connectors/{created['connector_id']}/health-check")
        listed = client.get("/connectors").json()["items"]
    assert [c["connector_id"] for c in listed] == [created["connector_id"]]
    assert listed[0]["health_status"] == "unhealthy"


def test_health_check_corrects_the_transport() -> None:
    created = _create(fetch=_fetch_ok("search"))
    with make_client(OWNER, check_liveness=_live(True, transport="sse")) as client:
        client.post(f"/connectors/{created['connector_id']}/health-check")
        assert client.get(f"/connectors/{created['connector_id']}").json()["transport"] == "sse"


def test_health_badge_goes_stale_when_nobody_checks(monkeypatch) -> None:
    """워커가 멈추면 마지막 정상값이 그대로 남는다 — 그것을 현재 상태로 읽으면 장애를 놓친다."""
    created = _create(fetch=_fetch_ok("search"))
    with make_client(OWNER, check_liveness=_live(True)) as client:
        client.post(f"/connectors/{created['connector_id']}/health-check")
    _backdate(created["connector_id"], "last_checked_at", "connector_health_checks", "3 hours")
    with make_client(OWNER) as client:
        detail = client.get(f"/connectors/{created['connector_id']}").json()
    assert detail["health_status"] == "healthy"
    assert detail["health_stale"] is True


def test_health_check_of_an_invisible_connector_is_a_plain_404() -> None:
    created = _create(fetch=_fetch_ok("search"))
    with make_client(OUTSIDER, check_liveness=_live(True)) as client:
        assert client.post(f"/connectors/{created['connector_id']}/health-check").status_code == 404


def test_connector_without_an_endpoint_is_not_probed() -> None:
    created = _create(fetch=_fetch_ok("search"), endpoint_url=None, transport=None)
    with make_client(OWNER, check_liveness=_live(True)) as client:
        resp = client.post(f"/connectors/{created['connector_id']}/health-check")
    assert resp.status_code == 200
    assert resp.json()["health_status"] == "unknown"  # 상태를 건드리지 않았다
    assert resp.json()["error"]["code"] == "no_endpoint"
