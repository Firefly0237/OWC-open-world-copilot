"""WS-P · multi-tenant platform: HS256 auth, RBAC, tenant isolation, control-plane store, audit."""

from __future__ import annotations

import time

import pytest

from owcopilot.platform import (
    AuditEntry,
    AuthError,
    Membership,
    PlatformStore,
    Principal,
    Role,
    Tenant,
    User,
    mint_token,
    require_role,
    resolve_principal,
    tenant_world_root,
    verify_token,
)

_SECRET = "unit-test-secret"


def _principal(tenant: str = "t1", role: Role = Role.EDITOR) -> Principal:
    return Principal(user_id="u1", tenant_id=tenant, role=role)


def test_signed_token_with_non_numeric_exp_is_autherror_not_raw_valueerror() -> None:
    # hardening: a (validly-signed) token whose exp is non-numeric must raise AuthError, not let a
    # raw int() ValueError leak through
    import base64
    import hashlib
    import hmac
    import json

    def b64(d: bytes) -> str:
        return base64.urlsafe_b64encode(d).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(
        json.dumps({"sub": "u1", "tenant": "t1", "role": "editor", "exp": "soon"}).encode()
    )
    sig = b64(hmac.new(_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    with pytest.raises(AuthError):
        verify_token(f"{header}.{payload}.{sig}", secret=_SECRET)


def test_tenant_world_name_rejects_drive_marker(tmp_path) -> None:
    # hardening: a Windows drive marker ':' in a world name is rejected (belt over boundary check)
    with pytest.raises(ValueError, match="非法的世界名"):
        tenant_world_root(_principal(), "C:", worlds_home=tmp_path)


# --------------------------------------------------------------- auth
def test_token_round_trips() -> None:
    token = mint_token(_principal(), secret=_SECRET)
    back = verify_token(token, secret=_SECRET)
    assert back.user_id == "u1" and back.tenant_id == "t1" and back.role == Role.EDITOR


def test_tampered_token_is_rejected() -> None:
    token = mint_token(_principal(role=Role.VIEWER), secret=_SECRET)
    header, payload, sig = token.split(".")
    forged = mint_token(_principal(role=Role.OWNER), secret=_SECRET).split(".")[1]
    with pytest.raises(AuthError, match="签名无效"):
        verify_token(f"{header}.{forged}.{sig}", secret=_SECRET)  # swap payload, keep old sig


def test_expired_token_is_rejected() -> None:
    token = mint_token(_principal(), secret=_SECRET, ttl_seconds=-1)
    with pytest.raises(AuthError, match="过期"):
        verify_token(token, secret=_SECRET)
    assert int(time.time())  # sanity


def test_wrong_secret_is_rejected() -> None:
    token = mint_token(_principal(), secret=_SECRET)
    with pytest.raises(AuthError, match="签名无效"):
        verify_token(token, secret="other-secret")


# --------------------------------------------------------------- RBAC
def test_role_ordering_and_require_role() -> None:
    assert Role.OWNER.at_least(Role.VIEWER) and not Role.VIEWER.at_least(Role.EDITOR)
    require_role(_principal(role=Role.EDITOR), Role.EDITOR)  # ok
    with pytest.raises(PermissionError, match="权限"):
        require_role(_principal(role=Role.VIEWER), Role.EDITOR)


# --------------------------------------------------------------- principal resolution
def test_resolve_prefers_bearer_then_loopback() -> None:
    token = mint_token(_principal("t9", Role.REVIEWER), secret=_SECRET)
    by_jwt = resolve_principal(
        authorization=f"Bearer {token}", x_api_key=None, expected_api_key="k", secret=_SECRET
    )
    assert by_jwt.tenant_id == "t9" and by_jwt.role == Role.REVIEWER

    loopback = resolve_principal(authorization=None, x_api_key="k", expected_api_key="k")
    assert loopback.is_loopback and loopback.role == Role.OWNER

    with pytest.raises(AuthError):
        resolve_principal(authorization=None, x_api_key="wrong", expected_api_key="k")


# --------------------------------------------------------------- tenant isolation
def test_tenant_world_root_isolates_and_blocks_traversal(tmp_path) -> None:
    a = tenant_world_root(_principal("tenant_a"), "world1", worlds_home=tmp_path)
    b = tenant_world_root(_principal("tenant_b"), "world1", worlds_home=tmp_path)
    assert a != b  # same world name, different tenants -> different roots
    assert "tenant_a" in str(a) and "tenant_b" in str(b)
    # tenant A cannot name its way into tenant B's data
    for evil in ("../tenant_b/world1", "..", "a/b", "/etc/passwd"):
        with pytest.raises(ValueError):
            tenant_world_root(_principal("tenant_a"), evil, worlds_home=tmp_path)


# --------------------------------------------------------------- control-plane store + audit
def test_store_memberships_and_audit() -> None:
    store = PlatformStore()
    try:
        store.create_tenant(Tenant(id="t1", name="Studio"))
        store.create_user(User(id="u1", email="a@x.com"))
        store.add_membership(Membership(tenant_id="t1", user_id="u1", role=Role.EDITOR))
        assert store.role_of("t1", "u1") == Role.EDITOR
        assert store.role_of("t1", "ghost") is None
        store.record(AuditEntry(tenant_id="t1", user_id="u1", action="world.create", target="w1"))
        trail = store.audit_for_tenant("t1")
        assert len(trail) == 1 and trail[0].action == "world.create"
        assert store.audit_for_tenant("t2") == []  # other tenant sees nothing
    finally:
        store.close()
