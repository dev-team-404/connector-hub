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

    # --- 데이터베이스 ---------------------------------------------------------------------
    # **AgentToolbox 와 다른 데이터베이스다.** 같은 인스턴스를 써도 상관없지만 같은 DB 를
    # 가리키면 안 된다 — 스키마 소유권이 갈리는 것이 분리의 핵심이다.
    database_url: str = Field(
        default="",
        alias="DATABASE_URL",
        description="postgresql+asyncpg://... — 미설정이면 DB 를 쓰는 라우트가 503",
    )

    # --- 커넥터 endpoint 아웃바운드 (connector-hub#3) ---------------------------------------
    # 사용자가 넣은 주소로 서버가 직접 나가는 경로다. 상한과 타임아웃이 여기 전부 모여 있다.
    connector_allowed_hosts: str = Field(
        default="",
        alias="CONNECTOR_ALLOWED_HOSTS",
        description=(
            "커넥터 endpoint 로 허용할 호스트(쉼표 구분). `.example.com` 은 하위 도메인 전체를 "
            "뜻한다. **비우면 호스트 제한 없음** — 기본값으로 잠그면 이관해 온 커넥터가 일제히 "
            "죽으므로 opt-in 이다. 비워 둬도 link-local(metadata)·multicast·reserved 차단은 항상 걸린다"
        ),
    )
    connector_probe_timeout_sec: float = Field(
        default=10.0,
        alias="CONNECTOR_PROBE_TIMEOUT_SEC",
        gt=0,
        description="tools 나열·liveness 핸드셰이크 개별 타임아웃(초)",
    )
    connector_register_fetch_timeout_sec: float = Field(
        default=4.0,
        alias="CONNECTOR_REGISTER_FETCH_TIMEOUT_SEC",
        gt=0,
        description=(
            "등록 직후 tools 자동 fetch 타임아웃(초). 정상 endpoint 는 핸드셰이크+list_tools 가 "
            "1s 안쪽이라 짧게 잡아 느린 endpoint 가 등록 응답을 붙잡지 않게 한다"
        ),
    )
    connector_outbound_concurrency: int = Field(
        default=8,
        alias="CONNECTOR_OUTBOUND_CONCURRENCY",
        ge=1,
        description=(
            "프로세스가 동시에 여는 MCP 세션 상한. 세션마다 소켓과 SSRF 가드의 DNS 스레드풀을 "
            "잡으므로 API(미리보기·새로고침)와 워커에 **같은 상한**을 건다"
        ),
    )
    connector_probe_rate_limit_per_min: int = Field(
        default=20,
        alias="CONNECTOR_PROBE_RATE_LIMIT_PER_MIN",
        description=(
            "사용자별 endpoint 프로브(미리보기·수동 재검사) 분당 상한. 0 이하면 비활성. "
            "프로세스 안에서만 세므로 복제본 수만큼 실효 상한이 커진다(`api/rate_limit.py`)"
        ),
    )
    connector_tools_stale_after_sec: int = Field(
        default=86400,
        alias="CONNECTOR_TOOLS_STALE_AFTER_SEC",
        ge=1,
        description=(
            "이 시간이 지난 tools 캐시는 stale 로 표시한다. 지우지는 않는다 — 워커·endpoint 가 "
            "죽어도 마지막 정상값은 보여 주고 낡았다는 것만 알린다(설계 §14)"
        ),
    )
    connector_health_stale_after_sec: int = Field(
        default=1800,
        alias="CONNECTOR_HEALTH_STALE_AFTER_SEC",
        ge=1,
        description=(
            "이 시간 동안 검사가 없으면 health 배지를 stale 로 표시한다. cron 주기(기본 5분)의 "
            "몇 배로 둔다 — 한두 tick 을 건너뛴 것과 워커가 죽은 것을 가르는 값이다"
        ),
    )

    # --- liveness 워커 (connector-hub#3) ------------------------------------------------------
    connector_liveness_interval_sec: int = Field(
        default=300,
        alias="CONNECTOR_LIVENESS_INTERVAL_SEC",
        ge=10,
        description="liveness 스윕 주기(초)",
    )
    connector_liveness_batch_limit: int = Field(
        default=200,
        alias="CONNECTOR_LIVENESS_BATCH_LIMIT",
        ge=1,
        description="한 tick 이 검사하는 최대 커넥터 수. 오래 안 본 것부터 고르므로 나머지는 다음 tick 이 본다",
    )
    connector_liveness_lock_key: int = Field(
        default=871_003_001,
        alias="CONNECTOR_LIVENESS_LOCK_KEY",
        description=(
            "PostgreSQL advisory lock 키. 워커를 여러 개 띄워도 한 tick 은 하나만 돈다 — "
            "브로커 없이 단일 실행을 보장하는 유일한 장치라 값이 겹치면 안 된다"
        ),
    )

    # --- 게이트웨이 -----------------------------------------------------------------------
    # 외부 base path 는 계약상 고정이다(설계 §11). 값을 바꾸면 게이트웨이·Web base 와 어긋난다.
    api_base_path: str = Field(default="/connector/api/v1", alias="CONNECTOR_API_BASE_PATH")


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    """프로세스 1회 평가. 테스트는 `load_settings.cache_clear()` 로 비운다."""
    return Settings()
