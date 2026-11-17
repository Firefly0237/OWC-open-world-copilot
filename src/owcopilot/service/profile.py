"""Explicit deployment profile and capability firewall.

``local`` keeps the existing single-process/file-backed product behavior.  ``team``
is intentionally fail-closed for surfaces whose ownership or durable storage has
not been migrated yet; returning NOT_AVAILABLE is safer than presenting local
process state as shared team state.
"""

from __future__ import annotations

import os
from typing import Literal

from fastapi import HTTPException, Request

DeploymentProfile = Literal["local", "team"]

PROFILE_ENV = "OWCOPILOT_PROFILE"
LOCAL_PROFILE: DeploymentProfile = "local"
TEAM_PROFILE: DeploymentProfile = "team"
_VALID_PROFILES = {LOCAL_PROFILE, TEAM_PROFILE}
SYNCHRONOUS_EXTRACTION_CAPABILITY = "synchronous_extraction_intake"


def deployment_profile() -> DeploymentProfile:
    """Read the explicit profile, preserving local as the compatibility default."""
    raw = os.getenv(PROFILE_ENV, LOCAL_PROFILE)
    if raw == LOCAL_PROFILE:
        return LOCAL_PROFILE
    if raw == TEAM_PROFILE:
        return TEAM_PROFILE
    allowed = "|".join(sorted(_VALID_PROFILES))
    raise ValueError(f"{PROFILE_ENV} must be one of {allowed}; got {raw!r}")


def not_available_detail(capability: str) -> dict[str, str]:
    return {
        "code": "NOT_AVAILABLE",
        "profile": TEAM_PROFILE,
        "capability": capability,
        "message": (
            f"{capability} is not available in team profile until it has shared "
            "ownership and durable storage"
        ),
    }


def require_local_capability(request: Request, capability: str) -> None:
    """Reject an unmigrated local-only surface before it touches local state."""
    if request.app.state.deployment_profile == TEAM_PROFILE:
        raise HTTPException(status_code=503, detail=not_available_detail(capability))


def require_local_synchronous_extraction(request: Request) -> None:
    """Front-door dependency for the process-local extraction run/submit hand-off."""
    require_local_capability(request, SYNCHRONOUS_EXTRACTION_CAPABILITY)
