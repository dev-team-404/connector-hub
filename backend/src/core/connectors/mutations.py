"""Connector 쓰기 경로.

카드 본문과 태그·공개범위는 **한 트랜잭션**에서 함께 바뀐다. 나누면 태그만 바뀌고 본문은
안 바뀐 중간 상태가 사용자에게 보인다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_INSERT = text("""
    INSERT INTO connectors (
        name, title, short_description, description, category, license,
        source_repo_url, endpoint_url, transport,
        scope_type, scope_id, creator_user_id, creator_team_id
    ) VALUES (
        :name, :title, :short_description, :description, :category, :license,
        :source_repo_url, :endpoint_url, :transport,
        :scope_type, :scope_id, :creator_user_id, :creator_team_id
    )
    RETURNING connector_id::text AS connector_id
""")

# 갱신 가능한 컬럼만 나열한다. `creator_user_id`·`created_at` 은 여기 없다 — 목록에
# 없으면 실수로도 바뀌지 않는다.
_UPDATE = text("""
    UPDATE connectors SET
        name = :name, title = :title, short_description = :short_description,
        description = :description, category = :category, license = :license,
        source_repo_url = :source_repo_url, endpoint_url = :endpoint_url,
        transport = :transport, scope_type = :scope_type, scope_id = :scope_id,
        updated_at = now()
    WHERE connector_id = CAST(:connector_id AS uuid)
""")


async def _replace_children(
    session: AsyncSession, *, connector_id: str, tags: list[str], visibility_teams: list[str]
) -> None:
    """태그·공개범위를 통째로 교체한다.

    delta 계산 대신 삭제 후 삽입이다. 목록이 짧고(태그 20개·팀 50개 상한) delta 로직은
    "지운 줄 알았는데 남아 있는" 부류의 버그를 부른다 — 그 버그는 공개범위에서 나면
    비공개 카드가 계속 보이는 결과가 된다.
    """
    await session.execute(
        text("DELETE FROM connector_tags WHERE connector_id = CAST(:c AS uuid)"),
        {"c": connector_id},
    )
    if tags:
        await session.execute(
            text(
                "INSERT INTO connector_tags (connector_id, tag) "
                "SELECT CAST(:c AS uuid), unnest(CAST(:tags AS text[]))"
            ),
            {"c": connector_id, "tags": tags},
        )
    await session.execute(
        text("DELETE FROM connector_visibility_teams WHERE connector_id = CAST(:c AS uuid)"),
        {"c": connector_id},
    )
    if visibility_teams:
        await session.execute(
            text(
                "INSERT INTO connector_visibility_teams (connector_id, team_code) "
                "SELECT CAST(:c AS uuid), unnest(CAST(:teams AS text[]))"
            ),
            {"c": connector_id, "teams": visibility_teams},
        )


async def create_connector(
    session: AsyncSession,
    *,
    payload: dict[str, Any],
    tags: list[str],
    visibility_teams: list[str],
    creator_user_id: str,
    creator_team_id: str,
) -> str:
    connector_id = (
        await session.execute(
            _INSERT,
            {**payload, "creator_user_id": creator_user_id, "creator_team_id": creator_team_id},
        )
    ).scalar_one()
    await _replace_children(
        session, connector_id=connector_id, tags=tags, visibility_teams=visibility_teams
    )
    # health 행을 함께 만든다. 없으면 목록이 LEFT JOIN 으로 'unknown' 을 보여 주긴 하지만,
    # liveness worker 가 "아직 한 번도 안 본 카드" 를 고를 축이 사라진다.
    await session.execute(
        text("INSERT INTO connector_health_checks (connector_id) VALUES (CAST(:c AS uuid))"),
        {"c": connector_id},
    )
    await session.commit()
    return str(connector_id)


async def update_connector(
    session: AsyncSession,
    *,
    connector_id: str,
    payload: dict[str, Any],
    tags: list[str],
    visibility_teams: list[str],
) -> None:
    await session.execute(_UPDATE, {**payload, "connector_id": connector_id})
    await _replace_children(
        session, connector_id=connector_id, tags=tags, visibility_teams=visibility_teams
    )
    await session.commit()


async def delete_connector(session: AsyncSession, *, connector_id: str) -> None:
    """물리 삭제. 딸린 행은 FK CASCADE 가 지운다.

    soft delete 를 쓰지 않는 이유는 `archived_at` 이 이미 "보이지 않게 하기" 를 맡고 있기
    때문이다. 두 가지 숨김 상태가 있으면 어느 쪽이 무엇을 뜻하는지 곧 흐려진다.
    """
    await session.execute(
        text("DELETE FROM connectors WHERE connector_id = CAST(:c AS uuid)"), {"c": connector_id}
    )
    await session.commit()


async def get_owner(session: AsyncSession, *, connector_id: str) -> str | None:
    """권한 확인용. **가시성을 보지 않는다** — 소유자 판정과 조회 판정은 다른 질문이다."""
    is_uuid = len(connector_id) == 36 and connector_id.count("-") == 4
    if not is_uuid:
        row = (
            await session.execute(
                text("SELECT creator_user_id FROM connectors WHERE short_id = :k"),
                {"k": connector_id},
            )
        ).first()
    else:
        row = (
            await session.execute(
                text(
                    "SELECT creator_user_id FROM connectors WHERE connector_id = CAST(:k AS uuid)"
                ),
                {"k": connector_id},
            )
        ).first()
    return str(row[0]) if row else None
