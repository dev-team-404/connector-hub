"""ConnectorHub API 진입점.

라우트는 아직 헬스체크와 세션 확인뿐이다. Connector 도메인은 P3-2 에서 들어온다.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from api.deps import CurrentSessionDep  # noqa: TC001
from core.settings import load_settings
from core.site_auth import SiteAuthClient, build_http_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    app.state.settings = settings
    app.state.site_auth = None
    if settings.site_auth_base_url and settings.site_auth_service_token:
        app.state.site_auth = SiteAuthClient(
            base_url=settings.site_auth_base_url.rstrip("/"),
            service_token=settings.site_auth_service_token,
            issuer=settings.site_auth_issuer,
            audience=settings.site_auth_audience,
            http=build_http_client(settings.site_auth_timeout_sec),
            introspect_cache_sec=settings.site_auth_introspect_cache_sec,
            jwks_cache_sec=settings.site_auth_jwks_cache_sec,
        )
    else:
        # 죽이지 않는다 — 헬스체크와 정적 경로는 인증 없이도 떠야 게이트웨이 배선을 먼저
        # 확인할 수 있다. 인증이 필요한 라우트만 503 을 낸다.
        logger.warning("site_auth.not_configured")

    yield

    client = app.state.site_auth
    if client is not None:
        await client.http.aclose()


def create_app() -> FastAPI:
    settings = load_settings()
    app = FastAPI(
        title="ConnectorHub API",
        version="0.0.0",
        lifespan=lifespan,
        # 게이트웨이가 붙이는 외부 경로. OpenAPI 의 server URL 이 이 값이어야 생성
        # 클라이언트가 올바른 주소를 부른다.
        root_path=settings.api_base_path,
    )

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        """게이트웨이·배포 스모크용. 의존성을 건드리지 않는다."""
        return {"status": "ok"}

    @app.get("/me/session", summary="현재 세션 (계약 소비 확인용)")
    async def me_session(session: CurrentSessionDep) -> dict[str, object]:
        """사이트 인증 계약이 실제로 동작하는지 확인하는 최소 라우트.

        Connector 도메인이 들어오기 전까지 이 라우트가 계약의 유일한 소비자다. 게이트웨이를
        붙인 뒤 "로그인 한 번으로 두 앱" 이 성립하는지 여기서 먼저 확인한다.
        """
        return {
            "sub": session.sub,
            "role": session.role,
            "team_codes": list(session.team_codes),
            "expires_at": session.expires_at,
        }

    return app


app = create_app()


def run() -> None:
    import uvicorn

    # 컨테이너 안에서 뜨므로 모든 인터페이스에 바인딩한다. 노출 범위는 게이트웨이가 정한다.
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000)
