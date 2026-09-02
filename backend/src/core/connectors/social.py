"""댓글 · 별 · 북마크 · 알림의 SQL.

**세 가지 규약이 여기 모여 있다.**

1. 별·북마크는 insert/delete 로만 다룬다. `(카드, 사용자)` 가 PK 라 그 자체가 멱등성이다.
   토글을 "읽고 뒤집어 쓴다" 로 구현하면 같은 사용자의 동시 요청이 서로를 덮고, 사용자는
   방금 누른 별이 사라지는 것을 본다.
2. 댓글 삭제는 soft delete 다. 답글이 달린 댓글을 물리 삭제하면 CASCADE 가 답글까지
   지운다 — 남의 글이 내 삭제로 사라지면 안 된다.
3. 알림은 **행위자 본인에게는 보내지 않는다.** 자기 댓글로 자기 알림이 쌓이면 알림 자체를
   안 보게 된다.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

MAX_COMMENT_PAGE = 100

# 댓글은 작성자 표시 이름을 함께 읽는다. `connector_users` 는 표시용 projection 이라
# 비어 있을 수 있다 — LEFT JOIN 이고, 없으면 화면이 user_id 로 대신 표시한다.
_COMMENT_COLUMNS = """
    cm.comment_id::text AS comment_id,
    cm.parent_id::text AS parent_id,
    cm.author_id,
    u.display_name AS author_display_name,
    CASE WHEN cm.deleted_at IS NULL THEN cm.body END AS body,
    cm.deleted_at IS NOT NULL AS deleted,
    cm.created_at, cm.updated_at
"""

# 삭제된 댓글은 **살아 있는 답글이 달려 있을 때만** 남긴다. 그때 지워 버리면 답글이
# 맥락 없이 뜨고, 반대로 늘 남기면 지운 흔적만 쌓인다.
_LIVE = """
    (cm.deleted_at IS NULL
     OR EXISTS (SELECT 1 FROM connector_comments r
                WHERE r.parent_id = cm.comment_id AND r.deleted_at IS NULL))
"""


async def list_comments(
    session: AsyncSession, *, connector_id: str, limit: int, offset: int
) -> tuple[list[dict[str, Any]], int]:
    """한 카드의 댓글 스레드 → (최상위 댓글 + 답글, 최상위 총 개수).

    페이지는 **최상위 댓글 기준**이다. 답글까지 섞어 자르면 페이지 경계에서 부모 없는
    답글이 나온다. 답글은 개수가 적어 부모와 함께 통째로 싣는다.
    """
    roots = (
        (
            await session.execute(
                text(f"""
                SELECT {_COMMENT_COLUMNS}
                FROM connector_comments cm
                LEFT JOIN connector_users u ON u.user_id = cm.author_id
                WHERE cm.connector_id = CAST(:c AS uuid) AND cm.parent_id IS NULL AND {_LIVE}
                ORDER BY cm.created_at ASC, cm.comment_id ASC
                LIMIT :limit OFFSET :offset
            """),
                {"c": connector_id, "limit": limit, "offset": offset},
            )
        )
        .mappings()
        .all()
    )
    total = (
        await session.execute(
            text(f"""
            SELECT count(*) FROM connector_comments cm
            WHERE cm.connector_id = CAST(:c AS uuid) AND cm.parent_id IS NULL AND {_LIVE}
        """),
            {"c": connector_id},
        )
    ).scalar_one()

    root_ids = [r["comment_id"] for r in roots]
    replies: list[dict[str, Any]] = []
    if root_ids:
        replies = [
            dict(r)
            for r in (
                await session.execute(
                    text(f"""
                    SELECT {_COMMENT_COLUMNS}
                    FROM connector_comments cm
                    LEFT JOIN connector_users u ON u.user_id = cm.author_id
                    WHERE cm.parent_id = ANY(CAST(:ids AS uuid[])) AND cm.deleted_at IS NULL
                    ORDER BY cm.created_at ASC, cm.comment_id ASC
                """),
                    {"ids": root_ids},
                )
            )
            .mappings()
            .all()
        ]

    by_parent: dict[str, list[dict[str, Any]]] = {}
    for reply in replies:
        by_parent.setdefault(reply["parent_id"], []).append(reply)
    threads = [{**dict(r), "replies": by_parent.get(r["comment_id"], [])} for r in roots]
    return threads, int(total)


async def read_comment(session: AsyncSession, *, comment_id: str) -> dict[str, Any] | None:
    """표시용 단건. 작성·수정 응답이 목록과 **같은 모양**을 돌려주게 한다."""
    row = (
        (
            await session.execute(
                text(f"""
                SELECT {_COMMENT_COLUMNS}
                FROM connector_comments cm
                LEFT JOIN connector_users u ON u.user_id = cm.author_id
                WHERE cm.comment_id = CAST(:k AS uuid)
            """),
                {"k": comment_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def get_comment(session: AsyncSession, *, comment_id: str) -> dict[str, Any] | None:
    """권한 판정용 원본. **가시성을 보지 않는다** — 소유자 판정과 조회 판정은 다른 질문이다."""
    row = (
        (
            await session.execute(
                text(
                    "SELECT comment_id::text AS comment_id, connector_id::text AS connector_id, "
                    "parent_id::text AS parent_id, author_id, deleted_at "
                    "FROM connector_comments WHERE comment_id = CAST(:k AS uuid)"
                ),
                {"k": comment_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def create_comment(
    session: AsyncSession, *, connector_id: str, author_id: str, body: str, parent_id: str | None
) -> str:
    comment_id = (
        await session.execute(
            text("""
            INSERT INTO connector_comments (connector_id, parent_id, author_id, body)
            VALUES (CAST(:c AS uuid), CAST(:p AS uuid), :a, :b)
            RETURNING comment_id::text
        """),
            {"c": connector_id, "p": parent_id, "a": author_id, "b": body},
        )
    ).scalar_one()
    return str(comment_id)


async def update_comment(session: AsyncSession, *, comment_id: str, body: str) -> None:
    await session.execute(
        text(
            "UPDATE connector_comments SET body = :b, updated_at = now() "
            "WHERE comment_id = CAST(:k AS uuid) AND deleted_at IS NULL"
        ),
        {"k": comment_id, "b": body},
    )


async def soft_delete_comment(session: AsyncSession, *, comment_id: str) -> None:
    """본문을 비우고 삭제 표시만 남긴다.

    물리 삭제하면 FK CASCADE 가 답글까지 지운다 — 내 삭제로 남의 글이 사라진다. 본문을
    함께 비우는 이유는 "삭제했는데 DB 에는 남아 있다" 가 사용자의 기대와 다르기 때문이다.
    """
    await session.execute(
        text(
            "UPDATE connector_comments SET deleted_at = now(), body = '', updated_at = now() "
            "WHERE comment_id = CAST(:k AS uuid) AND deleted_at IS NULL"
        ),
        {"k": comment_id},
    )


# ---- 별 · 북마크 ---------------------------------------------------------------------------


async def set_reaction(
    session: AsyncSession, *, table: str, connector_id: str, user_id: str, on: bool
) -> None:
    """별·북마크를 켜거나 끈다. **두 번 켜도 결과가 같다.**

    `table` 은 호출부가 고르는 리터럴 둘뿐이라 문자열로 조립해도 사용자 입력이 닿지 않는다.
    """
    if table not in ("connector_stars", "connector_bookmarks"):  # pragma: no cover - 방어
        raise ValueError(table)
    if on:
        await session.execute(
            text(
                f"INSERT INTO {table} (connector_id, user_id) VALUES (CAST(:c AS uuid), :u) "
                "ON CONFLICT DO NOTHING"
            ),
            {"c": connector_id, "u": user_id},
        )
    else:
        await session.execute(
            text(f"DELETE FROM {table} WHERE connector_id = CAST(:c AS uuid) AND user_id = :u"),
            {"c": connector_id, "u": user_id},
        )


async def count_reaction(session: AsyncSession, *, table: str, connector_id: str) -> int:
    if table not in ("connector_stars", "connector_bookmarks"):  # pragma: no cover - 방어
        raise ValueError(table)
    return int(
        (
            await session.execute(
                text(f"SELECT count(*) FROM {table} WHERE connector_id = CAST(:c AS uuid)"),
                {"c": connector_id},
            )
        ).scalar_one()
    )


# ---- 알림 ----------------------------------------------------------------------------------

#: 이 서비스가 만드는 알림 종류. **AgentToolbox 알림과 합산하지 않는다**(설계 §12) —
#: 두 화면의 알림 메뉴는 같은 모양이지만 각자 자기 것만 보여 준다.
#:
#: DB 에 CHECK 를 걸지 않는다. 이관해 온 행이 AgentToolbox 어휘(`qa_comment` 등)를 들고
#: 올 수 있고, 그때 CHECK 가 import 를 막는다. 대신 응답 모델도 `kind` 를 열린 문자열로
#: 둬서 모르는 종류가 500 이 아니라 그냥 렌더되게 한다.
KIND_COMMENT = "comment"
KIND_REPLY = "comment_reply"


async def notify(
    session: AsyncSession,
    *,
    user_id: str,
    actor_id: str,
    connector_id: str,
    kind: str,
    payload: dict[str, Any],
) -> None:
    """알림 한 건. **행위자 본인이면 만들지 않는다.**

    자기 댓글로 자기 알림이 쌓이면 알림 목록이 자기 활동 로그가 되고, 그러면 사람이 알림을
    안 본다. 조용히 건너뛰는 것이 맞다 — 호출부가 매번 같은 조건을 쓰게 하면 언젠가 한
    군데가 빠진다.
    """
    if user_id == actor_id:
        return
    await session.execute(
        text("""
            INSERT INTO connector_notifications (user_id, connector_id, kind, payload)
            VALUES (:u, CAST(:c AS uuid), :k, CAST(:p AS jsonb))
        """),
        {
            "u": user_id,
            "c": connector_id,
            "k": kind,
            "p": json.dumps({**payload, "actor_id": actor_id}),
        },
    )


async def list_notifications(
    session: AsyncSession, *, user_id: str, unread_only: bool, limit: int, offset: int
) -> tuple[list[dict[str, Any]], int]:
    where = "n.user_id = :u" + (" AND n.read_at IS NULL" if unread_only else "")
    rows = (
        (
            await session.execute(
                text(f"""
                SELECT n.notification_id::text AS notification_id,
                       n.connector_id::text AS connector_id,
                       c.short_id AS connector_short_id, c.name AS connector_name,
                       n.kind, n.payload, n.read_at, n.created_at
                FROM connector_notifications n
                LEFT JOIN connectors c ON c.connector_id = n.connector_id
                WHERE {where}
                ORDER BY n.created_at DESC, n.notification_id DESC
                LIMIT :limit OFFSET :offset
            """),
                {"u": user_id, "limit": limit, "offset": offset},
            )
        )
        .mappings()
        .all()
    )
    unread = (
        await session.execute(
            text(
                "SELECT count(*) FROM connector_notifications "
                "WHERE user_id = :u AND read_at IS NULL"
            ),
            {"u": user_id},
        )
    ).scalar_one()
    result = []
    for row in rows:
        item = dict(row)
        if isinstance(item["payload"], str):
            item["payload"] = json.loads(item["payload"])
        result.append(item)
    return result, int(unread)


async def mark_read(session: AsyncSession, *, user_id: str, notification_id: str | None) -> int:
    """읽음 처리 → 실제로 바뀐 건수.

    `notification_id` 가 없으면 전부 읽음이다. **이미 읽은 것은 건드리지 않는다** —
    `read_at` 을 다시 쓰면 "언제 읽었는가" 가 매번 바뀐다.
    """
    where = "user_id = :u AND read_at IS NULL"
    params: dict[str, Any] = {"u": user_id}
    if notification_id is not None:
        where += " AND notification_id = CAST(:n AS uuid)"
        params["n"] = notification_id
    rows = (
        await session.execute(
            text(
                f"UPDATE connector_notifications SET read_at = now() WHERE {where} "
                "RETURNING notification_id"
            ),
            params,
        )
    ).all()
    return len(rows)


# ---- 모더레이터 ------------------------------------------------------------------------------


async def is_moderator(session: AsyncSession, *, user_id: str | None) -> bool:
    """이 서비스의 모더레이터인가. **사이트 JWT 의 `role` 을 보지 않는다**(마이그레이션 0003)."""
    if not user_id:
        return False
    row = (
        await session.execute(
            text("SELECT 1 FROM connector_moderators WHERE user_id = :u"), {"u": user_id}
        )
    ).first()
    return row is not None
