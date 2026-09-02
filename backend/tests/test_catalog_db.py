"""카탈로그·CRUD — CONNECTOR_TEST_DATABASE_URL 설정 시에만 실행 (connector-hub#2).

**공개범위가 이 파일의 중심이다.** 판정이 새면 비공개 카드가 노출되고, 그것은 목록을 눈으로
봐서는 드러나지 않는다 — 특정 뷰어·특정 경로 조합에서만 샌다. 그래서 경로마다 확인한다.
"""

from __future__ import annotations

import pytest

from tests.conftest_db import REQUIRES_DB, Viewer, make_client, truncate_connectors

pytestmark = REQUIRES_DB

OWNER = Viewer(sub="u_owner", team_codes=("team-a",))
TEAMMATE = Viewer(sub="u_mate", team_codes=("team-a",))
OUTSIDER = Viewer(sub="u_out", team_codes=("team-z",))
SHARED = Viewer(sub="u_shared", team_codes=("team-b",))


@pytest.fixture(autouse=True)
def _clean():
    truncate_connectors()
    yield
    truncate_connectors()


def _body(**over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "sample-connector",
        "short_description": "요약",
        "category": "productivity",
        "source_repo_url": "https://repo.test/mcp",
        "endpoint_url": "https://mcp.test/stream",
        "transport": "http",
        "scope_type": "team",
        "scope_id": "team-a",
        "tags": ["mcp", "search"],
        "visibility_teams": [],
    }
    body.update(over)
    return body


def _create(viewer: Viewer, **over: object) -> dict:
    with make_client(viewer) as client:
        resp = client.post("/connectors", json=_body(**over))
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---- 등록 ----
def test_create_and_read_back() -> None:
    created = _create(OWNER)
    assert created["name"] == "sample-connector"
    assert created["tags"] == ["mcp", "search"]
    assert created["health_status"] == "unknown"
    assert created["star_count"] == 0

    with make_client(OWNER) as client:
        detail = client.get(f"/connectors/{created['connector_id']}")
    assert detail.status_code == 200
    assert detail.json()["source_repo_url"] == "https://repo.test/mcp"


def test_short_id_resolves_the_same_card() -> None:
    """deep link 는 UUID 와 short_id 를 모두 쓴다."""
    created = _create(OWNER)
    with make_client(OWNER) as client:
        by_short = client.get(f"/connectors/{created['short_id']}")
    assert by_short.status_code == 200
    assert by_short.json()["connector_id"] == created["connector_id"]


def test_anonymous_cannot_create() -> None:
    with make_client(None) as client:
        assert client.post("/connectors", json=_body()).status_code == 401


@pytest.mark.parametrize(
    "over,reason",
    [
        ({"scope_type": "global", "scope_id": "team-a"}, "global 에 scope_id"),
        ({"scope_type": "team", "scope_id": None}, "team 인데 scope_id 없음"),
        ({"transport": "http", "endpoint_url": None}, "transport 만 있고 주소 없음"),
    ],
)
def test_inconsistent_body_is_422(over: dict, reason: str) -> None:
    """DB CHECK 가 막아 주긴 하지만 그때는 클라이언트가 무엇이 틀렸는지 알 수 없다."""
    with make_client(OWNER) as client:
        assert client.post("/connectors", json=_body(**over)).status_code == 422, reason


def test_tag_normalization_and_rejection() -> None:
    with make_client(OWNER) as client:
        ok = client.post("/connectors", json=_body(tags=["MCP", "mcp", "Search"]))
        assert ok.status_code == 201
        assert ok.json()["tags"] == ["mcp", "search"], "대소문자만 다른 태그가 갈라지면 안 된다"
        bad = client.post("/connectors", json=_body(name="x", tags=["has space"]))
        assert bad.status_code == 422


# ---- 공개범위 ----
def test_team_card_is_hidden_from_outsiders_on_every_read_path() -> None:
    """목록·상세·태그·지표 넷 다 확인한다. 한 경로만 새도 카드가 드러난다."""
    created = _create(OWNER, scope_type="team", scope_id="team-a")

    with make_client(OUTSIDER) as client:
        assert client.get("/connectors").json()["items"] == []
        assert client.get(f"/connectors/{created['connector_id']}").status_code == 404
        assert client.get("/connectors/tags").json() == []
        assert client.get("/connectors/stats").json()["total"] == 0


def test_team_card_is_visible_to_the_owning_team() -> None:
    _create(OWNER, scope_type="team", scope_id="team-a")
    with make_client(TEAMMATE) as client:
        assert len(client.get("/connectors").json()["items"]) == 1


def test_owner_sees_own_card_even_without_the_team() -> None:
    """소속이 바뀌어도 자기가 등록한 카드는 보여야 한다 — 아니면 수정도 못 한다."""
    _create(OWNER, scope_type="team", scope_id="team-a")
    moved = Viewer(sub="u_owner", team_codes=("team-elsewhere",))
    with make_client(moved) as client:
        assert len(client.get("/connectors").json()["items"]) == 1


def test_extra_visibility_team_grants_access() -> None:
    _create(OWNER, scope_type="team", scope_id="team-a", visibility_teams=["team-b"])
    with make_client(SHARED) as client:
        assert len(client.get("/connectors").json()["items"]) == 1
    with make_client(OUTSIDER) as client:
        assert client.get("/connectors").json()["items"] == []


def test_global_card_is_visible_to_anonymous() -> None:
    _create(OWNER, scope_type="global", scope_id=None)
    with make_client(None) as client:
        assert len(client.get("/connectors").json()["items"]) == 1


def test_anonymous_sees_no_team_cards() -> None:
    _create(OWNER, scope_type="team", scope_id="team-a")
    with make_client(None) as client:
        assert client.get("/connectors").json()["items"] == []
        assert client.get("/connectors/stats").json()["total"] == 0


# ---- 수정·삭제 ----
def test_only_the_owner_can_edit() -> None:
    created = _create(OWNER)
    with make_client(TEAMMATE) as client:
        # 같은 팀이라 **볼 수는** 있지만 고칠 수는 없다. 404 인 이유는 존재를 흘리지 않기
        # 위해서가 아니라 — 여기서는 이미 보이므로 — 일관된 응답을 주기 위해서다.
        assert client.get(f"/connectors/{created['connector_id']}").status_code == 200
        assert (
            client.patch(
                f"/connectors/{created['connector_id']}", json=_body(name="hijacked")
            ).status_code
            == 404
        )
        assert client.delete(f"/connectors/{created['connector_id']}").status_code == 404


def test_update_replaces_tags_and_visibility() -> None:
    created = _create(OWNER, tags=["mcp"], visibility_teams=["team-b"])
    with make_client(OWNER) as client:
        resp = client.patch(
            f"/connectors/{created['connector_id']}",
            json=_body(tags=["docs"], visibility_teams=[]),
        )
    assert resp.status_code == 200
    assert resp.json()["tags"] == ["docs"]
    assert resp.json()["visibility_teams"] == []

    with make_client(SHARED) as client:
        assert client.get("/connectors").json()["items"] == [], "공개범위 회수가 즉시 반영돼야 한다"


def test_delete_removes_the_card_and_children() -> None:
    created = _create(OWNER)
    with make_client(OWNER) as client:
        assert client.delete(f"/connectors/{created['connector_id']}").status_code == 204
        assert client.get(f"/connectors/{created['connector_id']}").status_code == 404
        assert client.get("/connectors/tags").json() == []


# ---- 목록 ----
def test_tag_filter_requires_all_tags() -> None:
    """필터를 더할수록 결과가 넓어지면 사용자는 그것을 필터로 인식하지 못한다."""
    _create(OWNER, name="a", tags=["mcp", "search"])
    _create(OWNER, name="b", tags=["mcp"])
    with make_client(OWNER) as client:
        both = client.get("/connectors", params={"tag": ["mcp", "search"]}).json()["items"]
        one = client.get("/connectors", params={"tag": ["mcp"]}).json()["items"]
    assert [c["name"] for c in both] == ["a"]
    assert len(one) == 2


def test_search_matches_name_and_tag() -> None:
    _create(OWNER, name="grafana-bridge", tags=["metrics"])
    _create(OWNER, name="unrelated", tags=["other"])
    with make_client(OWNER) as client:
        by_name = client.get("/connectors", params={"q": "grafana-bridge"}).json()["items"]
        by_tag = client.get("/connectors", params={"q": "metrics"}).json()["items"]
    assert [c["name"] for c in by_name] == ["grafana-bridge"]
    assert [c["name"] for c in by_tag] == ["grafana-bridge"]


def test_keyset_pagination_covers_every_row_once() -> None:
    """offset 을 쓰지 않는 이유 — 페이지 사이에 카드가 끼어도 건너뛰거나 되풀이하지 않는다."""
    for i in range(7):
        _create(OWNER, name=f"conn-{i:02d}")
    seen: list[str] = []
    cursor = None
    with make_client(OWNER) as client:
        for _ in range(10):
            params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
            page = client.get("/connectors", params=params).json()
            seen.extend(c["name"] for c in page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                break
    assert sorted(seen) == [f"conn-{i:02d}" for i in range(7)]
    assert len(seen) == len(set(seen))


def test_cursor_from_a_different_sort_is_rejected() -> None:
    """정렬을 바꾸며 옛 커서를 쓰면 결과가 조용히 뒤섞인다."""
    for i in range(3):
        _create(OWNER, name=f"c{i}")
    with make_client(OWNER) as client:
        page = client.get("/connectors", params={"limit": 1}).json()
        resp = client.get(
            "/connectors", params={"limit": 1, "sort": "name", "cursor": page["next_cursor"]}
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_cursor"


def test_garbage_cursor_is_400_not_500() -> None:
    with make_client(OWNER) as client:
        assert client.get("/connectors", params={"cursor": "!!!"}).status_code == 400


def test_stats_counts_only_visible_cards() -> None:
    _create(OWNER, scope_type="global", scope_id=None)
    _create(OWNER, name="hidden", scope_type="team", scope_id="team-a")
    with make_client(OUTSIDER) as client:
        assert client.get("/connectors/stats").json() == {"total": 1, "by_health": {"unknown": 1}}
