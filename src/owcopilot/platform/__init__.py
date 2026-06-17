"""Multi-tenant platform control plane (WS-P): tenancy, auth, RBAC, isolation, audit.

The canon stays file-backed; this layer governs who may touch which tenant's worlds. The X-API-Key
loopback resolves to a single-tenant LOCAL owner so the pre-SaaS local/CLI/offline flows are
unchanged. Production swaps the SQLite control-plane store for Postgres and the HS256 tokens for an
OIDC provider — same contracts.
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
    "User",
    "mint_token",
    "require_role",
    "resolve_principal",
    "tenant_world_root",
    "verify_token",
]
