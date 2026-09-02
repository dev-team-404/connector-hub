"""알림 인박스 (connector-hub#4).

**이 서비스의 알림만 담는다.** AgentToolbox 알림과 합산하지 않는다(설계 §12) — 두 화면의
알림 메뉴는 같은 모양이지만 각자 자기 것만 보여 준다. 합치려면 한쪽이 다른 쪽 DB 를 읽거나
상시 통신을 만들어야 하는데, 둘 다 분리로 없애려던 것이다.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.connectors.schemas import Notification, NotificationPage
from api.deps import CurrentSessionDep  # noqa: TC001
from core.connectors import social
from core.db import get_session

router = APIRouter(tags=["notifications"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/me/notifications", response_model=NotificationPage, summary="내 알림")
async def list_notifications(
    db: SessionDep,
    viewer: CurrentSessionDep,
    unread_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> NotificationPage:
    """`unread` 는 필터와 무관하게 **읽지 않은 전체 개수**다 — 헤더 배지가 쓰는 값이라
    현재 페이지에 몇 개 있는지로 바뀌면 안 된다."""
    items, unread = await social.list_notifications(
        db, user_id=viewer.sub, unread_only=unread_only, limit=limit, offset=offset
    )
    return NotificationPage(items=[Notification.model_validate(i) for i in items], unread=unread)


@router.post(
    "/me/notifications/{notification_id}/read",
    response_model=NotificationPage,
    summary="알림 하나 읽음",
)
async def read_one(
    notification_id: str, db: SessionDep, viewer: CurrentSessionDep
) -> NotificationPage:
    """없는 알림이나 남의 알림이면 아무것도 바뀌지 않는다 — 404 로 가르지 않는다.

    읽음 처리는 되돌릴 것도 없고, 존재 여부를 알려 봐야 남의 알림 id 를 찍어 보는 데만
    쓰인다. 갱신 후의 목록을 그대로 돌려줘 화면이 한 번 더 조회하지 않게 한다.
    """
    await social.mark_read(db, user_id=viewer.sub, notification_id=notification_id)
    await db.commit()
    return await list_notifications(db, viewer)


@router.post("/me/notifications/read", response_model=NotificationPage, summary="알림 전부 읽음")
async def read_all(db: SessionDep, viewer: CurrentSessionDep) -> NotificationPage:
    await social.mark_read(db, user_id=viewer.sub, notification_id=None)
    await db.commit()
    return await list_notifications(db, viewer)
