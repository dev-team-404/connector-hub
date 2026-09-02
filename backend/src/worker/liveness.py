"""liveness 스윕 한 tick.

AgentToolbox `worker/connector_tasks.py` 에서 옮겨 왔다(연혁: AgentToolbox #2360). 그쪽과
다른 점은 결과가 카드 행이 아니라 `connector_health_checks` 로 간다는 것뿐이다.

**tools 캐시는 여기서 갱신하지 않는다.** 도구 목록은 거의 변하지 않는데 매 tick 전
커넥터를 refetch 하면 커넥터당 MCP 세션이 2개(liveness 핸드셰이크 + tools 세션)로 늘고
endpoint·DNS 부하가 그만큼 배가된다. 캐시 갱신은 등록 직후와 사람이 새로고침을 눌렀을 때가
담당한다.

**자동 archive 는 하지 않는다.** AgentToolbox 가 도입했다가 철회한 동작이다 — 죽은 커넥터를
목록에서 치우면 소유자에게는 "등록한 게 사라졌다" 로만 보이고 사유를 화면에서 알 수 없으며,
endpoint 가 되살아나도 후보 조회가 archive 된 행을 걸러 스스로 복귀하지 못한다. 대신
보이되 상태를 알린다. 같은 함정을 다시 파지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from core.connectors import probes
from core.mcp.client import check_liveness

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from core.settings import Settings

logger = logging.getLogger(__name__)

# 오래 안 본 것부터 본다. archived_at 가드를 남겨 두는 이유는 자동 archive 는 없앴지만
# 수동 archive 가 생기면 그 대상은 검사에서 빠져야 하기 때문이다.
_SELECT_CANDIDATES = text("""
    SELECT c.connector_id::text AS connector_id, c.endpoint_url, c.transport,
           h.last_checked_at
    FROM connectors c
    LEFT JOIN connector_health_checks h ON h.connector_id = c.connector_id
    WHERE c.archived_at IS NULL
      AND c.endpoint_url IS NOT NULL
      AND c.transport IS NOT NULL
    ORDER BY h.last_checked_at ASC NULLS FIRST, c.connector_id
    LIMIT :batch_limit
""")


async def fetch_candidates(conn: Any, *, batch_limit: int) -> list[dict[str, Any]]:
    """검사 대상 배치. 한 번도 검사하지 않은 카드가 맨 앞에 온다."""
    rows = (await conn.execute(_SELECT_CANDIDATES, {"batch_limit": batch_limit})).mappings().all()
    return [dict(r) for r in rows]


async def run_tick(engine: AsyncEngine, settings: Settings) -> dict[str, int]:
    """한 번의 스윕 → 결과 집계.

    후보 조회는 짧은 트랜잭션, 네트워크 검사는 트랜잭션 밖에서, 결과 기록은 커넥터마다
    개별 트랜잭션으로 즉시 커밋한다 — tick 이 중간에 끊겨도 앞선 결과가 남는다.

    커넥터는 제한 동시성으로 검사한다. 순차로 돌리면 `batch_limit x timeout` 이 그대로
    tick 길이가 되고(기본값으로 200 x 10s = 33분), 그것은 주기(5분)를 넘겨 tick 이 겹친다.

    집계 규약: `checked` 는 검사를 마친 건(양보 포함), `healthy`/`unhealthy` 는 **기록까지
    성공한** 건, `superseded` 는 프로브 도중 더 새 결과가 들어와 기록을 양보한 건이다.
    검사 자체가 예기치 못하게 터진 건은 어디에도 세지 않고 로그로만 남긴다 — 한 건의
    실패로 배치를 중단하지 않는다.
    """
    async with engine.connect() as conn:
        candidates = await fetch_candidates(
            conn, batch_limit=settings.connector_liveness_batch_limit
        )

    semaphore = asyncio.Semaphore(settings.connector_outbound_concurrency)

    async def _check_one(c: dict[str, Any]) -> str:
        # 네트워크와 기록을 **함께** semaphore 안에 둔다. 네트워크만 묶으면 대기하던
        # 태스크가 앞다퉈 풀려 동시 트랜잭션 수가 상한을 넘는다. 기록 자체는 짧아 슬롯을
        # 오래 잡지 않는다.
        async with semaphore:
            connector_id = c["connector_id"]
            transport = c["transport"]
            liveness = await check_liveness(
                c["endpoint_url"], transport, timeout=settings.connector_probe_timeout_sec
            )
            try:
                async with engine.begin() as conn:
                    checked_at = await probes.record_health(
                        conn,
                        connector_id=connector_id,
                        healthy=liveness.healthy,
                        last_error=liveness.failure.message if liveness.failure else None,
                        expect_last_checked_at=c["last_checked_at"],
                    )
                    if checked_at is None:
                        # 프로브 도중 수동 재검사나 다른 tick 이 더 새 결과를 남겼다 —
                        # 낡은 값으로 덮지 않고 버린다. 다음 tick 이 다시 검사한다.
                        logger.info("liveness.superseded connector_id=%s", connector_id)
                        return "superseded"
                    if liveness.transport is not None and liveness.transport != transport:
                        await probes.correct_transport(
                            conn, connector_id=connector_id, transport=liveness.transport
                        )
            except Exception:
                logger.exception("liveness.apply_failed connector_id=%s", connector_id)
                return "error"
            return "healthy" if liveness.healthy else "unhealthy"

    outcomes = await asyncio.gather(*(_check_one(c) for c in candidates), return_exceptions=True)
    for c, outcome in zip(candidates, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            logger.exception(
                "liveness.check_failed connector_id=%s", c["connector_id"], exc_info=outcome
            )

    counts = {
        "checked": sum(1 for o in outcomes if not isinstance(o, BaseException)),
        "healthy": outcomes.count("healthy"),
        "unhealthy": outcomes.count("unhealthy"),
        "superseded": outcomes.count("superseded"),
    }
    logger.info("liveness.tick %s", counts)
    return counts
