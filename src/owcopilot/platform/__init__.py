# 多租户平台控制面（WS-P）：租户/鉴权/RBAC/隔离/审计。
"""Multi-tenant platform control plane (WS-P): tenancy, auth, RBAC, isolation, audit.

The canon stays file-backed; this layer governs who may touch which tenant's worlds. The X-API-Key
loopback resolves to a single-tenant LOCAL owner so the pre-SaaS local/CLI/offline flows are
unchanged. The control-plane store is SQLite and the tokens are HS256; the contracts would admit a
heavier store or an OIDC provider, but neither is implemented.
"""

from __future__ import annotations

from .auth import AuthError, mint_token, verify_token
from .models import (
    LOCAL_PRINCIPAL,
    LOCAL_TENANT,
    AuditEntry,
    Membership,
    Principal,
    Role,
    Tenant,
    TenantId,
    User,
)
from .store import PlatformStore
from .tenancy import require_role, resolve_principal, tenant_world_root

__all__ = [
    "LOCAL_PRINCIPAL",
    "LOCAL_TENANT",
    "AuditEntry",
    "AuthError",
    "Membership",
    "PlatformStore",
    "Principal",
    "Role",
    "Tenant",
    "TenantId",
    "User",
    "mint_token",
    "require_role",
    "resolve_principal",
    "tenant_world_root",
    "verify_token",
]
