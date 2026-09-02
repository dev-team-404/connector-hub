"""SiteAuthClient 의 캐시·키 교체·introspection 동작.

계약 문서 §2.2(JWKS 캐시와 모르는 kid 재조회)·§4.1(introspection 캐시)을 코드로 고정한다.

단독 실행: `cd backend && uv run pytest tests/test_site_auth_client.py -q`
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.site_auth import SiteAuthClient

_ISS = "https://agent-factory.test"
_AUD = "agent-factory-site"


def _keypair() -> tuple[str, dict[str, Any]]:
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    algorithm = jwt.algorithms.get_default_algorithms()["EdDSA"]
    jwk = json.loads(algorithm.to_jwk(algorithm.prepare_key(public_pem)))
    return private_pem, jwk


def _token(private_pem: str, kid: str, *, sub: str = "u_1", jti: str = "s_1") -> str:
    now = int(time.time())
    return jwt.encode(
        {"jti": jti, "sub": sub, "iss": _ISS, "aud": _AUD, "iat": now, "exp": now + 3600},
        private_pem,
        algorithm="EdDSA",
        headers={"kid": kid},
    )


class _FakeSiteAuth:
    """호출 횟수를 세는 가짜 Site Auth."""

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, Any]] = {}
        self.jwks_calls = 0
        self.introspect_calls = 0
        self.introspect_body: dict[str, Any] = {"active": False}

    def add_key(self, kid: str, jwk: dict[str, Any]) -> None:
        self.keys[kid] = {**jwk, "kid": kid, "alg": "EdDSA", "use": "sig"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("jwks.json"):
            self.jwks_calls += 1
            return httpx.Response(200, json={"keys": list(self.keys.values())})
        if request.url.path.endswith("/auth/introspect"):
            self.introspect_calls += 1
            return httpx.Response(200, json=self.introspect_body)
        raise AssertionError(f"unexpected call: {request.url}")

    def client(self, **kwargs: Any) -> SiteAuthClient:
        return SiteAuthClient(
            base_url="http://site-auth.test",
            service_token="t" * 32,
            issuer=_ISS,
            audience=_AUD,
            http=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
            **kwargs,
        )


@pytest.fixture
def fake() -> _FakeSiteAuth:
    return _FakeSiteAuth()


async def test_jwks_is_fetched_once_and_cached(fake: _FakeSiteAuth) -> None:
    private_pem, jwk = _keypair()
    fake.add_key("k1", jwk)
    client = fake.client()
    try:
        for _ in range(3):
            await client.verify_signature(_token(private_pem, "k1"))
        assert fake.jwks_calls == 1, "요청마다 받아 오면 Site Auth 가 병목이 된다"
    finally:
        await client.http.aclose()


async def test_unknown_kid_triggers_exactly_one_refetch(fake: _FakeSiteAuth) -> None:
    """키 교체 직후에는 캐시에 새 kid 가 없는 것이 정상이다 — 그때마다 실패시키면 교체가 장애다."""
    old_pem, old_jwk = _keypair()
    fake.add_key("k1", old_jwk)
    client = fake.client()
    try:
        await client.verify_signature(_token(old_pem, "k1"))
        assert fake.jwks_calls == 1

        # 발급자가 키를 교체했다. 구 키는 검증용으로 남아 있고 새 키가 추가된 상태.
        new_pem, new_jwk = _keypair()
        fake.add_key("k2", new_jwk)

        await client.verify_signature(_token(new_pem, "k2"))
        assert fake.jwks_calls == 2, "모르는 kid 를 만나면 한 번 다시 받아야 한다"

        # 구 kid 로 서명된 토큰도 계속 통과해야 한다(두 세대 공존).
        await client.verify_signature(_token(old_pem, "k1"))
    finally:
        await client.http.aclose()


async def test_unknown_kid_refetch_is_throttled(fake: _FakeSiteAuth) -> None:
    """모르는 kid 를 흘려보내는 것만으로 Site Auth 를 두드릴 수 있으면 안 된다.

    인증되지 않은 입력이 그대로 아웃바운드 호출이 되면 증폭 경로가 된다. 쿨다운을 두어
    요청 수와 무관하게 재조회 횟수가 묶이는지 본다.
    """
    other_pem, _ = _keypair()
    _, jwk = _keypair()
    fake.add_key("k1", jwk)
    client = fake.client(jwks_min_refetch_sec=60.0)
    try:
        for _ in range(20):
            with pytest.raises(jwt.PyJWTError):
                await client.verify_signature(_token(other_pem, "does-not-exist"))
        assert fake.jwks_calls == 2, "최초 1회 + 강제 재조회 1회. 나머지는 쿨다운에 걸린다"
    finally:
        await client.http.aclose()


async def test_rotation_is_still_accepted_once_the_cooldown_passes(fake: _FakeSiteAuth) -> None:
    """쿨다운이 키 교체 수용을 막으면 안 된다.

    쿨다운을 0 으로 두면 `test_unknown_kid_refetch_is_throttled` 와 같은 코드 경로가 교체를
    그대로 받아들인다 — 즉 증폭을 막는 장치와 교체를 받는 장치가 같은 곳이고, 차이는
    간격뿐임을 고정한다.
    """
    old_pem, old_jwk = _keypair()
    fake.add_key("k1", old_jwk)
    client = fake.client(jwks_min_refetch_sec=0.0)
    try:
        await client.verify_signature(_token(old_pem, "k1"))

        new_pem, new_jwk = _keypair()
        fake.add_key("k2", new_jwk)
        payload = await client.verify_signature(_token(new_pem, "k2", sub="u_rotated"))
        assert payload["sub"] == "u_rotated"
    finally:
        await client.http.aclose()


async def test_introspect_result_is_cached(fake: _FakeSiteAuth) -> None:
    private_pem, jwk = _keypair()
    fake.add_key("k1", jwk)
    fake.introspect_body = {
        "active": True,
        "sub": "u_1",
        "sid": "s_1",
        "role": "user",
        "team_codes": ["D1"],
        "expires_at": "2026-09-02T18:00:00+00:00",
    }
    client = fake.client(introspect_cache_sec=60)
    token = _token(private_pem, "k1")
    try:
        first = await client.resolve(token)
        assert first is not None
        assert first.team_codes == ("D1",)
        for _ in range(4):
            await client.resolve(token)
        assert fake.introspect_calls == 1, "요청마다 부르면 이쪽이 Site Auth 를 때린다"
    finally:
        await client.http.aclose()


async def test_inactive_session_resolves_to_none(fake: _FakeSiteAuth) -> None:
    """서명이 멀쩡해도 폐기됐으면 인가하지 않는다 — 이 단계가 없으면 로그아웃이 무의미하다."""
    private_pem, jwk = _keypair()
    fake.add_key("k1", jwk)
    fake.introspect_body = {"active": False}
    client = fake.client()
    try:
        assert await client.resolve(_token(private_pem, "k1")) is None
    finally:
        await client.http.aclose()


async def test_forged_token_never_reaches_introspection(fake: _FakeSiteAuth) -> None:
    """서명 검증을 먼저 한다 — 아니면 아무 문자열이나 Site Auth 로 흘러가고 캐시가 오염된다."""
    _, jwk = _keypair()
    fake.add_key("k1", jwk)
    client = fake.client()
    try:
        assert await client.resolve("not-a-jwt") is None
        assert fake.introspect_calls == 0
    finally:
        await client.http.aclose()
