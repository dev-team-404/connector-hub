"""Connector API 스키마.

읽기와 쓰기 모델을 나눈다. 하나로 합치면 `created_at` 처럼 서버가 정하는 값이 요청 본문에
나타나고, 클라이언트가 그것을 보내면 무시되는지 반영되는지가 계약에서 사라진다.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic v2 는 런타임에 타입을 해석한다
from typing import Annotated, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

Transport = Literal["http", "sse"]
ScopeType = Literal["global", "team"]
SortKey = Literal["recent", "name"]

#: 태그는 소문자·하이픈으로 정규화한다. 대소문자만 다른 태그가 갈라지면 필터가 반쪽이 된다.
TAG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"


class ConnectorWrite(BaseModel):
    """등록·수정 공통 본문."""

    name: Annotated[str, Field(min_length=1, max_length=200)]
    title: Annotated[str | None, Field(max_length=200)] = None
    short_description: Annotated[str, Field(min_length=1, max_length=500)]
    description: Annotated[str | None, Field(max_length=20000)] = None
    category: Annotated[str, Field(min_length=1, max_length=100)]
    license: Annotated[str | None, Field(max_length=100)] = None

    # 보안 검토용. 설계가 필수로 승격했다 — endpoint 만 보고는 무엇을 하는 서버인지 알 수 없다.
    source_repo_url: HttpUrl
    endpoint_url: HttpUrl | None = None
    transport: Transport | None = None

    scope_type: ScopeType = "team"
    scope_id: str | None = None
    visibility_teams: Annotated[list[str], Field(max_length=50)] = []
    tags: Annotated[list[str], Field(max_length=20)] = []

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, raw: list[str]) -> list[str]:
        import re

        seen: list[str] = []
        for tag in raw:
            value = tag.strip().lower()
            if not re.match(TAG_PATTERN, value):
                raise ValueError(f"태그 형식이 아니다: {tag!r} (소문자·숫자·하이픈, 40자 이내)")
            if value not in seen:
                seen.append(value)
        return seen

    @field_validator("scope_id")
    @classmethod
    def _strip_scope_id(cls, value: str | None) -> str | None:
        return value.strip() if value else None

    def check_consistency(self) -> None:
        """DB CHECK 와 같은 규칙을 요청 단계에서도 본다.

        DB 가 막아 주긴 하지만 그때는 500 에 가까운 오류가 되고, 클라이언트는 무엇이 틀렸는지
        알 수 없다. 같은 규칙을 두 곳에 두는 비용보다 그 편이 낫다.
        """
        if self.scope_type == "global" and self.scope_id:
            raise ValueError("scope_type=global 이면 scope_id 를 둘 수 없다")
        if self.scope_type == "team" and not self.scope_id:
            raise ValueError("scope_type=team 이면 scope_id 가 필요하다")
        if self.transport and not self.endpoint_url:
            raise ValueError("transport 를 정했으면 endpoint_url 이 필요하다")


class ConnectorSummary(BaseModel):
    """목록 카드. 상세에만 있는 큰 필드(description·config)는 담지 않는다."""

    connector_id: str
    short_id: str
    name: str
    title: str | None
    short_description: str
    category: str
    transport: Transport | None
    endpoint_url: str | None
    scope_type: ScopeType
    scope_id: str | None
    creator_user_id: str
    verified_status: str | None
    health_status: str
    tags: list[str]
    star_count: int
    created_at: datetime
    updated_at: datetime


class ConnectorDetail(ConnectorSummary):
    description: str | None
    license: str | None
    source_repo_url: str
    compatible_hosts: list[str]
    visibility_teams: list[str]
    tools_fetched_at: datetime | None
    tools_fetch_error: str | None
    last_checked_at: datetime | None


class ConnectorPage(BaseModel):
    """키셋 페이지. `next_cursor` 가 없으면 마지막이다.

    offset 을 쓰지 않는 이유는 목록이 자주 바뀌기 때문이다 — 새 카드가 앞에 끼면 offset
    기반 다음 페이지가 이미 본 항목을 다시 준다.
    """

    items: list[ConnectorSummary]
    next_cursor: str | None = None


class TagCount(BaseModel):
    tag: str
    count: int


class CatalogStats(BaseModel):
    total: int
    by_health: dict[str, int]
