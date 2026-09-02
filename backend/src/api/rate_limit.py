"""사용자별 요청 상한 — **endpoint 프로브 경로 전용**.

미리보기와 수동 재검사는 인증 사용자가 **임의의 주소로 서버를 내보내는** 유일한 경로다.
SSRF 가드가 어디로 갈 수 있는지를 좁히고, 이쪽은 얼마나 자주 갈 수 있는지를 좁힌다. 둘
중 하나만 있으면 남는 위험(사유 문자열로 하는 포트 스캔)이 실용적인 속도로 가능해진다.

**프로세스 안에서만 센다.** AgentToolbox 는 Redis 로 세지만 이 서비스는 브로커가 없고,
liveness 워커를 위해 하나 들여올 이유도 없다(`worker/main.py`). 복제본이 N 개면 실효
상한이 N 배가 된다 — 남용을 완전히 막는 장치가 아니라 **속도를 꺾는** 장치이고, 그
목적에는 배수가 붙어도 충분하다. 정확한 상한이 필요해지면 그때 공유 저장소를 둔다.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import TYPE_CHECKING

from fastapi import HTTPException

from api.deps import CurrentSessionDep  # noqa: TC001
from core.settings import load_settings

if TYPE_CHECKING:
    from collections.abc import Callable

_WINDOW_SEC = 60.0

#: (버킷, 사용자) → (윈도우 시작, 그 윈도우의 요청 수)
_COUNTERS: dict[str, dict[str, tuple[float, int]]] = defaultdict(dict)


def _hit(bucket: str, user_id: str, limit: int) -> float | None:
    """한 번 세고, 넘었으면 남은 대기 시간(초)을 돌려준다.

    고정 윈도우다. 슬라이딩 윈도우가 더 정확하지만 경계에서 최대 2배가 통과하는 것이 여기
    문제가 되지 않는다 — 목적이 정확한 배분이 아니라 남용 속도를 꺾는 것이라서다.
    """
    now = time.monotonic()
    started, count = _COUNTERS[bucket].get(user_id, (now, 0))
    if now - started >= _WINDOW_SEC:
        started, count = now, 0
    if count >= limit:
        return _WINDOW_SEC - (now - started)
    _COUNTERS[bucket][user_id] = (started, count + 1)
    return None


def reset() -> None:
    """테스트용. 프로세스 전역 상태라 테스트끼리 새어 나가는 것을 막는다."""
    _COUNTERS.clear()


def probe_rate_limit(bucket: str) -> Callable[..., None]:
    """endpoint 프로브 경로에 거는 의존성. 버킷마다 따로 센다.

    한 사용자가 미리보기를 많이 쓴다고 해서 자기 커넥터의 재검사까지 막히면 안 되므로
    두 경로를 한 통에 넣지 않는다.
    """

    def _dependency(session: CurrentSessionDep) -> None:
        limit = load_settings().connector_probe_rate_limit_per_min
        if limit <= 0:  # 0 이하는 비활성 — 로컬 개발에서 끄고 쓸 수 있게 한다.
            return
        retry_after = _hit(bucket, session.sub, limit)
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail={"code": "rate_limited", "message": "요청이 너무 잦다"},
                headers={"Retry-After": str(max(1, int(retry_after) + 1))},
            )

    return _dependency
