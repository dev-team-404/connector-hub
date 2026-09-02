"""댓글 · 별 · 북마크 라우터 (connector-hub#4).

모든 경로가 **카드 가시성 게이트를 먼저 지난다.** 댓글은 카드에 딸린 것이라, 카드를 못
보는 사람이 댓글을 볼 수 있으면 비공개 카드의 내용이 그쪽으로 샌다. 게이트는 상세와 같은
함수(`queries.get_connector`)를 쓴다 — 판정을 복제하지 않는 것이 이 서비스의 규칙이다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.connectors.schemas import (
    Comment,
    CommentEdit,
    CommentPage,
    CommentThread,
    CommentWrite,
    ConnectorPage,
    ReactionState,
)
from api.deps import CurrentSessionDep, OptionalSessionDep, viewer_scope
from core.connectors import queries, social
from core.db import get_session

if TYPE_CHECKING:
    from core.site_auth import SiteSession

router = APIRouter(tags=["social"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def _visible_connector(
    db: AsyncSession, connector_id: str, viewer: SiteSession | None
) -> dict[str, object]:
    """카드를 볼 수 있어야 그 댓글도 볼 수 있다. 못 보면 **404** — 존재를 알리지 않는다."""
    viewer_id, teams = viewer_scope(viewer)
    row = await queries.get_connector(
        db, connector_id=connector_id, viewer_id=viewer_id, viewer_teams=teams
    )
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return row


# ---- 댓글 ----------------------------------------------------------------------------------


@router.get(
    "/connectors/{connector_id}/comments", response_model=CommentPage, summary="댓글 스레드"
)
async def list_comments(
    connector_id: str,
    db: SessionDep,
    viewer: OptionalSessionDep,
    limit: Annotated[int, Query(ge=1, le=social.MAX_COMMENT_PAGE)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CommentPage:
    card = await _visible_connector(db, connector_id, viewer)
    threads, total = await social.list_comments(
        db, connector_id=str(card["connector_id"]), limit=limit, offset=offset
    )
    return CommentPage(
        items=[CommentThread.model_validate(t) for t in threads],
        total=total,
    )


@router.post(
    "/connectors/{connector_id}/comments",
    response_model=Comment,
    status_code=status.HTTP_201_CREATED,
    summary="댓글 작성",
)
async def create_comment(
    connector_id: str, body: CommentWrite, db: SessionDep, viewer: CurrentSessionDep
) -> Comment:
    """댓글 또는 답글. 알림은 카드 소유자와 부모 댓글 작성자에게 간다.

    **답글에 답글은 받지 않는다.** DB 는 임의 깊이를 허용하지만 화면이 렌더할 수 있는
    것은 두 단계이고, 깊이를 열어 두면 세 번째 단계가 어디에 붙는지 사람마다 다르게
    본다. 두 단계로 못 박는 편이 낫다.
    """
    card = await _visible_connector(db, connector_id, viewer)
    resolved = str(card["connector_id"])

    parent = None
    if body.parent_id:
        parent = await social.get_comment(db, comment_id=body.parent_id)
        if parent is None or parent["connector_id"] != resolved or parent["deleted_at"]:
            raise HTTPException(status_code=404, detail={"code": "parent_not_found"})
        if parent["parent_id"] is not None:
            raise HTTPException(
                status_code=422,
                detail={"code": "nested_reply", "message": "답글에는 답글을 달 수 없다"},
            )

    comment_id = await social.create_comment(
        db, connector_id=resolved, author_id=viewer.sub, body=body.body, parent_id=body.parent_id
    )

    payload = {"comment_id": comment_id, "excerpt": body.body[:120]}
    if parent is not None:
        await social.notify(
            db,
            user_id=parent["author_id"],
            actor_id=viewer.sub,
            connector_id=resolved,
            kind=social.KIND_REPLY,
            payload=payload,
        )
    # 카드 소유자에게도 알린다. 부모 작성자와 같은 사람이면 두 번 가지 않게 한 번만 보낸다.
    if parent is None or parent["author_id"] != card["creator_user_id"]:
        await social.notify(
            db,
            user_id=str(card["creator_user_id"]),
            actor_id=viewer.sub,
            connector_id=resolved,
            kind=social.KIND_COMMENT,
            payload=payload,
        )
    await db.commit()

    created = await social.read_comment(db, comment_id=comment_id)
    assert created is not None  # 방금 만든 행이다
    return Comment.model_validate(created)


@router.patch(
    "/connectors/{connector_id}/comments/{comment_id}",
    response_model=Comment,
    summary="댓글 수정",
)
async def edit_comment(
    connector_id: str,
    comment_id: str,
    body: CommentEdit,
    db: SessionDep,
    viewer: CurrentSessionDep,
) -> Comment:
    """**작성자만 고칠 수 있다.** 모더레이터도 남의 글을 고치지는 못한다 — 지우는 것과
    고치는 것은 다르다. 남의 이름으로 남은 글의 내용이 바뀌면 그건 위조다."""
    card = await _visible_connector(db, connector_id, viewer)
    comment = await _owned_comment(db, card, comment_id)
    if comment["author_id"] != viewer.sub:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    await social.update_comment(db, comment_id=comment_id, body=body.body)
    await db.commit()
    updated = await social.read_comment(db, comment_id=comment_id)
    assert updated is not None
    return Comment.model_validate(updated)


@router.delete(
    "/connectors/{connector_id}/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="댓글 삭제",
)
async def delete_comment(
    connector_id: str, comment_id: str, db: SessionDep, viewer: CurrentSessionDep
) -> Response:
    """작성자 또는 이 서비스의 모더레이터.

    모더레이터 판정은 **이 서비스 DB** 에서 한다 — 사이트 JWT 의 `role` 은 공통 역할이라
    Connector 를 조정해도 된다는 뜻이 아니다(마이그레이션 0003).
    """
    card = await _visible_connector(db, connector_id, viewer)
    comment = await _owned_comment(db, card, comment_id)
    if comment["author_id"] != viewer.sub and not await social.is_moderator(db, user_id=viewer.sub):
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    await social.soft_delete_comment(db, comment_id=comment_id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _owned_comment(
    db: AsyncSession, card: dict[str, object], comment_id: str
) -> dict[str, object]:
    comment = await social.get_comment(db, comment_id=comment_id)
    if (
        comment is None
        or comment["connector_id"] != str(card["connector_id"])
        or comment["deleted_at"]
    ):
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return comment


# ---- 별 · 북마크 ----------------------------------------------------------------------------

_TABLES = {"star": "connector_stars", "bookmark": "connector_bookmarks"}


async def _set(
    db: AsyncSession, card: dict[str, object], kind: str, user_id: str, on: bool
) -> ReactionState:
    table = _TABLES[kind]
    resolved = str(card["connector_id"])
    await social.set_reaction(db, table=table, connector_id=resolved, user_id=user_id, on=on)
    await db.commit()
    return ReactionState(
        on=on, count=await social.count_reaction(db, table=table, connector_id=resolved)
    )


@router.put("/connectors/{connector_id}/star", response_model=ReactionState, summary="별 켜기")
async def add_star(connector_id: str, db: SessionDep, viewer: CurrentSessionDep) -> ReactionState:
    """**토글이 아니라 멱등한 켜기다.**

    POST 토글로 두면 재전송이 곧 취소가 된다 — 응답이 늦어 사용자가 한 번 더 누르거나
    클라이언트가 재시도하면 방금 켠 별이 꺼진다. 켜기/끄기를 나누면 몇 번을 보내도 결과가
    같고, DB 의 `(카드, 사용자)` PK 가 그 멱등성을 그대로 받아 준다.
    """
    return await _set(
        db, await _visible_connector(db, connector_id, viewer), "star", viewer.sub, True
    )


@router.delete("/connectors/{connector_id}/star", response_model=ReactionState, summary="별 끄기")
async def remove_star(
    connector_id: str, db: SessionDep, viewer: CurrentSessionDep
) -> ReactionState:
    return await _set(
        db, await _visible_connector(db, connector_id, viewer), "star", viewer.sub, False
    )


@router.put(
    "/connectors/{connector_id}/bookmark", response_model=ReactionState, summary="북마크 켜기"
)
async def add_bookmark(
    connector_id: str, db: SessionDep, viewer: CurrentSessionDep
) -> ReactionState:
    return await _set(
        db, await _visible_connector(db, connector_id, viewer), "bookmark", viewer.sub, True
    )


@router.delete(
    "/connectors/{connector_id}/bookmark", response_model=ReactionState, summary="북마크 끄기"
)
async def remove_bookmark(
    connector_id: str, db: SessionDep, viewer: CurrentSessionDep
) -> ReactionState:
    return await _set(
        db, await _visible_connector(db, connector_id, viewer), "bookmark", viewer.sub, False
    )


@router.get("/me/bookmarks", response_model=ConnectorPage, summary="내가 북마크한 커넥터")
async def my_bookmarks(
    db: SessionDep,
    viewer: CurrentSessionDep,
    limit: Annotated[int, Query(ge=1, le=queries.MAX_PAGE_SIZE)] = queries.DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ConnectorPage:
    """북마크는 **나중에 다시 찾기 위한 것**이라 목록이 없으면 기능이 성립하지 않는다.

    가시성 술어를 그대로 통과시킨다 — 북마크한 뒤 카드가 팀 비공개로 바뀌었다면 더는
    보이지 않아야 한다.
    """
    from api.connectors.router import _summary

    rows = await queries.list_bookmarked(
        db, viewer_id=viewer.sub, viewer_teams=viewer.team_codes, limit=limit, offset=offset
    )
    return ConnectorPage(items=[_summary(r) for r in rows])
