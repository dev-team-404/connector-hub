"""댓글 · 별 · 북마크 · 알림 (connector-hub#4).

세 가지를 본다. **누설** — 카드를 못 보는 사람에게 그 댓글이 보이면 안 된다. **멱등성** —
같은 요청을 두 번 보내도 결과가 같아야 한다. **알림의 주인** — 이 서비스 알림은 이 서비스
것만 담고, 행위자 자신에게는 가지 않는다.
"""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
import pytest

from tests.conftest_db import (
    REQUIRES_DB,
    Viewer,
    _sync_url,
    make_client,
    truncate_connectors,
    worker_engine,
)

pytestmark = REQUIRES_DB

OWNER = Viewer(sub="u_owner", team_codes=("team-a",))
TEAMMATE = Viewer(sub="u_mate", team_codes=("team-a",))
TEAMMATE2 = Viewer(sub="u_mate2", team_codes=("team-a",))
OUTSIDER = Viewer(sub="u_out", team_codes=("team-z",))
MOD = Viewer(sub="u_mod", team_codes=("team-a",))


@pytest.fixture(autouse=True)
def _clean():
    _wipe_moderators()
    truncate_connectors()
    yield
    truncate_connectors()
    _wipe_moderators()


def _wipe_moderators() -> None:
    with psycopg.connect(_sync_url(), autocommit=True) as conn:
        conn.execute("DELETE FROM connector_moderators WHERE user_id LIKE 'u_%%'")


def _grant_moderator(user_id: str) -> None:
    with psycopg.connect(_sync_url(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO connector_moderators (user_id, note) VALUES (%s, 'test') "
            "ON CONFLICT DO NOTHING",
            (user_id,),
        )


def _body(**over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "sample-connector",
        "short_description": "요약",
        "category": "productivity",
        "source_repo_url": "https://repo.test/mcp",
        "endpoint_url": None,
        "transport": None,
        "scope_type": "team",
        "scope_id": "team-a",
        "tags": [],
        "visibility_teams": [],
    }
    body.update(over)
    return body


def _create(viewer: Viewer = OWNER, **over: object) -> dict:
    with make_client(viewer) as client:
        resp = client.post("/connectors", json=_body(**over))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _comment(viewer: Viewer, connector_id: str, body: str, parent: str | None = None) -> dict:
    with make_client(viewer) as client:
        resp = client.post(
            f"/connectors/{connector_id}/comments",
            json={"body": body, "parent_id": parent},
        )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _threads(viewer: Viewer | None, connector_id: str) -> dict:
    with make_client(viewer) as client:
        resp = client.get(f"/connectors/{connector_id}/comments")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _notifications(viewer: Viewer, **params: Any) -> dict:
    with make_client(viewer) as client:
        resp = client.get("/me/notifications", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---- 댓글 ----------------------------------------------------------------------------------


def test_comment_and_reply_form_a_two_level_thread() -> None:
    card = _create()
    root = _comment(TEAMMATE, card["connector_id"], "질문이 있다")
    _comment(OWNER, card["connector_id"], "답한다", parent=root["comment_id"])

    page = _threads(TEAMMATE, card["connector_id"])
    assert page["total"] == 1  # 최상위 기준 — 답글까지 세면 화면의 스레드 수와 어긋난다
    assert len(page["items"]) == 1
    assert page["items"][0]["body"] == "질문이 있다"
    assert [r["body"] for r in page["items"][0]["replies"]] == ["답한다"]


def test_reply_to_a_reply_is_refused() -> None:
    """DB 는 임의 깊이를 허용하지만 화면이 렌더할 수 있는 것은 두 단계다."""
    card = _create()
    root = _comment(TEAMMATE, card["connector_id"], "질문")
    reply = _comment(OWNER, card["connector_id"], "답", parent=root["comment_id"])
    with make_client(TEAMMATE) as client:
        resp = client.post(
            f"/connectors/{card['connector_id']}/comments",
            json={"body": "또 답", "parent_id": reply["comment_id"]},
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "nested_reply"


def test_reply_must_belong_to_the_same_connector() -> None:
    """다른 카드의 댓글을 부모로 삼으면 비공개 카드의 댓글 id 를 확인하는 통로가 된다."""
    a = _create()
    b = _create(name="other")
    root = _comment(OWNER, a["connector_id"], "a 의 댓글")
    with make_client(TEAMMATE) as client:
        resp = client.post(
            f"/connectors/{b['connector_id']}/comments",
            json={"body": "엉뚱한 부모", "parent_id": root["comment_id"]},
        )
    assert resp.status_code == 404


def test_comments_of_an_invisible_card_are_a_plain_404() -> None:
    card = _create()
    author = _comment(OWNER, card["connector_id"], "비공개 카드의 댓글")
    with make_client(OUTSIDER) as client:
        assert client.get(f"/connectors/{card['connector_id']}/comments").status_code == 404
        assert (
            client.post(
                f"/connectors/{card['connector_id']}/comments", json={"body": "끼어들기"}
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"/connectors/{card['connector_id']}/comments/{author['comment_id']}"
            ).status_code
            == 404
        )


def test_anonymous_cannot_comment() -> None:
    card = _create(scope_type="global", scope_id=None)
    with make_client(None) as client:
        assert client.get(f"/connectors/{card['connector_id']}/comments").status_code == 200
        assert (
            client.post(
                f"/connectors/{card['connector_id']}/comments", json={"body": "익명"}
            ).status_code
            == 401
        )


def test_only_the_author_can_edit() -> None:
    """모더레이터도 남의 글을 고치지는 못한다 — 지우는 것과 고치는 것은 다르다."""
    card = _create()
    comment = _comment(TEAMMATE, card["connector_id"], "원문")
    _grant_moderator(MOD.sub)

    with make_client(TEAMMATE) as client:
        ok = client.patch(
            f"/connectors/{card['connector_id']}/comments/{comment['comment_id']}",
            json={"body": "고친 글"},
        )
    assert ok.status_code == 200
    assert ok.json()["body"] == "고친 글"

    for viewer in (OWNER, MOD):
        with make_client(viewer) as client:
            resp = client.patch(
                f"/connectors/{card['connector_id']}/comments/{comment['comment_id']}",
                json={"body": "위조"},
            )
        assert resp.status_code == 404


def test_a_moderator_can_delete_but_a_stranger_cannot() -> None:
    """모더레이터 판정은 이 서비스 DB 에서 한다 — 사이트 JWT 의 role 을 보지 않는다."""
    card = _create()
    comment = _comment(TEAMMATE, card["connector_id"], "지울 글")

    with make_client(OWNER) as client:  # 카드 주인이라도 남의 댓글은 못 지운다
        assert (
            client.delete(
                f"/connectors/{card['connector_id']}/comments/{comment['comment_id']}"
            ).status_code
            == 404
        )

    _grant_moderator(MOD.sub)
    with make_client(MOD) as client:
        assert (
            client.delete(
                f"/connectors/{card['connector_id']}/comments/{comment['comment_id']}"
            ).status_code
            == 204
        )


def test_the_site_role_does_not_make_a_moderator() -> None:
    """AgentToolbox 가 admin 이라고 해서 Connector 를 조정해도 된다는 뜻은 아니다."""
    card = _create()
    comment = _comment(TEAMMATE, card["connector_id"], "지울 글")
    site_admin = Viewer(sub="u_siteadmin", team_codes=("team-a",), role="admin")
    with make_client(site_admin) as client:
        resp = client.delete(f"/connectors/{card['connector_id']}/comments/{comment['comment_id']}")
    assert resp.status_code == 404


def test_deleting_a_comment_keeps_its_replies() -> None:
    """물리 삭제하면 CASCADE 가 답글까지 지운다 — 내 삭제로 남의 글이 사라지면 안 된다."""
    card = _create()
    root = _comment(TEAMMATE, card["connector_id"], "지울 질문")
    _comment(OWNER, card["connector_id"], "남을 답", parent=root["comment_id"])

    with make_client(TEAMMATE) as client:
        client.delete(f"/connectors/{card['connector_id']}/comments/{root['comment_id']}")

    page = _threads(OWNER, card["connector_id"])
    assert page["items"][0]["deleted"] is True
    assert page["items"][0]["body"] is None  # 본문은 남기지 않는다
    assert [r["body"] for r in page["items"][0]["replies"]] == ["남을 답"]


def test_a_deleted_comment_without_replies_disappears() -> None:
    """자리만 남길 이유가 없으면 남기지 않는다 — 지운 흔적만 쌓이면 스레드가 읽기 어렵다."""
    card = _create()
    comment = _comment(TEAMMATE, card["connector_id"], "혼잣말")
    with make_client(TEAMMATE) as client:
        client.delete(f"/connectors/{card['connector_id']}/comments/{comment['comment_id']}")
    page = _threads(OWNER, card["connector_id"])
    assert page["items"] == []
    assert page["total"] == 0


def test_comment_count_appears_on_the_card() -> None:
    card = _create()
    root = _comment(TEAMMATE, card["connector_id"], "1")
    _comment(OWNER, card["connector_id"], "2", parent=root["comment_id"])
    with make_client(OWNER) as client:
        assert client.get(f"/connectors/{card['connector_id']}").json()["comment_count"] == 2


# ---- 별 · 북마크 ----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["star", "bookmark"])
def test_reaction_is_idempotent(kind: str) -> None:
    """재전송이 취소가 되면 안 된다 — 응답이 늦어 한 번 더 누른 사용자가 방금 켠 것을 잃는다."""
    card = _create()
    path = f"/connectors/{card['connector_id']}/{kind}"
    with make_client(TEAMMATE) as client:
        first = client.put(path).json()
        second = client.put(path).json()
        assert first == second == {"on": True, "count": 1}

        off = client.delete(path).json()
        again = client.delete(path).json()
        assert off == again == {"on": False, "count": 0}


def test_star_state_is_per_viewer() -> None:
    card = _create()
    with make_client(TEAMMATE) as client:
        client.put(f"/connectors/{card['connector_id']}/star")
    with make_client(TEAMMATE) as client:
        mine = client.get(f"/connectors/{card['connector_id']}").json()
    with make_client(OWNER) as client:
        theirs = client.get(f"/connectors/{card['connector_id']}").json()
    assert mine["starred"] is True
    assert theirs["starred"] is False
    assert theirs["star_count"] == 1  # 개수는 모두에게 같다


async def test_concurrent_stars_do_not_duplicate() -> None:
    """`(카드, 사용자)` PK 가 곧 멱등성이다. 읽고 뒤집어 쓰는 구현이면 여기서 갈라진다."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from core.connectors import social

    card = _create()
    connector_id = card["connector_id"]

    async with worker_engine() as engine:
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def _star() -> None:
            async with factory() as session:
                await social.set_reaction(
                    session,
                    table="connector_stars",
                    connector_id=connector_id,
                    user_id="u_racer",
                    on=True,
                )
                await session.commit()

        await asyncio.gather(*(_star() for _ in range(8)))

        async with factory() as session:
            count = await social.count_reaction(
                session, table="connector_stars", connector_id=connector_id
            )
    assert count == 1


def test_bookmarks_are_listed_for_later() -> None:
    card = _create()
    with make_client(TEAMMATE) as client:
        client.put(f"/connectors/{card['connector_id']}/bookmark")
        listed = client.get("/me/bookmarks").json()["items"]
    assert [c["connector_id"] for c in listed] == [card["connector_id"]]
    assert listed[0]["bookmarked"] is True


def test_a_bookmark_does_not_grant_access() -> None:
    """북마크한 뒤 카드가 비공개로 바뀌면 목록에서도 사라져야 한다 — 북마크는 표시지 권한이 아니다."""
    card = _create(scope_type="global", scope_id=None)
    with make_client(OUTSIDER) as client:
        client.put(f"/connectors/{card['connector_id']}/bookmark")
        assert len(client.get("/me/bookmarks").json()["items"]) == 1

    with make_client(OWNER) as client:
        resp = client.patch(
            f"/connectors/{card['connector_id']}",
            json=_body(scope_type="team", scope_id="team-a"),
        )
        assert resp.status_code == 200

    with make_client(OUTSIDER) as client:
        assert client.get("/me/bookmarks").json()["items"] == []


# ---- 알림 ----------------------------------------------------------------------------------


def test_a_comment_notifies_the_card_owner() -> None:
    card = _create()
    _comment(TEAMMATE, card["connector_id"], "질문")

    inbox = _notifications(OWNER)
    assert inbox["unread"] == 1
    assert inbox["items"][0]["kind"] == "comment"
    assert inbox["items"][0]["connector_id"] == card["connector_id"]
    assert inbox["items"][0]["payload"]["actor_id"] == TEAMMATE.sub
    assert inbox["items"][0]["payload"]["excerpt"] == "질문"


def test_you_are_not_notified_of_your_own_comment() -> None:
    """자기 활동이 알림으로 쌓이면 사람이 알림을 안 보게 된다."""
    card = _create()
    _comment(OWNER, card["connector_id"], "내 카드에 내 댓글")
    assert _notifications(OWNER)["unread"] == 0


def test_a_reply_notifies_both_the_parent_author_and_the_owner() -> None:
    card = _create()
    root = _comment(TEAMMATE, card["connector_id"], "질문")
    _comment(TEAMMATE2, card["connector_id"], "끼어든 답", parent=root["comment_id"])

    assert [n["kind"] for n in _notifications(TEAMMATE)["items"]] == ["comment_reply"]
    # 카드 주인은 최초 댓글과 답글로 두 건.
    assert _notifications(OWNER)["unread"] == 2


def test_a_reply_to_the_owners_own_comment_notifies_once() -> None:
    """부모 작성자와 카드 주인이 같은 사람이면 한 번만 간다."""
    card = _create()
    root = _comment(OWNER, card["connector_id"], "내 카드에 내 댓글")
    _comment(TEAMMATE, card["connector_id"], "답", parent=root["comment_id"])
    inbox = _notifications(OWNER)
    assert inbox["unread"] == 1
    assert inbox["items"][0]["kind"] == "comment_reply"


def test_notifications_are_private_to_their_owner() -> None:
    card = _create()
    _comment(TEAMMATE, card["connector_id"], "질문")
    assert _notifications(TEAMMATE)["items"] == []
    assert _notifications(OUTSIDER)["items"] == []


def test_reading_marks_only_what_was_asked() -> None:
    card = _create()
    _comment(TEAMMATE, card["connector_id"], "하나")
    _comment(TEAMMATE2, card["connector_id"], "둘")
    inbox = _notifications(OWNER)
    assert inbox["unread"] == 2

    first = inbox["items"][0]["notification_id"]
    with make_client(OWNER) as client:
        after = client.post(f"/me/notifications/{first}/read").json()
    assert after["unread"] == 1

    with make_client(OWNER) as client:
        assert client.post("/me/notifications/read").json()["unread"] == 0


def test_unread_only_filters_the_list_but_not_the_badge() -> None:
    """`unread` 는 헤더 배지가 쓰는 값이라 현재 페이지 내용으로 바뀌면 안 된다."""
    card = _create()
    _comment(TEAMMATE, card["connector_id"], "하나")
    inbox = _notifications(OWNER)
    with make_client(OWNER) as client:
        client.post(f"/me/notifications/{inbox['items'][0]['notification_id']}/read")
    _comment(TEAMMATE2, card["connector_id"], "둘")

    unread_only = _notifications(OWNER, unread_only=True)
    assert len(unread_only["items"]) == 1
    assert unread_only["unread"] == 1
    assert len(_notifications(OWNER)["items"]) == 2


def test_reading_someone_elses_notification_changes_nothing() -> None:
    card = _create()
    _comment(TEAMMATE, card["connector_id"], "질문")
    target = _notifications(OWNER)["items"][0]["notification_id"]
    with make_client(OUTSIDER) as client:
        assert client.post(f"/me/notifications/{target}/read").status_code == 200
    assert _notifications(OWNER)["unread"] == 1


def test_deleting_a_connector_takes_its_notifications() -> None:
    """알림은 카드에 딸린 것이다 — 카드가 사라졌는데 알림만 남으면 링크가 죽는다."""
    card = _create()
    _comment(TEAMMATE, card["connector_id"], "질문")
    assert _notifications(OWNER)["unread"] == 1
    with make_client(OWNER) as client:
        assert client.delete(f"/connectors/{card['connector_id']}").status_code == 204
    assert _notifications(OWNER)["unread"] == 0
