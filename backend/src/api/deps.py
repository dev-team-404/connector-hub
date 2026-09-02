"""요청 → SiteSession 의존성.

세션 쿠키를 우선 보고, 없으면 `Authorization: Bearer` 를 본다. AgentToolbox 와 같은 순서다
— 브라우저는 쿠키로 오고 CLI 성격의 호출만 헤더를 쓴다.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from core.site_auth import SiteAuthClient, SiteSession

#: AgentToolbox 가 발급하는 세션 쿠키 이름. 같은 호스트를 공유하므로 같은 쿠키가 온다.
SESSION_COOKIE_NAME = "session"


def _token_from(request: Request) -> str | None:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie:
        return cookie
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def get_site_auth(request: Request) -> SiteAuthClient:
    client = getattr(request.app.state, "site_auth", None)
    if client is None:
        # 설정이 없으면 부팅에서 이미 막힌다. 여기 오면 배선이 빠진 것이다.
        raise HTTPException(status_code=503, detail={"code": "site_auth_unavailable"})
    return client  # type: ignore[no-any-return]


async def get_optional_session(request: Request) -> SiteSession | None:
    """익명 접근을 허용하는 라우트용. 토큰이 없거나 유효하지 않으면 None."""
    token = _token_from(request)
    if not token:
        return None
    return await get_site_auth(request).resolve(token)


async def get_current_session(request: Request) -> SiteSession:
    """로그인을 요구하는 라우트용. 실패하면 401.

    **설정 확인을 먼저 한다.** 토큰이 없으면 `get_optional_session` 이 곧바로 None 을
    돌려주므로, 순서를 바꾸면 인증이 아예 배선되지 않은 배포도 평범한 401 로 보인다.
    운영자는 "로그인이 안 된다" 는 신고를 받고 사용자 쪽을 뒤지게 된다.

    익명을 허용하는 라우트에는 이 검사가 없다 — 그쪽은 설정이 없어도 익명으로 계속
    동작하는 것이 맞다.
    """
    get_site_auth(request)
    session = await get_optional_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail={"code": "unauthenticated"})
    return session


def viewer_scope(session: SiteSession | None) -> tuple[str | None, tuple[str, ...]]:
    """가시성 술어에 넣을 (사용자, 팀). 익명이면 (None, ()) — 전역 공개만 남는다.

    라우터 둘이 같은 변환을 쓴다. 한쪽에만 두면 다른 쪽이 자기 버전을 만들고, 그러다
    한 군데가 팀을 빠뜨리면 비공개 카드가 노출된다.
    """
    return (session.sub, session.team_codes) if session else (None, ())


CurrentSessionDep = Annotated[SiteSession, Depends(get_current_session)]
OptionalSessionDep = Annotated[SiteSession | None, Depends(get_optional_session)]
