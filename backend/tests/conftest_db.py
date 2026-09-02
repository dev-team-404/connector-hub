"""DB 를 쓰는 API 테스트의 공통 픽스처.

가짜 Site Auth 를 붙여 세션을 만들어 준다 — 실제 AgentToolbox 없이 공개범위 판정을
확인해야 하기 때문이다. 검증 자체는 `test_site_auth_*` 가 계약 벡터로 본다.

**클라이언트마다 새 엔진을 만들고 `NullPool` 을 쓴다.** `TestClient` 는 블록마다 자기
이벤트 루프를 돌리는데, 전역 엔진을 공유하면 앞 블록에서 만든 asyncpg 커넥션이 다음
루프에서 재사용되어 "attached to a different loop" 로 터진다. 풀을 두지 않으면 커넥션이
요청 안에서만 살아 그 문제가 생기지 않는다.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

if TYPE_CHECKING:
    from collections.abc import Iterator

REQUIRES_DB = pytest.mark.skipif(
    not os.getenv("CONNECTOR_TEST_DATABASE_URL"),
    reason="CONNECTOR_TEST_DATABASE_URL not set",
)


def _raw_url() -> str:
    return os.environ["CONNECTOR_TEST_DATABASE_URL"]


def _async_url() -> str:
    raw = _raw_url()
    for prefix in (
        "postgresql+asyncpg://",
        "postgresql+psycopg://",
        "postgresql://",
        "postgres://",
    ):
        if raw.startswith(prefix):
            return "postgresql+asyncpg://" + raw[len(prefix) :]
    return raw


def _sync_url() -> str:
    raw = _raw_url()
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://"):
        if raw.startswith(prefix):
            return "postgresql://" + raw[len(prefix) :]
    return raw


def truncate_connectors() -> None:
    """테스트 사이 정리. 동기 드라이버로 한다 — 픽스처가 이벤트 루프를 갖지 않아야
    클라이언트 블록의 루프와 얽히지 않는다."""
    with psycopg.connect(_sync_url(), autocommit=True) as conn:
        conn.execute("DELETE FROM connectors WHERE creator_user_id LIKE 'u_%%'")


@dataclass(frozen=True)
class Viewer:
    sub: str
    team_codes: tuple[str, ...] = ()
    role: str = "user"


@contextmanager
def make_client(viewer: Viewer | None) -> Iterator[TestClient]:
    """뷰어를 고정한 클라이언트. None 이면 익명."""
    os.environ["DATABASE_URL"] = _raw_url()

    from fastapi import HTTPException

    from api.deps import get_current_session, get_optional_session
    from api.main import create_app
    from core.db import get_session
    from core.settings import load_settings
    from core.site_auth import SiteSession

    load_settings.cache_clear()
    engine = create_async_engine(_async_url(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    def _session() -> SiteSession | None:
        if viewer is None:
            return None
        return SiteSession(
            sub=viewer.sub, sid=f"s_{viewer.sub}", role=viewer.role, team_codes=viewer.team_codes
        )

    async def _optional() -> SiteSession | None:
        return _session()

    async def _required() -> SiteSession:
        # 둘 다 대체한다. `get_current_session` 은 토큰보다 **설정 확인을 먼저** 하는데
        # (배선 누락이 401 로 가려지지 않게), 여기서는 Site Auth 를 띄우지 않으므로 그
        # 검사에 걸린다. 그 순서 자체는 `test_app_routes.py` 가 본다.
        session = _session()
        if session is None:
            raise HTTPException(status_code=401, detail={"code": "unauthenticated"})
        return session

    async def _db():
        async with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_optional_session] = _optional
    app.dependency_overrides[get_current_session] = _required
    app.dependency_overrides[get_session] = _db

    with TestClient(app) as client:
        yield client
