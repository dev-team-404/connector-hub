"""공개범위 판정 — **모든 읽기 경로가 이 한 곳을 통과한다.**

판정을 호출부마다 쓰면 언젠가 한 군데가 빠지고, 그 한 군데가 비공개 카드를 노출한다.
빠진 것은 테스트로도 잘 드러나지 않는다 — 목록은 정상으로 보이고 특정 조합에서만 샌다.
그래서 술어를 문자열 하나로 두고 SQL 을 조립하는 쪽이 그것을 반드시 끼우게 한다.

판정 축은 **introspection 이 준 팀 코드**다(계약 §4). 팀 **이름**으로 판정하지 않는다 —
이름은 표시용이고 바뀔 수 있으며, 같은 이름의 다른 팀이 생길 수 있다.
"""

from __future__ import annotations

from typing import Final

#: 뷰어가 볼 수 있는 카드의 조건. `:viewer_id` 와 `:viewer_teams`(text[]) 를 바인딩한다.
#: 익명 뷰어는 둘 다 NULL/빈 배열로 넘기면 전역 공개만 남는다.
VISIBLE_PREDICATE: Final = """(
    c.scope_type = 'global'
    OR c.creator_user_id = :viewer_id
    OR (c.scope_type = 'team' AND c.scope_id = ANY(:viewer_teams))
    OR EXISTS (
        SELECT 1 FROM connector_visibility_teams v
        WHERE v.connector_id = c.connector_id AND v.team_code = ANY(:viewer_teams)
    )
)"""


def viewer_params(user_id: str | None, team_codes: tuple[str, ...] | None) -> dict[str, object]:
    """술어에 넣을 바인딩. 익명이면 아무것도 매치되지 않는 값으로 채운다.

    `viewer_id` 를 None 으로 두면 `c.creator_user_id = NULL` 이 UNKNOWN 이 되어 그 절이
    조용히 빠진다 — 결과는 맞지만 의도를 읽기 어렵다. 빈 문자열은 실제 user_id 가 될 수
    없으므로 "아무도 아님" 을 명시적으로 표현한다.
    """
    return {
        "viewer_id": user_id or "",
        "viewer_teams": list(team_codes or ()),
    }


def can_edit(creator_user_id: str, viewer_id: str | None) -> bool:
    """수정·삭제 권한.

    관리자 우회는 **여기에 넣지 않는다.** 사이트 JWT 의 `role` 은 공통 역할이라 Connector
    모더레이터를 뜻하지 않는다(계약 §2.1). 이 서비스의 관리자 개념이 생기면 이 서비스 DB 를
    보는 별도 판정으로 추가한다.
    """
    return viewer_id is not None and creator_user_id == viewer_id
