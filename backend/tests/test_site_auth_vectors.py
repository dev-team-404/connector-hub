"""사이트 세션 JWT 검증 — 공유 계약 벡터 대조.

`tests/fixtures/site_jwt/vectors.json` 은 AgentToolbox 에서 복사한 사본이다. 발급자와 소비자가
**같은 판정**을 내는지 확인하는 것이 이 파일의 목적이다.

벡터의 `expect` 는 발급자의 모드별 판정인데, 소비자에게는 모드가 없다 — 비대칭 서명만 받는다.
그래서 `asymmetric` 열만 읽는다. `hs256_legacy_valid` 가 그 차이를 드러낸다.

단독 실행: `cd backend && uv run pytest tests/test_site_auth_vectors.py -q`
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.site_auth import SiteAuthClient

_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "site_jwt" / "vectors.json").read_text(encoding="utf-8")
)


def _generate(alg: str) -> tuple[str, str]:
    key = (
        Ed25519PrivateKey.generate()
        if alg == "EdDSA"
        else rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
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
    return private_pem, public_pem


_KEYS: dict[str, dict[str, str]] = {}
for _name, _spec in _VECTORS["keys"].items():
    _priv, _pub = _generate(_spec["alg"])
    _KEYS[_name] = {
        "kid": _spec["kid"],
        "alg": _spec["alg"],
        "private_pem": _priv,
        "public_pem": _pub,
    }


def _key(name: str) -> dict[str, str]:
    return _KEYS[name]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _jwk(name: str) -> dict[str, Any]:
    """JWKS 에 실릴 형태. 발급자의 `SigningKey.to_jwk()` 와 같은 모양이어야 한다."""
    k = _key(name)
    algorithm = jwt.algorithms.get_default_algorithms()[k["alg"]]
    entry = json.loads(algorithm.to_jwk(algorithm.prepare_key(k["public_pem"])))
    entry.update({"kid": k["kid"], "alg": k["alg"], "use": "sig"})
    return entry


def _forge_hs256_with_public_key(payload: dict[str, Any]) -> str:
    """공개키 바이트를 HMAC 비밀로 쓴 위조. PyJWT 가 막으므로 손으로 굽는다."""
    header = {"alg": "HS256", "typ": "JWT", "kid": _key("active")["kid"]}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    sig = hmac.new(
        _key("active")["public_pem"].encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_b64url(sig)}"


def _build_token(build: dict[str, Any]) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "jti": "s_vector",
        "sub": "u_vector",
        "iss": _VECTORS["issuer"],
        "aud": _VECTORS["audience"],
        "iat": now + int(build.get("iat_offset_sec", 0)),
        "exp": now + int(build.get("exp_offset_sec", 3600)),
        "role": "user",
    }
    payload.update(build.get("claims", {}))
    for claim in build.get("omit", []):
        payload.pop(claim, None)

    sign = build["sign"]
    if sign == "hs256":
        token = jwt.encode(payload, _VECTORS["secret"], algorithm="HS256")
    elif sign == "hs256_with_public_key":
        token = _forge_hs256_with_public_key(payload)
    elif sign == "none":
        token = jwt.encode(payload, key="", algorithm="none")
    else:
        k = _key(sign)
        token = jwt.encode(payload, k["private_pem"], algorithm=k["alg"], headers={"kid": k["kid"]})

    if build.get("tamper_signature"):
        signing_input, _, sig = token.rpartition(".")
        # 마지막 base64 글자만 바꾸면 안 된다 — Ed25519 서명 64바이트는 86글자로 실려 끝
        # 글자에 미사용 비트 4개가 남고, 그것만 건드리면 디코드 결과가 같다.
        raw = bytearray(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)))
        raw[0] ^= 0xFF
        token = f"{signing_input}.{_b64url(bytes(raw))}"
    return token


def _client(*, jwks_keys: list[str] | None = None) -> SiteAuthClient:
    """JWKS 를 응답하는 가짜 Site Auth 를 붙인 클라이언트.

    `unknown`·`rsa` 키는 JWKS 에 넣지 않는다 — 그 둘이 "설정에 없는 키" 케이스다.
    """
    names = jwks_keys if jwks_keys is not None else ["active", "rotated"]
    body = {"keys": [_jwk(n) for n in names]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json=body)
        raise AssertionError(f"unexpected call: {request.url}")

    return SiteAuthClient(
        base_url="http://site-auth.test",
        service_token="t" * 32,
        issuer=_VECTORS["issuer"],
        audience=_VECTORS["audience"],
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize("case", _VECTORS["cases"], ids=lambda c: c["name"])
async def test_signature_verification_matches_the_contract(case: dict[str, Any]) -> None:
    """소비자는 발급자의 `asymmetric` 판정과 같아야 한다."""
    expected = case["expect"]["asymmetric"]
    token = _build_token(case["build"])
    client = _client()
    try:
        if expected == "accept":
            payload = await client.verify_signature(token)
            assert payload["sub"] == "u_vector", case["why"]
        else:
            with pytest.raises(jwt.PyJWTError):
                await client.verify_signature(token)
    finally:
        await client.http.aclose()


async def test_hs256_is_rejected_even_though_the_issuer_may_accept_it() -> None:
    """발급자의 `dual` 은 HS256 을 받지만 소비자는 받으면 안 된다.

    받으려면 공유 비밀이 있어야 하고, 그 비밀을 갖지 않는 것이 분리의 목적이다.
    """
    assert (
        next(c for c in _VECTORS["cases"] if c["name"] == "hs256_legacy_valid")["expect"]["dual"]
        == "accept"
    )

    token = _build_token({"sign": "hs256", "omit": ["aud"]})
    client = _client()
    try:
        with pytest.raises(jwt.InvalidAlgorithmError):
            await client.verify_signature(token)
    finally:
        await client.http.aclose()
