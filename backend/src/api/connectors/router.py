"""Connector 카탈로그·CRUD 라우터."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.connectors.schemas import (
    CatalogStats,
    ConnectorDetail,
    ConnectorHealthResponse,
    ConnectorPage,
    ConnectorSummary,
    ConnectorTool,
    ConnectorToolsPreviewRequest,
    ConnectorToolsResponse,
    ConnectorWrite,
    ProbeError,
    SortKey,
    TagCount,
)
from api.deps import CurrentSessionDep, OptionalSessionDep, viewer_scope
from api.rate_limit import probe_rate_limit
from core.connectors import mutations, probes, queries
from core.connectors.visibility import can_edit
from core.db import get_session
from core.mcp.client import (
    ConnectorUnreachableError,
    LiveTool,
    ProbeFailure,
    check_liveness,
    fetch_tools,
    tools_to_cache,
)
from core.settings import load_settings

if TYPE_CHECKING:
    from core.site_auth import SiteSession

router = APIRouter(tags=["connectors"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _with_staleness(row: dict[str, object]) -> dict[str, object]:
    """행에 `health_stale` 을 얹는다.

    SQL 에서 계산하지 않는 이유는 임계값이 설정이라 모든 조회에 파라미터가 하나씩 더
    붙기 때문이다. 값 하나를 시각에서 파생하는 것이라 조회 결과를 다시 읽을 필요도 없다.
    """
    stale_after = load_settings().connector_health_stale_after_sec
    return {**row, "health_stale": probes.is_stale(row.get("last_checked_at"), stale_after)}  # type: ignore[arg-type]


def _summary(row: dict[str, object]) -> ConnectorSummary:
    return ConnectorSummary.model_validate(_with_staleness(row))


def _detail(row: dict[str, object]) -> ConnectorDetail:
    return ConnectorDetail.model_validate(_with_staleness(row))


def _error(failure: ProbeFailure) -> ProbeError:
    return ProbeError(code=failure.code, message=failure.message)


def _to_tool(t: LiveTool) -> ConnectorTool:
    return ConnectorTool(
        name=t.name,
        description=t.description,
        input_schema=t.input_schema,
        read_only=t.read_only,
    )


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
    viewer_id, teams = viewer_scope(viewer)
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
    return ConnectorPage(items=[_summary(r) for r in rows], next_cursor=next_cursor)


@router.get("/connectors/tags", response_model=list[TagCount], summary="태그 목록")
async def list_tags(db: SessionDep, viewer: OptionalSessionDep) -> list[TagCount]:
    viewer_id, teams = viewer_scope(viewer)
    rows = await queries.list_tags(db, viewer_id=viewer_id, viewer_teams=teams)
    return [TagCount.model_validate(r) for r in rows]


@router.get("/connectors/stats", response_model=CatalogStats, summary="카탈로그 지표")
async def stats(db: SessionDep, viewer: OptionalSessionDep) -> CatalogStats:
    viewer_id, teams = viewer_scope(viewer)
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
        items=[_summary(r) for r in mine],
        next_cursor=next_cursor if len(mine) == len(rows) else None,
    )


@router.get("/connectors/{connector_id}", response_model=ConnectorDetail, summary="커넥터 상세")
async def get_connector(
    connector_id: str, db: SessionDep, viewer: OptionalSessionDep
) -> ConnectorDetail:
    viewer_id, teams = viewer_scope(viewer)
    row = await queries.get_connector(
        db, connector_id=connector_id, viewer_id=viewer_id, viewer_teams=teams
    )
    if row is None:
        # 보이지 않는 카드와 없는 카드를 **같은 404** 로 돌려준다. 403 으로 가르면 비공개
        # 카드의 존재가 새어 나간다 — 팀 비공개로 등록한 사람은 남이 그 사실조차 모르기를
        # 기대한다.
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return _detail(row)


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
    # 등록 직후 서버가 직접 endpoint 에 붙어 tools 를 캐시한다. 프론트가 미리보기 결과를
    # 왕복 저장하게 하면 위변조된 목록이 그대로 카드에 실린다 — 화면에 보이는 도구 목록은
    # 서버가 본 것이어야 한다. 실패해도 등록은 성공으로 남긴다(사유는 캐시에 기록).
    await _refresh_tools(
        db,
        connector_id=connector_id,
        endpoint_url=str(body.endpoint_url) if body.endpoint_url else None,
        transport=body.transport,
        timeout=load_settings().connector_register_fetch_timeout_sec,
    )
    # 캐시를 채운 **뒤에** 읽는다. 순서를 바꾸면 방금 받은 도구 목록과 폴백으로 교정된
    # transport 가 201 응답에 빠져, 등록 화면이 곧바로 새로고침해야 제 값을 본다.
    row = await queries.get_connector(
        db, connector_id=connector_id, viewer_id=viewer.sub, viewer_teams=viewer.team_codes
    )
    assert row is not None  # 방금 만든 카드는 본인에게 보인다
    return _detail(row)


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
    return _detail(row)


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


# ---- tools 캐시 · liveness (connector-hub#3) ----------------------------------------------


async def _refresh_tools(
    db: AsyncSession,
    *,
    connector_id: str,
    endpoint_url: object,
    transport: object,
    timeout: float,
) -> None:
    """endpoint 에 붙어 tools 를 받아 캐시를 갱신한다(등록 직후·수동 새로고침 공용).

    도달 실패를 예외로 올리지 않는다 — 호출부(등록·조회)는 그대로 진행해야 하고, 실패
    사유는 캐시에 남아 다음 조회가 함께 보여 준다.

    **원격 왕복 전에 트랜잭션을 닫는다.** 안 그러면 endpoint 가 느린 동안 풀 커넥션 하나가
    `idle in transaction` 으로 묶여, 동시 새로고침이 풀 크기만큼만 쌓여도 커넥터와 무관한
    요청까지 대기한다. 여기 오기 전까지는 읽기뿐이거나 이미 커밋한 뒤라 잃을 쓰기가 없다.
    """
    if not endpoint_url or not transport:
        return
    await db.rollback()
    try:
        fetched = await fetch_tools(str(endpoint_url), str(transport), timeout=timeout)
    except ConnectorUnreachableError as exc:
        await probes.save_tools_cache(
            db, connector_id=connector_id, tools=None, error=exc.failure.message
        )
    else:
        await probes.save_tools_cache(
            db, connector_id=connector_id, tools=tools_to_cache(fetched.tools), error=None
        )
        if fetched.transport != transport:
            # 폴백이 등록값과 다른 쪽으로 성공했다 — 되기록해 다음 요청이 첫 시도에 맞히게 한다.
            await probes.correct_transport(
                db, connector_id=connector_id, transport=fetched.transport
            )
    await db.commit()


@router.post(
    "/connectors/preview-tools",
    response_model=ConnectorToolsResponse,
    summary="등록 전 tools 미리보기",
    dependencies=[Depends(probe_rate_limit("preview"))],
)
async def preview_tools(
    body: ConnectorToolsPreviewRequest, viewer: CurrentSessionDep
) -> ConnectorToolsResponse:
    """카드가 아직 없는 상태에서 endpoint 만으로 tools 를 보여 준다.

    DB 를 전혀 건드리지 않는다 — 여기서 받은 목록은 저장되지 않고, 등록 후 서버가 다시
    붙어 캐시한다. 로그인과 분당 상한을 요구한다: 이 경로가 임의 주소로 서버를 내보내는
    유일한 입구라 남용 속도를 꺾어 둬야 한다.
    """
    try:
        fetched = await fetch_tools(
            str(body.endpoint_url),
            body.transport,
            timeout=load_settings().connector_probe_timeout_sec,
        )
    except ConnectorUnreachableError as exc:
        return ConnectorToolsResponse(tools=[], error=_error(exc.failure))
    return ConnectorToolsResponse(tools=[_to_tool(t) for t in fetched.tools])


@router.get(
    "/connectors/{connector_id}/tools",
    response_model=ConnectorToolsResponse,
    summary="커넥터 tools (캐시 우선)",
)
async def get_tools(
    connector_id: str,
    db: SessionDep,
    viewer: OptionalSessionDep,
    refresh: bool = False,
) -> ConnectorToolsResponse:
    """캐시된 tools 를 돌려준다. `?refresh=true` 면 endpoint 에 다시 붙어 캐시를 갱신한다.

    가시성 게이트는 상세와 같다 — 보이지 않는 카드는 404 로 존재조차 알리지 않는다.
    """
    viewer_id, teams = viewer_scope(viewer)
    row = await queries.get_connector(
        db, connector_id=connector_id, viewer_id=viewer_id, viewer_teams=teams
    )
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})

    resolved_id = str(row["connector_id"])
    if not row["endpoint_url"] or not row["transport"]:
        return ConnectorToolsResponse(
            tools=[],
            error=ProbeError(code="no_endpoint", message="이 커넥터에는 접속 주소가 없다"),
        )

    if refresh:
        # 새로고침만 로그인과 상한을 요구한다. 라우트 전체에 의존성으로 걸지 않는 이유는
        # `refresh=false` 일 때 이 경로가 DB 만 읽기 때문이다 — 그쪽까지 묶으면 전역 공개
        # 커넥터를 익명으로 보는 평범한 조회가 막힌다. 반대로 여기를 열어 두면 로그인 없이
        # 임의 주소로 서버를 내보낼 수 있다.
        if viewer is None:
            raise HTTPException(status_code=401, detail={"code": "unauthenticated"})
        probe_rate_limit("refresh")(viewer)
        await _refresh_tools(
            db,
            connector_id=resolved_id,
            endpoint_url=row["endpoint_url"],
            transport=row["transport"],
            timeout=load_settings().connector_probe_timeout_sec,
        )

    cached, fetched_at, cache_error = await probes.read_tools_cache(db, connector_id=resolved_id)
    stale = probes.is_stale(fetched_at, load_settings().connector_tools_stale_after_sec)
    if cached is None:
        return ConnectorToolsResponse(
            tools=[],
            error=ProbeError(code="not_fetched", message=cache_error or "아직 도구를 받지 못했다"),
            stale=True,
        )
    return ConnectorToolsResponse(
        tools=[
            ConnectorTool(
                name=str(t.get("name") or ""),
                description=t.get("description"),
                input_schema=t.get("input_schema"),
                # 구 캐시 행에 read_only 키가 없어도 배지가 사라질 뿐 깨지지 않는다.
                read_only=t.get("read_only") if isinstance(t.get("read_only"), bool) else None,
            )
            for t in cached
        ],
        # 캐시는 남아 있는데 마지막 fetch 가 실패한 상태 — 목록과 사유를 함께 보여 준다.
        error=ProbeError(code="stale_cache", message=cache_error) if cache_error else None,
        fetched_at=fetched_at,
        cached=True,
        stale=stale,
    )


@router.post(
    "/connectors/{connector_id}/health-check",
    response_model=ConnectorHealthResponse,
    summary="liveness 즉시 재검사",
    dependencies=[Depends(probe_rate_limit("health"))],
)
async def recheck_health(
    connector_id: str, db: SessionDep, viewer: CurrentSessionDep
) -> ConnectorHealthResponse:
    """지금 endpoint 에 붙어 살아있는지 다시 판정하고 상태를 갱신한다.

    워커는 주기가 있어 방금 서버를 고친 사람이 결과를 바로 못 본다. 이 경로가 그 대기를
    없앤다 — 워커와 **같은 판정 함수**를 써서 두 경로가 다른 결론을 내지 않게 한다.

    도달 실패는 오류가 아니라 상태다: 200 + `health_status='unhealthy'` 로 내려간다.
    """
    row = await queries.get_connector(
        db, connector_id=connector_id, viewer_id=viewer.sub, viewer_teams=viewer.team_codes
    )
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})

    resolved_id = str(row["connector_id"])
    transport = row["transport"]
    if not row["endpoint_url"] or not transport:
        # 검사할 대상이 없다 — 상태를 건드리지 않고 현재값을 그대로 돌려준다.
        return ConnectorHealthResponse(
            health_status=str(row["health_status"]),
            last_checked_at=row["last_checked_at"],
            stale=probes.is_stale(
                row["last_checked_at"], load_settings().connector_health_stale_after_sec
            ),
            error=ProbeError(code="no_endpoint", message="이 커넥터에는 접속 주소가 없다"),
        )

    await db.rollback()  # 원격 왕복 전에 커넥션을 놓는다(_refresh_tools 와 같은 이유)
    liveness = await check_liveness(
        str(row["endpoint_url"]),
        str(transport),
        timeout=load_settings().connector_probe_timeout_sec,
    )
    checked_at = await probes.record_health(
        db,
        connector_id=resolved_id,
        healthy=liveness.healthy,
        last_error=liveness.failure.message if liveness.failure else None,
    )
    if liveness.transport is not None and liveness.transport != transport:
        await probes.correct_transport(db, connector_id=resolved_id, transport=liveness.transport)
    await db.commit()
    return ConnectorHealthResponse(
        health_status="healthy" if liveness.healthy else "unhealthy",
        last_checked_at=checked_at,
        stale=False,
        error=_error(liveness.failure) if liveness.failure else None,
    )
