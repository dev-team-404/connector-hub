"""liveness 워커 진입점.

**브로커(Redis·ARQ)를 두지 않는다.** AgentToolbox 는 ARQ 로 cron 을 돌리지만, 이 서비스가
배경에서 하는 일은 "주기적으로 훑는다" 하나뿐이고 그 일정 상태는 이미 DB 에
(`connector_health_checks.last_checked_at`) 있다. 큐가 주는 것 — 작업 인큐·재시도·지연
실행 — 중 쓰는 게 없는데 Redis 를 들이면 local·dev·prod 셋 다에 운영할 구성 요소와 장애
모드가 하나씩 늘어난다.

대신 필요한 것 하나, **복제본이 여럿이어도 한 tick 은 하나만 돈다**는 보장은 PostgreSQL
advisory lock 으로 얻는다. 잠금을 못 잡은 프로세스는 그 tick 을 건너뛴다.

되돌리는 기준을 미리 적어 둔다: 사용자가 **직접 유발하는** 배경 작업(등록 시 스캔·알림
발송 같은, 재시도와 개별 진행 상태가 필요한 일)이 생기면 그때 브로커를 들인다. 주기 작업이
몇 개 더 늘어나는 것만으로는 기준이 아니다 — 그건 여기 스윕이 하나 더 붙는 일이다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import TYPE_CHECKING

from sqlalchemy import text

from core.db import dispose_engine, get_engine
from core.settings import load_settings
from worker.liveness import run_tick

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from core.settings import Settings

logger = logging.getLogger(__name__)


async def _tick_once(engine: AsyncEngine, settings: Settings) -> None:
    """advisory lock 을 잡은 프로세스만 스윕한다.

    잠금은 **세션(커넥션) 수명**을 따르므로 잡은 직후 트랜잭션을 닫는다. 안 닫으면 스윕
    내내 커넥션 하나가 `idle in transaction` 으로 남아 vacuum 을 막는다. 프로세스가 죽으면
    커넥션이 끊기면서 잠금도 함께 풀린다 — 그것이 이 방식을 쓰는 이유다(직접 만든 lease
    테이블이라면 죽은 소유자의 잠금을 누가 언제 걷어낼지 정해야 한다).
    """
    key = settings.connector_liveness_lock_key
    async with engine.connect() as lock_conn:
        acquired = (
            await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
        ).scalar_one()
        await lock_conn.commit()
        if not acquired:
            logger.info("liveness.tick_skipped_lock_held")
            return
        try:
            await run_tick(engine, settings)
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            await lock_conn.commit()


async def run_forever(stop: asyncio.Event) -> None:
    """`stop` 이 설정될 때까지 주기적으로 스윕한다.

    주기는 **tick 시작 시각 기준**이다. 끝난 뒤부터 재는 방식이면 느린 tick 이 다음 주기를
    계속 밀어 실제 간격이 설정값보다 커진다. tick 이 주기보다 오래 걸리면 곧바로 다음
    tick 이 시작되는데, 그건 잠금이 있으니 안전하고 배치 상한이 있으니 폭주하지 않는다.
    """
    settings = load_settings()
    engine = get_engine()
    interval = settings.connector_liveness_interval_sec
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        started = loop.time()
        try:
            await _tick_once(engine, settings)
        except Exception:
            # 한 tick 의 실패로 워커를 죽이지 않는다 — DB 가 잠깐 흔들린 경우가 대부분이고
            # 다음 주기에 저절로 회복된다.
            logger.exception("liveness.tick_failed")
        remaining = interval - (loop.time() - started)
        if remaining > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=remaining)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # 진행 중인 tick 을 끊지 않고 끝나기를 기다린다 — 프로브 도중에 잘리면 그 커넥터의
        # 상태가 갱신되지 않은 채 남고, 배포 때마다 그런 카드가 생긴다.
        loop.add_signal_handler(sig, stop.set)
    try:
        await run_forever(stop)
    finally:
        await dispose_engine()
    logger.info("liveness.worker_stopped")


def run() -> None:
    asyncio.run(main())
