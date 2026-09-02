"""Connector DB 접속.

**AgentToolbox 데이터베이스와 다른 DB 다.** 같은 PostgreSQL 인스턴스를 써도 되지만 같은
데이터베이스를 가리키면 안 된다 — 스키마 소유권이 갈리는 것이 분리의 핵심이고, 한쪽
마이그레이션이 다른 쪽 테이블을 보게 되는 순간 배포가 다시 묶인다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.settings import load_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def _normalize(url: str) -> str:
    """어떤 형태로 와도 asyncpg 드라이버로 맞춘다.

    운영·CI·로컬이 각각 다른 표기를 쓰는 것이 흔해서(`postgresql://` · `postgres://`),
    호출부마다 형식을 따지게 두면 "왜 sync 드라이버로 뜨지" 를 반복해서 디버깅한다.
    """
    for prefix in (
        "postgresql+asyncpg://",
        "postgresql+psycopg://",
        "postgresql://",
        "postgres://",
    ):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix) :]
    return url


def get_engine() -> AsyncEngine:
    global _engine, _factory
    if _engine is None:
        url = load_settings().database_url
        if not url:
            raise RuntimeError("DATABASE_URL 미설정 — Connector DB 에 접속할 수 없다")
        _engine = create_async_engine(_normalize(url), pool_pre_ping=True)
        _factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 의존성. 요청 하나에 세션 하나."""
    get_engine()
    assert _factory is not None
    async with _factory() as session:
        yield session


async def dispose_engine() -> None:
    """테스트가 DB 를 바꿀 때와 종료 시 호출한다."""
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _factory = None
