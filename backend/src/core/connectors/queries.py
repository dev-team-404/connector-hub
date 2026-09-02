"""Connector 읽기·쓰기 SQL.

**AgentToolbox 의 카탈로그 SQL 을 옮겨 오지 않았다.** 그쪽은 `items` 다형성과 컴포넌트
조인 위에 세워져 있어 여기서는 맞지 않고, 그대로 들여오면 필요 없는 복잡도가 함께 온다.

모든 읽기는 `visibility.VISIBLE_PREDICATE` 를 거친다. 술어를 여기 한 곳에서만 끼워
넣으므로 새 조회를 추가할 때 빠뜨릴 자리가 없다.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import text

from core.connectors.visibility import VISIBLE_PREDICATE, viewer_params

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25

# 목록 카드가 읽는 컬럼. 상세 전용 큰 필드(description·config·manifest)는 빼서 목록 응답이
# 카드 수에 비례해 부풀지 않게 한다.
_SUMMARY_COLUMNS = """
    c.connector_id::text AS connector_id, c.short_id, c.name, c.title,
    c.short_description, c.category, c.transport, c.endpoint_url,
    c.scope_type, c.scope_id, c.creator_user_id, c.verified_status,
    COALESCE(h.health_status, 'unknown') AS health_status, h.last_checked_at,
    c.created_at, c.updated_at,
    COALESCE(t.tags, ARRAY[]::text[]) AS tags,
    COALESCE(s.star_count, 0) AS star_count
"""

# 태그와 별 개수는 상관 서브쿼리 대신 LATERAL 집계로 붙인다. 카드마다 서브쿼리를 도는 대신
# 한 번씩만 집계한다.
_SUMMARY_JOINS = """
    FROM connectors c
    LEFT JOIN connector_health_checks h ON h.connector_id = c.connector_id
    LEFT JOIN LATERAL (
        SELECT array_agg(ct.tag ORDER BY ct.tag) AS tags
        FROM connector_tags ct WHERE ct.connector_id = c.connector_id
    ) t ON TRUE
    LEFT JOIN LATERAL (
        SELECT count(*) AS star_count
        FROM connector_stars cs WHERE cs.connector_id = c.connector_id
    ) s ON TRUE
"""

SortKey = Literal["recent", "name"]

# 정렬 축마다 **동점 처리에 connector_id 를 붙인다.** 없으면 같은 값을 가진 행들의 순서가
# 페이지마다 달라져 키셋 페이징이 항목을 건너뛰거나 되풀이한다.
_ORDER_BY: dict[str, str] = {
    "recent": "c.created_at DESC, c.connector_id DESC",
    "name": "c.name ASC, c.connector_id ASC",
}
_CURSOR_COMPARE: dict[str, str] = {
    "recent": "(c.created_at, c.connector_id) < (:cursor_value, :cursor_id)",
    "name": "(c.name, c.connector_id) > (:cursor_value, :cursor_id)",
}


class CursorError(ValueError):
    """커서를 해석할 수 없을 때 — 400 으로 돌려준다(500 이 아니다)."""


def encode_cursor(sort: str, value: Any, connector_id: str) -> str:
    payload = {
        "s": sort,
        "v": value.isoformat() if isinstance(value, datetime) else value,
        "i": connector_id,
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def decode_cursor(cursor: str, sort: str) -> tuple[Any, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
        value, connector_id, cursor_sort = payload["v"], payload["i"], payload["s"]
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CursorError("커서를 해석할 수 없다") from exc
    if cursor_sort != sort:
        # 정렬을 바꾸면서 이전 커서를 그대로 쓰면 결과가 조용히 뒤섞인다. 거절하는 편이 낫다.
        raise CursorError(f"커서는 sort={cursor_sort!r} 로 만들어졌다 (요청은 {sort!r})")
    if sort == "recent":
        try:
            value = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise CursorError("커서 값이 시각이 아니다") from exc
    return value, connector_id


async def list_connectors(
    session: AsyncSession,
    *,
    viewer_id: str | None,
    viewer_teams: tuple[str, ...] | None,
    q: str | None = None,
    tags: tuple[str, ...] = (),
    sort: SortKey = "recent",
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """카탈로그 목록. 반환은 (행, 다음 커서)."""
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    params: dict[str, Any] = {**viewer_params(viewer_id, viewer_teams), "limit": limit + 1}
    where = [VISIBLE_PREDICATE, "c.archived_at IS NULL"]

    if q:
        # 이름·요약은 tsvector 인덱스를 타고, 태그는 별도 EXISTS 로 본다. 하나의 OR 덩어리로
        # 묶으면 인덱스를 통째로 잃는다(AgentToolbox #2928 이 그 함정을 겪었다).
        where.append(
            "(c.search_tsv @@ plainto_tsquery('simple', :q)"
            " OR EXISTS (SELECT 1 FROM connector_tags ct2"
            "            WHERE ct2.connector_id = c.connector_id AND ct2.tag = lower(:q)))"
        )
        params["q"] = q
    if tags:
        # 지정한 태그를 **모두** 가진 카드. 하나라도 가진 카드가 아니다 — 필터를 더할수록
        # 결과가 넓어지면 사용자는 그것을 필터로 인식하지 못한다.
        where.append(
            "(SELECT count(DISTINCT ct3.tag) FROM connector_tags ct3"
            "  WHERE ct3.connector_id = c.connector_id AND ct3.tag = ANY(:tags)) = :tag_count"
        )
        params["tags"] = list(tags)
        params["tag_count"] = len(set(tags))
    if cursor:
        value, cursor_id = decode_cursor(cursor, sort)
        where.append(_CURSOR_COMPARE[sort])
        params["cursor_value"] = value
        params["cursor_id"] = cursor_id

    sql = (
        f"SELECT {_SUMMARY_COLUMNS} {_SUMMARY_JOINS} "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY {_ORDER_BY[sort]} LIMIT :limit"
    )
    rows = [dict(r) for r in (await session.execute(text(sql), params)).mappings().all()]

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        key = last["created_at"] if sort == "recent" else last["name"]
        next_cursor = encode_cursor(sort, key, last["connector_id"])
    return rows, next_cursor


async def get_connector(
    session: AsyncSession,
    *,
    connector_id: str,
    viewer_id: str | None,
    viewer_teams: tuple[str, ...] | None,
) -> dict[str, Any] | None:
    """상세. **보이지 않는 카드는 None** 이다 — 403 과 404 를 가르지 않는다.

    구분해 주면 비공개 카드의 존재 여부가 새어 나간다. 팀 비공개로 등록한 사람은 남이 그
    카드가 있다는 사실조차 모르기를 기대한다.

    `connector_id` 는 UUID 또는 short_id 를 받는다 — deep link 가 둘 다 쓴다.
    """
    is_uuid = len(connector_id) == 36 and connector_id.count("-") == 4
    key_clause = "c.connector_id = CAST(:key AS uuid)" if is_uuid else "c.short_id = :key"
    sql = f"""
        SELECT {_SUMMARY_COLUMNS},
               c.description, c.license, c.source_repo_url, c.compatible_hosts,
               tc.fetched_at AS tools_fetched_at, tc.fetch_error AS tools_fetch_error,
               COALESCE(vt.teams, ARRAY[]::text[]) AS visibility_teams
        {_SUMMARY_JOINS}
        LEFT JOIN connector_tools_cache tc ON tc.connector_id = c.connector_id
        LEFT JOIN LATERAL (
            SELECT array_agg(v.team_code ORDER BY v.team_code) AS teams
            FROM connector_visibility_teams v WHERE v.connector_id = c.connector_id
        ) vt ON TRUE
        WHERE {key_clause} AND {VISIBLE_PREDICATE}
    """
    params = {**viewer_params(viewer_id, viewer_teams), "key": connector_id}
    row = (await session.execute(text(sql), params)).mappings().first()
    return dict(row) if row else None


async def list_tags(
    session: AsyncSession, *, viewer_id: str | None, viewer_teams: tuple[str, ...] | None
) -> list[dict[str, Any]]:
    """태그와 개수. **뷰어가 볼 수 있는 카드만 센다** — 아니면 태그 이름으로 비공개 카드의
    존재가 드러난다."""
    sql = f"""
        SELECT ct.tag, count(*) AS count
        FROM connector_tags ct
        JOIN connectors c ON c.connector_id = ct.connector_id
        WHERE c.archived_at IS NULL AND {VISIBLE_PREDICATE}
        GROUP BY ct.tag ORDER BY count DESC, ct.tag ASC
    """
    return [
        dict(r)
        for r in (await session.execute(text(sql), viewer_params(viewer_id, viewer_teams)))
        .mappings()
        .all()
    ]


async def catalog_stats(
    session: AsyncSession, *, viewer_id: str | None, viewer_teams: tuple[str, ...] | None
) -> dict[str, Any]:
    sql = f"""
        SELECT COALESCE(h.health_status, 'unknown') AS health_status, count(*) AS n
        FROM connectors c
        LEFT JOIN connector_health_checks h ON h.connector_id = c.connector_id
        WHERE c.archived_at IS NULL AND {VISIBLE_PREDICATE}
        GROUP BY 1
    """
    rows = (
        (await session.execute(text(sql), viewer_params(viewer_id, viewer_teams))).mappings().all()
    )
    by_health = {r["health_status"]: r["n"] for r in rows}
    return {"total": sum(by_health.values()), "by_health": by_health}
