# 多租户控制面：principal/租户/成员/开发令牌/审计。
# Tenancy control plane.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ...platform import AuditEntry, AuthError, Membership, Principal, Role, Tenant, User, mint_token
from ..dependencies import _principal, _require_role_http
from ..pagination import PaginationCursorError, paginate
from ..schemas import DevTokenRequest, MembershipRequest, TenantCreateRequest

router = APIRouter()


def _bearer_token(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        value = authorization[7:].strip()
        return value or None
    return None


def _platform_principal(
    request: Request,
    authorization: str | None,
    x_api_key: str | None,
) -> Principal:
    token = _bearer_token(authorization)
    if token and request.app.state.platform_store.token_is_revoked(token):
        raise HTTPException(status_code=401, detail="开发令牌已撤销")
    return _principal(authorization, x_api_key)


def _require_tenant_owner(principal: Principal, tenant_id: str) -> None:
    _require_role_http(principal, Role.OWNER)
    if not principal.is_loopback and principal.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="不能管理其他租户")


def _page_or_error(items: list[dict[str, Any]], *, limit: int, cursor: str | None) -> Any:
    try:
        return paginate(items, limit=limit, cursor=cursor)
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc


@router.get("/platform/me")
def platform_me(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    return _platform_principal(request, authorization, x_api_key).model_dump(mode="json")


@router.post("/platform/tenants", status_code=201)
def platform_create_tenant(
    req: TenantCreateRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    principal = _platform_principal(request, authorization, x_api_key)
    _require_role_http(principal, Role.OWNER)
    if not principal.is_loopback:
        raise HTTPException(status_code=403, detail="只有本机平台所有者可以创建租户")
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
    principal = _platform_principal(request, authorization, x_api_key)
    _require_tenant_owner(principal, req.tenant_id)
    try:
        request.app.state.platform_store.upsert_user(User(id=req.user_id, email=req.email))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="邮箱已被其他平台用户使用") from exc
    request.app.state.platform_store.add_membership(
        Membership(tenant_id=req.tenant_id, user_id=req.user_id, role=Role(req.role))
    )
    request.app.state.platform_store.record(
        AuditEntry(
            tenant_id=req.tenant_id,
            user_id=principal.user_id,
            action="membership.upsert",
            target=f"{req.user_id}:{req.role}",
        )
    )
    return {"tenant_id": req.tenant_id, "user_id": req.user_id, "role": req.role}


@router.get("/platform/memberships")
def platform_memberships(
    request: Request,
    tenant_id: str = Query(min_length=1, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1000),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    principal = _platform_principal(request, authorization, x_api_key)
    _require_tenant_owner(principal, tenant_id)
    page = _page_or_error(
        request.app.state.platform_store.list_memberships(tenant_id),
        limit=limit,
        cursor=cursor,
    )
    return {
        "tenant_id": tenant_id,
        "memberships": page.items,
        "total": page.total,
        "next_cursor": page.next_cursor,
        "snapshot_etag": page.snapshot_etag,
    }


@router.delete("/platform/memberships/{tenant_id}/{user_id}")
def platform_remove_member(
    tenant_id: str,
    user_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    principal = _platform_principal(request, authorization, x_api_key)
    _require_tenant_owner(principal, tenant_id)
    store = request.app.state.platform_store
    members = store.list_memberships(tenant_id)
    target = next((member for member in members if member["user_id"] == user_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="成员不存在")
    if target["role"] == Role.OWNER.value:
        owner_count = sum(member["role"] == Role.OWNER.value for member in members)
        if owner_count <= 1:
            raise HTTPException(status_code=409, detail="不能移除租户最后一名所有者")
    store.remove_membership(tenant_id, user_id)
    store.record(
        AuditEntry(
            tenant_id=tenant_id,
            user_id=principal.user_id,
            action="membership.remove",
            target=user_id,
        )
    )
    return {"tenant_id": tenant_id, "removed_user_id": user_id}


@router.post("/platform/auth/dev-token")
def platform_dev_token(
    req: DevTokenRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Mint a bearer token (dev/test; production uses the OIDC provider). Owner-gated."""
    principal = _platform_principal(request, authorization, x_api_key)
    _require_tenant_owner(principal, req.tenant_id)
    role = Role(req.role)
    try:
        token = mint_token(
            Principal(user_id=req.user_id, tenant_id=req.tenant_id, role=role),
            ttl_seconds=req.ttl_seconds,
        )
    except AuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    token_id = f"dt_{uuid.uuid4().hex}"
    expires_at = (datetime.now(UTC) + timedelta(seconds=req.ttl_seconds)).isoformat()
    metadata = request.app.state.platform_store.register_dev_token(
        token_id=token_id,
        token=token,
        tenant_id=req.tenant_id,
        user_id=req.user_id,
        role=role,
        issued_by=principal.user_id,
        expires_at=expires_at,
    )
    request.app.state.platform_store.record(
        AuditEntry(
            tenant_id=req.tenant_id,
            user_id=principal.user_id,
            action="dev_token.create",
            target=token_id,
        )
    )
    return {"token": token, "token_type": "Bearer", **metadata}


@router.get("/platform/auth/dev-tokens")
def platform_dev_tokens(
    request: Request,
    tenant_id: str = Query(min_length=1, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1000),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    principal = _platform_principal(request, authorization, x_api_key)
    _require_tenant_owner(principal, tenant_id)
    page = _page_or_error(
        request.app.state.platform_store.list_dev_tokens(tenant_id),
        limit=limit,
        cursor=cursor,
    )
    return {
        "tenant_id": tenant_id,
        "tokens": page.items,
        "total": page.total,
        "next_cursor": page.next_cursor,
        "snapshot_etag": page.snapshot_etag,
    }


@router.post("/platform/auth/dev-tokens/{token_id}:revoke")
def platform_revoke_dev_token(
    token_id: str,
    request: Request,
    tenant_id: str = Query(min_length=1, max_length=64),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    principal = _platform_principal(request, authorization, x_api_key)
    _require_tenant_owner(principal, tenant_id)
    store = request.app.state.platform_store
    target = next(
        (item for item in store.list_dev_tokens(tenant_id) if item["id"] == token_id),
        None,
    )
    if target is None:
        raise HTTPException(status_code=404, detail="开发令牌不存在")
    revoked = store.revoke_dev_token(tenant_id, token_id, revoked_by=principal.user_id)
    if revoked:
        store.record(
            AuditEntry(
                tenant_id=tenant_id,
                user_id=principal.user_id,
                action="dev_token.revoke",
                target=token_id,
            )
        )
    return {"tenant_id": tenant_id, "token_id": token_id, "revoked": revoked}


@router.get("/platform/audit")
def platform_audit(
    request: Request,
    tenant_id: str | None = Query(default=None, min_length=1, max_length=64),
    limit: int | None = Query(default=None, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1000),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    principal = _platform_principal(request, authorization, x_api_key)
    audit_tenant_id = tenant_id or principal.tenant_id
    if tenant_id is not None:
        _require_tenant_owner(principal, audit_tenant_id)
    entries = [
        entry.model_dump(mode="json")
        for entry in request.app.state.platform_store.audit_for_tenant(audit_tenant_id)
    ]
    if limit is None and cursor is None:
        return {"tenant_id": audit_tenant_id, "entries": entries}
    page = _page_or_error(list(reversed(entries)), limit=limit or 50, cursor=cursor)
    return {
        "tenant_id": audit_tenant_id,
        "entries": page.items,
        "total": page.total,
        "next_cursor": page.next_cursor,
        "snapshot_etag": page.snapshot_etag,
    }
