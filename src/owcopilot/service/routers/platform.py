# 多租户控制面：principal/租户/成员/开发令牌/审计。
# Tenancy control plane.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from ...platform import AuditEntry, AuthError, Membership, Principal, Role, Tenant, User, mint_token
from ..dependencies import _principal, _require_role_http
from ..schemas import DevTokenRequest, MembershipRequest, TenantCreateRequest

router = APIRouter()


@router.get("/platform/me")
def platform_me(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    return _principal(authorization, x_api_key).model_dump(mode="json")


@router.post("/platform/tenants", status_code=201)
def platform_create_tenant(
    req: TenantCreateRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    principal = _principal(authorization, x_api_key)
    _require_role_http(principal, Role.OWNER)
    owner = User(id=f"{req.tenant_id}_owner", email=req.owner_email)
    request.app.state.platform_store.create_tenant(Tenant(id=req.tenant_id, name=req.name))
    request.app.state.platform_store.create_user(owner)
    request.app.state.platform_store.add_membership(
        Membership(tenant_id=req.tenant_id, user_id=owner.id, role=Role.OWNER)
    )
    request.app.state.platform_store.record(
        AuditEntry(
            tenant_id=req.tenant_id,
            user_id=principal.user_id,
            action="tenant.create",
            target=req.tenant_id,
        )
    )
    return {"tenant_id": req.tenant_id, "owner_user_id": owner.id}


@router.post("/platform/memberships", status_code=201)
def platform_add_member(
    req: MembershipRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    principal = _principal(authorization, x_api_key)
    _require_role_http(principal, Role.OWNER)
    try:
        request.app.state.platform_store.create_user(User(id=req.user_id, email=req.email))
    except Exception:  # noqa: BLE001 — user may already exist; membership is the point
        pass
    request.app.state.platform_store.add_membership(
        Membership(tenant_id=req.tenant_id, user_id=req.user_id, role=Role(req.role))
    )
    return {"tenant_id": req.tenant_id, "user_id": req.user_id, "role": req.role}


@router.post("/platform/auth/dev-token")
def platform_dev_token(
    req: DevTokenRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Mint a bearer token (dev/test; production uses the OIDC provider). Owner-gated."""
    principal = _principal(authorization, x_api_key)
    _require_role_http(principal, Role.OWNER)
    try:
        token = mint_token(
            Principal(user_id=req.user_id, tenant_id=req.tenant_id, role=Role(req.role)),
            ttl_seconds=req.ttl_seconds,
        )
    except AuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"token": token, "token_type": "Bearer"}


@router.get("/platform/audit")
def platform_audit(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    principal = _principal(authorization, x_api_key)
    return {
        "tenant_id": principal.tenant_id,
        "entries": [
            e.model_dump(mode="json")
            for e in request.app.state.platform_store.audit_for_tenant(principal.tenant_id)
        ],
    }
