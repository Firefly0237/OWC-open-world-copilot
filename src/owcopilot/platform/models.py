"""Multi-tenant platform metadata models (WS-P).

These describe the SaaS control plane — tenants, users, memberships, roles — that wraps the
file-backed canon. The canon itself stays file-backed (a per-tenant ContentStore); this layer only
governs *who* may touch *which* tenant's worlds, and records an audit trail. Deliberately small.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class Role(str, Enum):
    OWNER = "owner"  # full control incl. membership + delete
    EDITOR = "editor"  # create/edit/generate content
    REVIEWER = "reviewer"  # approve/reject review items
    VIEWER = "viewer"  # read only

    def at_least(self, minimum: Role) -> bool:
        order = [Role.VIEWER, Role.REVIEWER, Role.EDITOR, Role.OWNER]
        return order.index(self) >= order.index(minimum)


class Tenant(BaseModel):
    id: str
    name: str
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class User(BaseModel):
    id: str
    email: str
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class Membership(BaseModel):
    tenant_id: str
    user_id: str
    role: Role


class Principal(BaseModel):
    """The resolved caller of one request: who, in which tenant, with what role."""

    user_id: str
    tenant_id: str
    role: Role
    is_loopback: bool = False  # the local/dev X-API-Key owner (single-tenant local mode)


# The implicit principal when the request comes through the X-API-Key loopback (local/CI/offline):
# a single-tenant owner so every existing endpoint behaves exactly as before SaaS wiring.
LOCAL_TENANT = "local"
LOCAL_PRINCIPAL = Principal(
    user_id="local", tenant_id=LOCAL_TENANT, role=Role.OWNER, is_loopback=True
)


class AuditEntry(BaseModel):
    at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    tenant_id: str
    user_id: str
    action: str  # e.g. "world.create", "review.decide", "world.delete"
    target: str = ""
