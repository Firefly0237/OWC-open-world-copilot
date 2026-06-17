"""Minimal HS256 bearer tokens (a JWT, implemented on the stdlib so no new dependency).

Production can swap this for an OIDC provider — the contract is just ``mint_token`` /
``verify_token`` returning a ``Principal``. Tokens are signed (HMAC-SHA256), carry tenant + role +
expiry, and any tamper or expiry makes verification raise ``AuthError`` (never a silent accept).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from .models import Principal, Role


class AuthError(ValueError):
    """Raised when a token is missing, malformed, tampered, or expired."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _secret(secret: str | None) -> bytes:
    value = secret or os.getenv("OWCOPILOT_JWT_SECRET")
    if not value:
        raise AuthError("OWCOPILOT_JWT_SECRET 未设置，无法签发/校验令牌")
    return value.encode("utf-8")


def mint_token(principal: Principal, *, secret: str | None = None, ttl_seconds: int = 3600) -> str:
    key = _secret(secret)
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps(
            {
                "sub": principal.user_id,
                "tenant": principal.tenant_id,
                "role": principal.role.value,
                "exp": int(time.time()) + ttl_seconds,
            },
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = _b64url(hmac.new(key, signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def verify_token(token: str, *, secret: str | None = None) -> Principal:
    key = _secret(secret)
    try:
        header_b64, payload_b64, signature = token.split(".")
    except ValueError as exc:
        raise AuthError("令牌格式不正确") from exc
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected = _b64url(hmac.new(key, signing_input, hashlib.sha256).digest())
    if not hmac.compare_digest(expected, signature):  # constant-time; rejects any tamper
        raise AuthError("令牌签名无效")
    try:
        claims = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthError("令牌载荷无法解析") from exc
    try:
        expires_at = int(claims.get("exp", 0))
    except (TypeError, ValueError) as exc:  # a signed-but-malformed exp is still a bad token
        raise AuthError("令牌的过期时间无效") from exc
    if expires_at < int(time.time()):
        raise AuthError("令牌已过期")
    try:
        role = Role(claims["role"])
    except (KeyError, ValueError) as exc:
        raise AuthError("令牌缺少有效角色") from exc
    return Principal(
        user_id=str(claims.get("sub", "")),
        tenant_id=str(claims.get("tenant", "")),
        role=role,
    )
