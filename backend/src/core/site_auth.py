"""사이트 세션 검증 — AgentToolbox Site Auth 계약의 소비자 구현.

계약 정본: AgentToolbox `docs/architecture/site-auth-contract.md`. 이 모듈은 그 문서의
§2(JWT 검증)·§4(introspection)·§6(소비자 체크리스트)를 코드로 옮긴 것이다.

두 단계를 **모두** 거쳐야 인가한다.

1. **서명 검증** — JWKS 공개키로 토큰이 위조가 아님을 확인한다. 여기까지는 "이 토큰을
   Site Auth 가 발급했다" 까지만 말한다.
2. **introspection** — 그 세션이 아직 살아 있는지, 사용자가 지금 어느 팀인지 묻는다.
   폐기(로그아웃)는 서명에 드러나지 않으므로 이 단계가 없으면 로그아웃한 사용자가
   토큰 만료(기본 12시간)까지 계속 들어온다.

**허용 알고리즘은 우리 설정이 정한다.** 토큰 헤더의 `alg` 를 믿고 키 종류를 고르면,
공개키를 HS256 비밀로 써서 서명한 위조가 통과한다. `alg=none` 은 어느 목록에도 없다.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from jwt import PyJWK

#: 받아들일 서명 알고리즘. Site Auth 는 EdDSA 를 기본으로 쓰고 사내 도구 호환 문제가 있으면
#: RS256 으로 간다(계약 §2.3). HS256 은 **없다** — 그 방식은 검증에 공유 비밀이 필요하고,
#: 그 비밀을 이쪽이 갖지 않는 것이 분리의 목적이다.
ALLOWED_ALGORITHMS: tuple[str, ...] = ("EdDSA", "RS256")


class SiteAuthError(Exception):
    """Site Auth 호출이 불가능하거나 설정이 없을 때 — 인증 실패와 구분한다."""


@dataclass(frozen=True)
class SiteSession:
    """검증을 통과한 세션. `team_codes` 가 공개범위 판정의 축이다."""

    sub: str
    sid: str
    role: str
    team_codes: tuple[str, ...]
    expires_at: str | None = None


@dataclass
class _Cached:
    value: Any
    expires_at: float


@dataclass
class SiteAuthClient:
    """JWKS·introspection 을 캐시해 부르는 클라이언트.

    프로세스당 하나를 만들어 재사용한다. `httpx.AsyncClient` 를 요청마다 만들면 연결 풀을
    버리게 되고, JWKS 캐시도 매번 비게 된다.
    """

    base_url: str
    service_token: str
    issuer: str
    audience: str
    http: httpx.AsyncClient
    introspect_cache_sec: int = 45
    jwks_cache_sec: int = 600
    #: 모르는 `kid` 때문에 JWKS 를 강제로 다시 받는 최소 간격(초). 이 값이 없으면 모르는
    #: `kid` 를 흘려보내는 것만으로 Site Auth 를 요청 수만큼 두드릴 수 있다 — 인증되지 않은
    #: 입력이 곧 아웃바운드 호출이 되는 증폭 경로다. 키 교체는 드물고 즉시성이 필요하지도
    #: 않으므로, 짧은 쿨다운으로 교체 수용과 증폭 차단을 함께 얻는다.
    jwks_min_refetch_sec: float = 10.0
    _jwks: _Cached | None = field(default=None, repr=False)
    _last_forced_refetch: float = field(default=0.0, repr=False)
    _introspect: dict[str, _Cached] = field(default_factory=dict, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # ---- JWKS ----
    async def _fetch_jwks(self) -> dict[str, PyJWK]:
        resp = await self.http.get(
            f"{self.base_url}/auth/.well-known/jwks.json",
            headers={"X-API-Key": self.service_token},
        )
        resp.raise_for_status()
        keys: dict[str, PyJWK] = {}
        for raw in resp.json().get("keys", []):
            kid = raw.get("kid")
            # alg 가 우리 허용 목록 밖이면 아예 담지 않는다. 담아 두면 나중에 그 키로
            # 검증하려다 알고리즘 판단이 두 곳으로 갈린다.
            if isinstance(kid, str) and raw.get("alg") in ALLOWED_ALGORITHMS:
                keys[kid] = PyJWK.from_dict(raw)
        return keys

    async def _keys(self, *, force: bool = False) -> dict[str, PyJWK]:
        now = time.monotonic()
        if not force and self._jwks is not None and self._jwks.expires_at > now:
            return dict(self._jwks.value)
        async with self._lock:
            # 락 대기 중 다른 코루틴이 이미 채웠을 수 있다.
            if not force and self._jwks is not None and self._jwks.expires_at > time.monotonic():
                return dict(self._jwks.value)
            keys = await self._fetch_jwks()
            self._jwks = _Cached(value=keys, expires_at=time.monotonic() + self.jwks_cache_sec)
            return dict(keys)

    async def _key_for(self, kid: str) -> PyJWK | None:
        """`kid` 로 키를 찾는다. 없으면 쿨다운 안에서 한 번 다시 받아 본다.

        키 교체 직후에는 캐시에 새 `kid` 가 없는 것이 정상이다. 그때마다 실패시키면 교체가
        곧 장애가 된다. 그렇다고 모르는 `kid` 마다 다시 받으면, 인증되지 않은 입력이 그대로
        아웃바운드 호출이 되어 요청 수만큼 Site Auth 를 두드릴 수 있다.

        그래서 강제 재조회에 `jwks_min_refetch_sec` 쿨다운을 둔다. 교체는 드물고 몇 초 늦게
        반영돼도 문제가 없지만, 증폭은 몇 초 만에 문제가 된다.
        """
        keys = await self._keys()
        if kid in keys:
            return keys[kid]

        now = time.monotonic()
        if now - self._last_forced_refetch < self.jwks_min_refetch_sec:
            return None
        self._last_forced_refetch = now
        return (await self._keys(force=True)).get(kid)

    # ---- 검증 ----
    async def verify_signature(self, token: str) -> dict[str, Any]:
        """1단계. 서명·`iss`·`aud`·`exp` 검증. 실패하면 예외를 던진다."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise jwt.InvalidTokenError(f"malformed header: {exc}") from exc

        alg, kid = header.get("alg"), header.get("kid")
        if alg not in ALLOWED_ALGORITHMS:
            # `alg=none` 과 HS256 위장이 여기서 걸린다.
            raise jwt.InvalidAlgorithmError(f"algorithm {alg!r} is not allowed")
        if not isinstance(kid, str):
            raise jwt.InvalidTokenError("missing kid")

        key = await self._key_for(kid)
        if key is None:
            raise jwt.InvalidKeyError(f"unknown kid {kid!r}")
        if key.algorithm_name is not None and key.algorithm_name != alg:
            raise jwt.InvalidAlgorithmError(
                f"kid {kid!r} is {key.algorithm_name}, token says {alg}"
            )

        # 검증에 넘기는 알고리즘은 **JWKS 가 말한 것**이다. 토큰이 준 값을 그대로 흘리지 않는다.
        return dict(
            jwt.decode(
                token,
                key,
                algorithms=[key.algorithm_name or alg],
                issuer=self.issuer,
                audience=self.audience,
            )
        )

    async def introspect(self, token: str) -> SiteSession | None:
        """2단계. 세션 생존과 현재 팀. 살아 있지 않으면 None.

        응답을 짧게 캐시한다(계약 §4.1). 캐시 키는 토큰 자체이므로 사용자마다 갈린다.
        """
        now = time.monotonic()
        hit = self._introspect.get(token)
        if hit is not None and hit.expires_at > now:
            return hit.value  # type: ignore[no-any-return]

        resp = await self.http.post(
            f"{self.base_url}/auth/introspect",
            json={"token": token},
            headers={"X-API-Key": self.service_token},
        )
        resp.raise_for_status()
        body = resp.json()
        session = (
            SiteSession(
                sub=body["sub"],
                sid=body["sid"],
                role=body.get("role") or "user",
                team_codes=tuple(body.get("team_codes") or ()),
                expires_at=body.get("expires_at"),
            )
            if body.get("active")
            else None
        )
        self._introspect[token] = _Cached(value=session, expires_at=now + self.introspect_cache_sec)
        return session

    async def resolve(self, token: str) -> SiteSession | None:
        """두 단계를 순서대로. 어느 쪽이라도 실패하면 None.

        서명 검증을 **먼저** 한다. 위조 토큰으로 introspection 을 부르게 두면 Site Auth 가
        아무나 던진 문자열을 처리하게 되고, 이쪽 캐시도 쓰레기로 채워진다.
        """
        try:
            await self.verify_signature(token)
        except jwt.PyJWTError:
            return None
        return await self.introspect(token)


def build_http_client(timeout_sec: float) -> httpx.AsyncClient:
    """Site Auth 전용 클라이언트.

    `trust_env=False` — upstream 은 사내망 내부 호스트다. 환경 프록시를 타면 내부 주소가
    외부 프록시로 나가고, 그쪽에서 막히거나(연결 실패) 새어 나간다.
    """
    return httpx.AsyncClient(timeout=timeout_sec, trust_env=False)
