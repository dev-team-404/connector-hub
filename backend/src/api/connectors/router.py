"""Connector 카탈로그·CRUD 라우터."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.connectors.schemas import (
    CatalogStats,
    ConnectorDetail,
    ConnectorPage,
    ConnectorSummary,
    ConnectorWrite,
    SortKey,
    TagCount,
)
from api.deps import CurrentSessionDep, OptionalSessionDep  # noqa: TC001
from core.connectors import mutations, queries
from core.connectors.visibility import can_edit
from core.db import get_session

if TYPE_CHECKING:
    from core.site_auth import SiteSession

router = APIRouter(tags=["connectors"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _viewer(session: SiteSession | None) -> tuple[str | None, tuple[str, ...]]:
    return (session.sub, session.team_codes) if session else (None, ())


def _write_payload(body: ConnectorWrite) -> dict[str, object]:
    return {
        "name": body.name,
        "title": body.title,
        "short_description": body.short_description,
        "description": body.description,
        "category": body.category,
        "license": body.license,
        "source_repo_url": str(body.source_repo_url),
        "endpoint_url": str(body.endpoint_url) if body.endpoint_url else None,
        "transport": body.transport,
        "scope_type": body.scope_type,
        "scope_id": body.scope_id,
    }


def _validated(body: ConnectorWrite) -> ConnectorWrite:
    try:
        body.check_consistency()
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "invalid_connector", "message": str(exc)}
        ) from exc
    return body


@router.get("/connectors", response_model=ConnectorPage, summary="카탈로그 목록")
async def list_connectors(
    db: SessionDep,
    viewer: OptionalSessionDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    tag: Annotated[list[str] | None, Query(description="모두 가진 카드만")] = None,
    sort: SortKey = "recent",
    limit: Annotated[int, Query(ge=1, le=queries.MAX_PAGE_SIZE)] = queries.DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> ConnectorPage:
    viewer_id, teams = _viewer(viewer)
    try:
        rows, next_cursor = await queries.list_connectors(
            db,
            viewer_id=viewer_id,
            viewer_teams=teams,
            q=q,
            tags=tuple(tag or ()),
            sort=sort,
            limit=limit,
            cursor=cursor,
        )
    except queries.CursorError as exc:
        # 잘못된 커서는 클라이언트 잘못이다. 500 으로 떨구면 원인을 알 수 없다.
        raise HTTPException(
            status_code=400, detail={"code": "invalid_cursor", "message": str(exc)}
        ) from exc
    return ConnectorPage(
        items=[ConnectorSummary.model_validate(r) for r in rows], next_cursor=next_cursor
    )


@router.get("/connectors/tags", response_model=list[TagCount], summary="태그 목록")
async def list_tags(db: SessionDep, viewer: OptionalSessionDep) -> list[TagCount]:
    viewer_id, teams = _viewer(viewer)
    rows = await queries.list_tags(db, viewer_id=viewer_id, viewer_teams=teams)
    return [TagCount.model_validate(r) for r in rows]


@router.get("/connectors/stats", response_model=CatalogStats, summary="카탈로그 지표")
async def stats(db: SessionDep, viewer: OptionalSessionDep) -> CatalogStats:
    viewer_id, teams = _viewer(viewer)
    return CatalogStats.model_validate(
        await queries.catalog_stats(db, viewer_id=viewer_id, viewer_teams=teams)
    )


@router.get("/me/connectors", response_model=ConnectorPage, summary="내가 등록한 커넥터")
async def my_connectors(
    db: SessionDep,
    viewer: CurrentSessionDep,
    limit: Annotated[int, Query(ge=1, le=queries.MAX_PAGE_SIZE)] = queries.DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> ConnectorPage:
    """본인 카드만. 공개범위와 무관하게 자기 것은 모두 보인다(가시성 술어의 creator 절)."""
    rows, next_cursor = await queries.list_connectors(
        db, viewer_id=viewer.sub, viewer_teams=(), limit=limit, cursor=cursor
    )
    mine = [r for r in rows if r["creator_user_id"] == viewer.sub]
    return ConnectorPage(
        items=[ConnectorSummary.model_validate(r) for r in mine],
        next_cursor=next_cursor if len(mine) == len(rows) else None,
    )


@router.get("/connectors/{connector_id}", response_model=ConnectorDetail, summary="커넥터 상세")
async def get_connector(
    connector_id: str, db: SessionDep, viewer: OptionalSessionDep
) -> ConnectorDetail:
    viewer_id, teams = _viewer(viewer)
    row = await queries.get_connector(
        db, connector_id=connector_id, viewer_id=viewer_id, viewer_teams=teams
    )
    if row is None:
        # 보이지 않는 카드와 없는 카드를 **같은 404** 로 돌려준다. 403 으로 가르면 비공개
        # 카드의 존재가 새어 나간다 — 팀 비공개로 등록한 사람은 남이 그 사실조차 모르기를
        # 기대한다.
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return ConnectorDetail.model_validate(row)


@router.post(
    "/connectors",
    response_model=ConnectorDetail,
    status_code=status.HTTP_201_CREATED,
    summary="커넥터 등록",
)
async def create_connector(
    body: ConnectorWrite, db: SessionDep, viewer: CurrentSessionDep
) -> ConnectorDetail:
    _validated(body)
    creator_team = body.scope_id or (viewer.team_codes[0] if viewer.team_codes else "")
    if not creator_team:
        raise HTTPException(
            status_code=422,
            detail={"code": "no_team", "message": "소속 팀을 알 수 없어 등록할 수 없다"},
        )
    connector_id = await mutations.create_connector(
        db,
        payload=_write_payload(body),
        tags=body.tags,
        visibility_teams=body.visibility_teams,
        creator_user_id=viewer.sub,
        creator_team_id=creator_team,
    )
    row = await queries.get_connector(
        db, connector_id=connector_id, viewer_id=viewer.sub, viewer_teams=viewer.team_codes
    )
    assert row is not None  # 방금 만든 카드는 본인에게 보인다
    return ConnectorDetail.model_validate(row)


async def _require_owner(db: AsyncSession, connector_id: str, viewer: SiteSession) -> None:
    owner = await mutations.get_owner(db, connector_id=connector_id)
    if owner is None or not can_edit(owner, viewer.sub):
        # 남의 카드 수정 시도와 없는 카드가 같은 응답이다. 여기서도 존재를 흘리지 않는다.
        raise HTTPException(status_code=404, detail={"code": "not_found"})


@router.patch("/connectors/{connector_id}", response_model=ConnectorDetail, summary="커넥터 수정")
async def update_connector(
    connector_id: str, body: ConnectorWrite, db: SessionDep, viewer: CurrentSessionDep
) -> ConnectorDetail:
    _validated(body)
    await _require_owner(db, connector_id, viewer)
    await mutations.update_connector(
        db,
        connector_id=connector_id,
        payload=_write_payload(body),
        tags=body.tags,
        visibility_teams=body.visibility_teams,
    )
    row = await queries.get_connector(
        db, connector_id=connector_id, viewer_id=viewer.sub, viewer_teams=viewer.team_codes
    )
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return ConnectorDetail.model_validate(row)


@router.delete(
    "/connectors/{connector_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="커넥터 삭제",
)
async def delete_connector(
    connector_id: str, db: SessionDep, viewer: CurrentSessionDep
) -> Response:
    await _require_owner(db, connector_id, viewer)
    await mutations.delete_connector(db, connector_id=connector_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
