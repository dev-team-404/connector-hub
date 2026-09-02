"""liveness 워커 (connector-hub#3).

프로브는 가짜다. 여기서 보는 것은 endpoint 와 대화하는 방법이 아니라 **결과를 어떻게
기록하는가** — 전이, 낡은 결과의 양보, 동시성 상한, 그리고 죽은 커넥터를 목록에서 치우지
않는다는 규약이다.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import psycopg
import pytest

from core.mcp.client import Liveness, ProbeFailure
from core.settings import load_settings
from tests.conftest_db import REQUIRES_DB, _sync_url, truncate_connectors, worker_engine
from worker import liveness as mod
from worker.main import _tick_once

pytestmark = REQUIRES_DB


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    os.environ["DATABASE_URL"] = os.environ["CONNECTOR_TEST_DATABASE_URL"]
    load_settings.cache_clear()
    truncate_connectors()
    yield
    truncate_connectors()


def _settings(**env: str) -> Any:
    for key, value in env.items():
        os.environ[key] = value
    load_settings.cache_clear()
    return load_settings()


def _insert(
    name: str,
    *,
    endpoint: str | None = "https://mcp.test/s",
    transport: str | None = "streamable_http",
) -> str:
    with psycopg.connect(_sync_url(), autocommit=True) as conn:
        row = conn.execute(
            """
            INSERT INTO connectors (name, short_description, category, source_repo_url,
                                    endpoint_url, transport, scope_type, scope_id,
                                    creator_user_id, creator_team_id)
            VALUES (%s, '요약', 'productivity', 'https://repo.test/x', %s, %s,
                    'team', 'team-a', 'u_worker', 'team-a')
            RETURNING connector_id::text
            """,
            (name, endpoint, transport),
        ).fetchone()
    assert row is not None
    return row[0]


def _health(connector_id: str) -> dict[str, Any] | None:
    with psycopg.connect(_sync_url(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT health_status, consecutive_failures, last_error, last_checked_at "
            "FROM connector_health_checks WHERE connector_id = %s",
            (connector_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "health_status": row[0],
        "consecutive_failures": row[1],
        "last_error": row[2],
        "last_checked_at": row[3],
    }


def _transport(connector_id: str) -> str | None:
    with psycopg.connect(_sync_url(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT transport FROM connectors WHERE connector_id = %s", (connector_id,)
        ).fetchone()
    return row[0] if row else None


def _probe(healthy: bool, transport: str | None = "streamable_http", *, delay: float = 0.0):
    async def _fake(url: str, tr: str, *, timeout: float = 0) -> Liveness:
        if delay:
            await asyncio.sleep(delay)
        if healthy:
            return Liveness(healthy=True, transport=transport)
        return Liveness(healthy=False, transport=None, failure=ProbeFailure.of("unreachable"))

    return _fake


# ---- 후보 선정 -------------------------------------------------------------------------


async def test_never_checked_connectors_come_first(monkeypatch) -> None:
    """한 번도 안 본 카드를 뒤로 미루면 새 커넥터의 배지가 언제까지고 unknown 으로 남는다."""
    settings = _settings(CONNECTOR_LIVENESS_BATCH_LIMIT="10")
    seen = _insert("seen")
    monkeypatch.setattr(mod, "check_liveness", _probe(True))
    async with worker_engine() as engine:
        await mod.run_tick(engine, settings)
        fresh = _insert("fresh")
        async with engine.connect() as conn:
            ids = [c["connector_id"] for c in await mod.fetch_candidates(conn, batch_limit=10)]
    assert ids[0] == fresh
    assert ids[1] == seen


async def test_connectors_without_an_endpoint_are_not_probed(monkeypatch) -> None:
    settings = _settings()
    _insert("no-endpoint", endpoint=None, transport=None)
    calls: list[str] = []

    async def _record(url: str, tr: str, *, timeout: float = 0) -> Liveness:
        calls.append(url)
        return Liveness(healthy=True, transport=tr)

    monkeypatch.setattr(mod, "check_liveness", _record)
    async with worker_engine() as engine:
        counts = await mod.run_tick(engine, settings)
    assert calls == []
    assert counts["checked"] == 0


# ---- 상태 전이 -------------------------------------------------------------------------


async def test_failure_then_recovery(monkeypatch) -> None:
    """되살아난 endpoint 가 다음 tick 에 healthy 로 복귀한다.

    자동 archive 를 넣었다면 이 복귀가 일어나지 않는다 — 후보 조회가 archive 된 행을
    거르기 때문이다. 그 함정을 여기서 고정한다.
    """
    settings = _settings()
    connector_id = _insert("flaky")

    async with worker_engine() as engine:
        monkeypatch.setattr(mod, "check_liveness", _probe(False))
        for _ in range(3):
            await mod.run_tick(engine, settings)
        failed = _health(connector_id)
        assert failed is not None
        assert failed["health_status"] == "unhealthy"
        assert failed["consecutive_failures"] == 3
        assert failed["last_error"] is not None

        monkeypatch.setattr(mod, "check_liveness", _probe(True))
        counts = await mod.run_tick(engine, settings)

    recovered = _health(connector_id)
    assert recovered is not None
    assert recovered["health_status"] == "healthy"
    assert recovered["consecutive_failures"] == 0  # 0 으로 되돌린다
    assert counts == {"checked": 1, "healthy": 1, "unhealthy": 0, "superseded": 0}


async def test_failure_reason_stored_is_the_safe_one(monkeypatch) -> None:
    """DB 에 남는 사유도 뭉갠 쪽이어야 한다 — 언젠가 API 로 나가더라도 안전하도록."""
    settings = _settings()
    connector_id = _insert("dead")
    monkeypatch.setattr(mod, "check_liveness", _probe(False))
    async with worker_engine() as engine:
        await mod.run_tick(engine, settings)
    stored = _health(connector_id)
    assert stored is not None
    assert stored["last_error"] == ProbeFailure.of("unreachable").message


async def test_transport_correction_is_written_back(monkeypatch) -> None:
    """폴백 결과를 안 쓰면 매 tick 이 같은 폴백을 다시 거쳐 왕복이 2배가 된다."""
    settings = _settings()
    connector_id = _insert("legacy-sse")
    monkeypatch.setattr(mod, "check_liveness", _probe(True, transport="sse"))
    async with worker_engine() as engine:
        await mod.run_tick(engine, settings)
    assert _transport(connector_id) == "sse"


# ---- 낡은 결과 양보 ---------------------------------------------------------------------


async def test_a_slow_probe_does_not_overwrite_a_newer_result(monkeypatch) -> None:
    """죽어 있던 커넥터의 프로브는 실패까지 오래 걸린다.

    그동안 사용자가 서버를 고치고 "즉시 재검사" 를 눌러 healthy 가 기록될 수 있다. 가드가
    없으면 뒤늦게 끝난 워커 프로브가 그것을 unhealthy 로 되돌려, 방금 초록으로 바뀐 배지가
    다음 tick 까지 다시 빨갛게 남는다. 사람이 재검사를 누르는 상황이 정확히 그 상황이다.
    """
    settings = _settings()
    connector_id = _insert("raced")

    async def _slow_failure(url: str, tr: str, *, timeout: float = 0) -> Liveness:
        # 프로브가 도는 사이에 수동 재검사가 healthy 를 기록한 상황을 재현한다.
        with psycopg.connect(_sync_url(), autocommit=True) as conn:
            conn.execute(
                "INSERT INTO connector_health_checks "
                "(connector_id, health_status, last_checked_at, consecutive_failures) "
                "VALUES (%s, 'healthy', now(), 0) "
                "ON CONFLICT (connector_id) DO UPDATE "
                "SET health_status='healthy', last_checked_at=now(), consecutive_failures=0",
                (connector_id,),
            )
        return Liveness(healthy=False, transport=None, failure=ProbeFailure.of("unreachable"))

    monkeypatch.setattr(mod, "check_liveness", _slow_failure)
    async with worker_engine() as engine:
        counts = await mod.run_tick(engine, settings)

    assert counts["superseded"] == 1
    assert counts["unhealthy"] == 0
    current = _health(connector_id)
    assert current is not None
    assert current["health_status"] == "healthy"  # 사용자가 방금 본 결과가 살아남았다


# ---- 동시성 · 단일 실행 -----------------------------------------------------------------


async def test_probes_respect_the_concurrency_cap(monkeypatch) -> None:
    """소켓·DNS 스레드풀·DB 풀이 모두 유한하다 — 배치 크기만큼 한꺼번에 열면 안 된다."""
    settings = _settings(CONNECTOR_OUTBOUND_CONCURRENCY="2")
    for i in range(6):
        _insert(f"c{i}")

    in_flight = 0
    peak = 0

    async def _slow(url: str, tr: str, *, timeout: float = 0) -> Liveness:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.02)
            return Liveness(healthy=True, transport=tr)
        finally:
            in_flight -= 1

    monkeypatch.setattr(mod, "check_liveness", _slow)
    async with worker_engine() as engine:
        counts = await mod.run_tick(engine, settings)

    assert peak <= 2
    assert counts["healthy"] == 6


async def test_batch_limit_defers_the_rest_to_the_next_tick(monkeypatch) -> None:
    settings = _settings(CONNECTOR_LIVENESS_BATCH_LIMIT="2")
    for i in range(5):
        _insert(f"b{i}")
    monkeypatch.setattr(mod, "check_liveness", _probe(True))
    async with worker_engine() as engine:
        first = await mod.run_tick(engine, settings)
        second = await mod.run_tick(engine, settings)
    assert first["checked"] == 2
    assert second["checked"] == 2  # 남은 것부터 — 오래 안 본 순서라 앞의 둘은 다시 안 온다


async def test_only_one_worker_sweeps_at_a_time(monkeypatch) -> None:
    """브로커 대신 advisory lock 으로 단일 실행을 보장한다(`worker/main.py`).

    이 보장이 없으면 복제본 수만큼 같은 endpoint 를 동시에 두드린다.
    """
    settings = _settings()
    _insert("locked")
    probes: list[str] = []

    async def _record(url: str, tr: str, *, timeout: float = 0) -> Liveness:
        probes.append(url)
        await asyncio.sleep(0.05)
        return Liveness(healthy=True, transport=tr)

    monkeypatch.setattr(mod, "check_liveness", _record)
    async with worker_engine() as engine:
        await asyncio.gather(_tick_once(engine, settings), _tick_once(engine, settings))

    assert len(probes) == 1


async def test_the_lock_is_released_for_the_next_tick(monkeypatch) -> None:
    """잠금을 놓지 않으면 워커가 한 번 돌고 영영 멈춘다."""
    settings = _settings()
    _insert("sequential")
    monkeypatch.setattr(mod, "check_liveness", _probe(True))
    async with worker_engine() as engine:
        await _tick_once(engine, settings)
        await _tick_once(engine, settings)
        counts = await mod.run_tick(engine, settings)
    assert counts["checked"] == 1
