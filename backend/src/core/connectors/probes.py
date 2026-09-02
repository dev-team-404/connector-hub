"""tools 캐시 · health 상태의 읽기/쓰기.

**카드 본문(`connectors`)과 다른 테이블에 산다**(0001 의 분리 근거). 5분 주기 cron 이 카드
행을 계속 UPDATE 하면 사람이 쓰는 값과 워커가 쓰는 값이 같은 행에서 잠금과 dead tuple 을
서로 물려 준다.

**낡은 값을 지우지 않는다.** fetch 가 실패해도 마지막 정상 tools 는 남기고 오류만 덧쓴다.
그래야 워커나 endpoint 가 죽어도 화면이 빈손이 되지 않고 "언제 본 값인지" 만 알리면 된다
(설계 §14, connector-hub#3 "마지막 정상값에 stale 표시").
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

# 두 진입점이 같은 SQL 을 쓴다 — API 는 `AsyncSession`, 워커는 `AsyncConnection`. 둘 다
# `.execute(text, params)` 만 필요해서 `Any` 로 받는다. 공통 상위 타입이 없어 Protocol 을
# 세우는 것 말고는 방법이 없는데, 메서드 하나짜리 Protocol 은 이득이 없다.


class _Unset:
    """CAS 를 걸지 않는다는 표식. `None` 은 "아직 검사한 적 없음" 이라는 실제 값이라 못 쓴다."""


ANY_STATE = _Unset()


def is_stale(checked_at: datetime | None, max_age_sec: float) -> bool:
    """마지막 확인이 너무 오래됐는가. 한 번도 확인한 적 없으면 stale 이다."""
    if checked_at is None:
        return True
    reference = checked_at if checked_at.tzinfo else checked_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - reference > timedelta(seconds=max_age_sec)


# ---- tools 캐시 --------------------------------------------------------------------------

_READ_TOOLS = text("""
    SELECT tools, fetched_at, fetch_error
    FROM connector_tools_cache
    WHERE connector_id = CAST(:c AS uuid)
""")

# tools 가 NULL(=이번 fetch 실패)이면 이전 값과 이전 시각을 그대로 둔다. 그래야 화면이
# "마지막으로 본 도구 + 그때 시각 + 이번 오류" 를 함께 보여 줄 수 있다.
_SAVE_TOOLS = text("""
    INSERT INTO connector_tools_cache (connector_id, tools, fetched_at, fetch_error)
    VALUES (CAST(:c AS uuid), CAST(:tools AS jsonb), CASE WHEN :has_tools THEN now() END, :error)
    ON CONFLICT (connector_id) DO UPDATE SET
        tools = COALESCE(EXCLUDED.tools, connector_tools_cache.tools),
        fetched_at = CASE
            WHEN :has_tools THEN EXCLUDED.fetched_at
            ELSE connector_tools_cache.fetched_at
        END,
        fetch_error = EXCLUDED.fetch_error
""")


async def read_tools_cache(
    db: Any, *, connector_id: str
) -> tuple[list[dict[str, Any]] | None, datetime | None, str | None]:
    """캐시된 tools → (tools, 마지막 성공 시각, 마지막 오류)."""
    row = (await db.execute(_READ_TOOLS, {"c": connector_id})).mappings().first()
    if row is None:
        return None, None, None
    tools = row["tools"]
    # asyncpg 는 jsonb 를 문자열로 줄 수도, 파싱해 줄 수도 있다(드라이버 설정에 따라).
    if isinstance(tools, str):
        tools = json.loads(tools)
    return tools, row["fetched_at"], row["fetch_error"]


async def save_tools_cache(
    db: Any, *, connector_id: str, tools: list[dict[str, Any]] | None, error: str | None
) -> None:
    """fetch 결과를 캐시에 반영한다. `tools=None` 이면 오류만 갱신하고 이전 값을 남긴다."""
    await db.execute(
        _SAVE_TOOLS,
        {
            "c": connector_id,
            "tools": json.dumps(tools) if tools is not None else None,
            "has_tools": tools is not None,
            "error": error,
        },
    )


# ---- health ------------------------------------------------------------------------------


async def record_health(
    db: Any,
    *,
    connector_id: str,
    healthy: bool,
    last_error: str | None,
    expect_last_checked_at: datetime | _Unset | None = ANY_STATE,
) -> datetime | None:
    """liveness 결과를 기록한다 → 기록된 시각(양보했으면 None).

    **연속 실패를 SQL 안에서 원자적으로 올린다.** 읽어 둔 값에 +1 해서 쓰면 그 사이 다른
    경로가 같은 행을 갱신했을 때 그 결과를 낡은 값으로 덮는다.

    `expect_last_checked_at` 을 주면 CAS 가 걸린다 — 그 값에서 바뀌지 않았을 때만 쓴다.
    워커가 쓰는 경로다. 막으려는 상황은 이것이다: 죽어 있던 커넥터의 프로브는 실패까지
    몇 초씩 걸리는데, 그동안 사용자가 서버를 고치고 "즉시 재검사" 를 눌러 healthy 가
    기록될 수 있다. 가드가 없으면 뒤늦게 끝난 워커 프로브가 그 결과를 unhealthy 로
    되돌려, 방금 초록으로 바뀐 배지가 다음 tick 까지 다시 빨갛게 남는다.

    **수동 경로에는 CAS 를 걸지 않는다**(기본값). 사용자는 자기 클릭의 결과를 지금 보고
    있으므로 그 프로브 결과를 보여 주는 편이 옳다. 배경 작업이 양보한다.

    **연속 실패로 자동 archive 하지 않는다.** 죽은 커넥터를 목록에서 치우면 소유자에게는
    "등록한 게 사라졌다" 로만 보이고 사유를 화면에서 알 수 없으며, endpoint 가 되살아나도
    후보 조회가 archive 된 행을 걸러 스스로 복귀하지 못한다. 대신 보이되 상태를 알린다.
    """
    cas = not isinstance(expect_last_checked_at, _Unset)
    # IS NOT DISTINCT FROM 이라야 미검사(NULL) 행도 CAS 대상이 된다 — `=` 는 NULL 에서
    # 항상 UNKNOWN 이라 최초 검사가 전부 막힌다.
    guard = (
        " WHERE connector_health_checks.last_checked_at IS NOT DISTINCT FROM :seen" if cas else ""
    )
    sql = text(f"""
        INSERT INTO connector_health_checks
            (connector_id, health_status, last_checked_at, consecutive_failures, last_error)
        VALUES (CAST(:c AS uuid), :status, now(), CASE WHEN :healthy THEN 0 ELSE 1 END, :error)
        ON CONFLICT (connector_id) DO UPDATE SET
            health_status = EXCLUDED.health_status,
            last_checked_at = EXCLUDED.last_checked_at,
            consecutive_failures = CASE
                WHEN :healthy THEN 0
                ELSE connector_health_checks.consecutive_failures + 1
            END,
            last_error = EXCLUDED.last_error
        {guard}
        RETURNING last_checked_at
    """)
    params: dict[str, Any] = {
        "c": connector_id,
        "status": "healthy" if healthy else "unhealthy",
        "healthy": healthy,
        "error": last_error,
    }
    if cas:
        params["seen"] = expect_last_checked_at
    row = (await db.execute(sql, params)).first()
    return row[0] if row else None


async def correct_transport(db: Any, *, connector_id: str, transport: str) -> None:
    """폴백이 등록값과 다른 transport 로 성공했을 때 그 값을 되기록한다.

    안 쓰면 매 검사가 같은 폴백을 다시 거쳐 커넥터당 왕복이 2배가 된다. `updated_at` 은
    건드리지 않는다 — 사람의 수정이 아니라 관측 결과의 반영이라 "최근 수정" 정렬을 흔들면
    안 된다.
    """
    await db.execute(
        text("UPDATE connectors SET transport = :t WHERE connector_id = CAST(:c AS uuid)"),
        {"t": transport, "c": connector_id},
    )
