"""ConnectorHub 설정 — 환경 변수 → typed Settings.

AgentToolbox 와 **공유하지 않는다.** 같은 이름의 값이라도 각자 읽는다 — 설정을 공유하기
시작하면 배포가 다시 묶인다.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- 사이트 인증 (AgentToolbox Site Auth 계약의 소비자) -------------------------------
    # 계약 정본: AgentToolbox docs/architecture/site-auth-contract.md
    site_auth_base_url: str = Field(
        default="",
        alias="SITE_AUTH_BASE_URL",
        description="Site Auth origin (예: http://agent-toolbox-api:8000) — 미설정이면 인증 불가",
    )
    site_auth_service_token: str = Field(
        default="",
        alias="SITE_AUTH_SERVICE_TOKEN",
        description="introspection·Directory 호출용 서비스 키 (secret) — X-API-Key 헤더",
    )
    site_auth_issuer: str = Field(
        default="",
        alias="SITE_AUTH_ISSUER",
        description="세션 JWT 의 기대 iss. 비면 iss 검증을 건너뛰지 않고 부팅에서 막는다",
    )
    site_auth_audience: str = Field(
        default="agent-factory-site",
        alias="SITE_AUTH_AUDIENCE",
        description="세션 JWT 의 기대 aud",
    )
    # 계약 §4.1 — 짧게 잡으면 이쪽이 병목이 되고, 길면 로그아웃 반영이 늦다.
    site_auth_introspect_cache_sec: int = Field(
        default=45,
        alias="SITE_AUTH_INTROSPECT_CACHE_SEC",
        ge=1,
        le=300,
        description="introspection 응답 캐시(초) — 계약 권고 30~60",
    )
    site_auth_jwks_cache_sec: int = Field(
        default=600,
        alias="SITE_AUTH_JWKS_CACHE_SEC",
        ge=1,
        description="JWKS 캐시(초). 모르는 kid 를 만나면 이와 별개로 1회 재조회한다",
    )
    site_auth_timeout_sec: float = Field(
        default=2.5,
        alias="SITE_AUTH_TIMEOUT_SEC",
        gt=0,
        description="Site Auth 호출 타임아웃(초)",
    )

    # --- 게이트웨이 -----------------------------------------------------------------------
    # 외부 base path 는 계약상 고정이다(설계 §11). 값을 바꾸면 게이트웨이·Web base 와 어긋난다.
    api_base_path: str = Field(default="/connector/api/v1", alias="CONNECTOR_API_BASE_PATH")


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    """프로세스 1회 평가. 테스트는 `load_settings.cache_clear()` 로 비운다."""
    return Settings()
