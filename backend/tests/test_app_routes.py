"""앱 배선 — 헬스체크와 인증 게이트.

단독 실행: `cd backend && uv run pytest tests/test_app_routes.py -q`
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from core.settings import load_settings


@pytest.fixture(autouse=True)
def _clear_settings():
    load_settings.cache_clear()
    yield
    load_settings.cache_clear()


def test_health_works_without_site_auth_configured() -> None:
    """설정이 없어도 떠야 게이트웨이 배선을 인증보다 먼저 확인할 수 있다."""
    with TestClient(create_app()) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_protected_route_is_503_when_site_auth_is_not_configured() -> None:
    """인증이 배선되지 않은 것과 인증 실패는 다른 상태다 — 401 로 뭉개면 원인을 못 가린다."""
    with TestClient(create_app()) as client:
        resp = client.get("/me/session")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "site_auth_unavailable"


def test_protected_route_is_401_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SITE_AUTH_BASE_URL", "http://site-auth.test")
    monkeypatch.setenv("SITE_AUTH_SERVICE_TOKEN", "t" * 32)
    monkeypatch.setenv("SITE_AUTH_ISSUER", "https://agent-factory.test")
    load_settings.cache_clear()

    with TestClient(create_app()) as client:
        resp = client.get("/me/session")
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


def test_openapi_uses_the_gateway_base_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """생성 클라이언트가 부를 주소가 게이트웨이 경로여야 한다."""
    load_settings.cache_clear()
    app = create_app()
    assert app.root_path == "/connector/api/v1"
