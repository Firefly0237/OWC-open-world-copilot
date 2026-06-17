"""Request principal resolution + per-tenant world isolation (WS-P).

``resolve_principal`` turns request credentials into a Principal: a JWT bearer → its claims; the
X-API-Key loopback → the single-tenant LOCAL owner (so every pre-SaaS endpoint behaves unchanged).
``tenant_world_root`` resolves a world strictly inside the caller's tenant directory, applying the
same traversal/zip-slip hardening as workspaces — a tenant cannot name its way into another's data.
"""

from __future__ import annotations

import hmac
import re
from pathlib import Path

from .auth import AuthError, verify_token
from .models import LOCAL_PRINCIPAL, Principal, Role


def resolve_principal(
    *,
    authorization: str | None,
    x_api_key: str | None,
    expected_api_key: str | None,
    secret: str | None = None,
) -> Principal:
    """Resolve the caller. A bearer JWT wins; otherwise the X-API-Key loopback grants the LOCAL
    owner. With neither (and a key is required) it raises AuthError — never a silent open door."""
    if authorization and authorization.lower().startswith("bearer "):
        return verify_token(authorization[7:].strip(), secret=secret)
    # Constant-time compare so the API key isn't timing-probeable (matches the JWT signature path).
    if expected_api_key is None or (
        x_api_key is not None and hmac.compare_digest(x_api_key, expected_api_key)
    ):
        return LOCAL_PRINCIPAL
    raise AuthError("缺少有效的 Authorization 或 X-API-Key")


# Reject path separators, traversal, and the Windows drive marker ':' (e.g. "C:") in a world name.
_BAD = re.compile(r"[/\\:]|\.\.")


def _sanitize_world(world: str) -> str:
    name = world.strip()
    if not name or _BAD.search(name) or Path(name).is_absolute():
        raise ValueError(f"非法的世界名：{world!r}")
    return name


def tenant_world_root(principal: Principal, world: str, *, worlds_home: str | Path) -> Path:
    """The on-disk root for ``world`` within the caller's tenant. Hardened against traversal: the
    resolved path must stay under ``<worlds_home>/<tenant_id>/``."""
    tenant_dir = (Path(worlds_home) / principal.tenant_id).resolve()
    target = (tenant_dir / _sanitize_world(world)).resolve()
    if tenant_dir not in target.parents and target != tenant_dir:
        raise ValueError("世界路径越过租户边界")
    return target


def require_role(principal: Principal, minimum: Role) -> None:
    """Raise PermissionError when the caller's role is below ``minimum`` (RBAC gate)."""
    if not principal.role.at_least(minimum):
        raise PermissionError(f"需要 {minimum.value} 及以上权限，当前 {principal.role.value}")
