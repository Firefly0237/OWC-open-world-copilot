"""Bearer-only identity boundary for the PostgreSQL-backed team profile."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Header, HTTPException

from ..platform import AuthError
from ..platform.auth import verify_token


@dataclass(frozen=True, slots=True)
class TeamIdentity:
    """Authenticated JWT identity; database membership remains the authorization truth."""

    tenant_id: str
    subject_id: str
    asserted_role: str


def _canonical_uuid(value: object, *, claim: str) -> str:
    if type(value) is not str:
        raise HTTPException(status_code=401, detail=f"team token {claim} must be a UUID string")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=401, detail=f"team token {claim} is not a UUID") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise HTTPException(
            status_code=401,
            detail=f"team token {claim} must be a canonical non-nil UUID",
        )
    return value


def require_team_identity(authorization: str | None = Header(default=None)) -> TeamIdentity:
    """Authenticate a team bearer token without accepting the local X-API-Key loopback."""

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="team profile requires Authorization: Bearer")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="team bearer token is empty")
    try:
        principal = verify_token(token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    tenant_id = _canonical_uuid(principal.tenant_id, claim="tenant")
    subject_id = _canonical_uuid(principal.user_id, claim="sub")
    return TeamIdentity(
        tenant_id=tenant_id,
        subject_id=subject_id,
        # This signed assertion is retained only for diagnostics. PostgreSQL membership is
        # reloaded for every request and is the sole authorization role.
        asserted_role=principal.role.value,
    )
