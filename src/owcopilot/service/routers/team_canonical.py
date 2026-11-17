"""The sole N1.2 team canonical HTTP path, backed only by PostgreSQL."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError

from ...contracts import OwnershipScope
from ...contracts.hashing import canonical_json_from_text
from ...teamdb.errors import (
    TeamAuthorizationError,
    TeamCanonicalUnavailableError,
    TeamDatabaseError,
    TeamDatabaseUnavailable,
    TeamIntegrityError,
    TeamNotFoundError,
    TeamSchemaMismatch,
)
from ..pagination import PaginationCursorError, paginate
from ..team_auth import TeamIdentity, require_team_identity

router = APIRouter(prefix="/team", tags=["team-canonical"])

_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 100


def _repository(request: Request) -> Any:
    return request.app.state.team_repository


def _source_ref_count(value: object) -> int:
    if isinstance(value, Mapping):
        count = sum(1 for key in value if key == "source_ref") + sum(
            _source_ref_count(item) for item in value.values()
        )
        payload_json = value.get("payload_json")
        if type(payload_json) is str:
            try:
                canonical = canonical_json_from_text(payload_json)
                count += _source_ref_count(json.loads(canonical))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise TeamIntegrityError("canonical object payload_json is invalid") from exc
        return count
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return sum(_source_ref_count(item) for item in value)
    return 0


def _summary(encoded: object) -> dict[str, int]:
    if not isinstance(encoded, Mapping):
        return {
            "object_count": 0,
            "membership_count": 0,
            "tombstone_count": 0,
            "relation_set_count": 0,
            "source_ref_count": 0,
        }
    objects = encoded.get("objects")
    memberships = encoded.get("memberships")
    object_rows = objects if isinstance(objects, Sequence) else []
    membership_rows = memberships if isinstance(memberships, Sequence) else []
    relation_sets = sum(
        1
        for item in object_rows
        if isinstance(item, Mapping) and item.get("object_type") == "relation_set"
    )
    tombstones = sum(
        1 for item in membership_rows if isinstance(item, Mapping) and item.get("tombstone") is True
    )
    return {
        "object_count": len(object_rows),
        "membership_count": len(membership_rows),
        "tombstone_count": tombstones,
        "relation_set_count": relation_sets,
        "source_ref_count": _source_ref_count(object_rows),
    }


def _rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _page_error(exc: PaginationCursorError) -> HTTPException:
    return HTTPException(
        status_code=409 if exc.stale else 422,
        detail={
            "code": "STALE_CURSOR" if exc.stale else "INVALID_CURSOR",
            "message": str(exc),
        },
    )


@router.get("/containers/{container_id}/canonical")
def read_team_canonical(
    container_id: Annotated[str, Path()],
    request: Request,
    identity: Annotated[TeamIdentity, Depends(require_team_identity)],
    head: Annotated[Literal["authoring", "serving"], Query()] = "authoring",
    collection: Annotated[Literal["objects", "memberships"], Query()] = "objects",
    limit: Annotated[int, Query(ge=1, le=_MAX_PAGE_SIZE)] = _DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    q: Annotated[str, Query(max_length=200)] = "",
    object_type: Annotated[str, Query(max_length=120)] = "",
) -> dict[str, Any]:
    """Read one bounded page from an immutable canonical revision through app-role RLS."""

    try:
        scope = OwnershipScope(tenant_id=identity.tenant_id, container_id=container_id)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail="container_id must be a canonical UUID"
        ) from exc
    repository = _repository(request)
    try:
        grant = repository.authenticate_membership(
            tenant_id=identity.tenant_id,
            subject_id=identity.subject_id,
        )
        state = repository.read_canonical(
            scope,
            subject_id=identity.subject_id,
            head=head,
        )
    except TeamAuthorizationError as exc:
        raise HTTPException(
            status_code=403, detail="team membership does not authorize access"
        ) from exc
    except (TeamNotFoundError, TeamCanonicalUnavailableError) as exc:
        # RLS deliberately makes an absent row and another scope indistinguishable.
        raise HTTPException(status_code=404, detail="canonical revision not found") from exc
    except TeamDatabaseUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "DATABASE_UNAVAILABLE", "message": str(exc)},
        ) from exc
    except TeamSchemaMismatch as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "SCHEMA_NOT_READY", "message": str(exc)},
        ) from exc
    except TeamIntegrityError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "CANONICAL_INTEGRITY_ERROR", "message": str(exc)},
        ) from exc
    except TeamDatabaseError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "TEAM_DATABASE_ERROR", "message": str(exc)},
        ) from exc

    encoded = jsonable_encoder(state)
    if isinstance(encoded, Mapping):
        result = dict(encoded)
    else:
        result = {"canonical": encoded}
    result["scope"] = {"tenant_id": scope.tenant_id, "container_id": scope.container_id}
    result["authorization"] = {
        "subject_id": identity.subject_id,
        "membership_role": getattr(grant, "role", None),
        "jwt_role_is_authoritative": False,
    }
    try:
        result["summary"] = _summary(result)
    except TeamIntegrityError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "CANONICAL_INTEGRITY_ERROR", "message": str(exc)},
        ) from exc
    objects = _rows(result.get("objects"))
    memberships = _rows(result.get("memberships"))
    result["object_types"] = sorted(
        {str(item.get("object_type")) for item in objects if item.get("object_type")}
    )
    needle = q.strip().casefold()
    checked_type = object_type.strip()
    if collection == "objects":
        filtered = [
            item
            for item in objects
            if (not checked_type or str(item.get("object_type", "")) == checked_type)
            and (
                not needle
                or needle
                in " ".join(
                    (
                        str(item.get("logical_id", "")),
                        str(item.get("object_version_id", "")),
                        str(item.get("object_type", "")),
                        str(item.get("payload_json", "")),
                    )
                ).casefold()
            )
        ]
    else:
        filtered = memberships
    snapshot_marker = {
        "collection": collection,
        "selected_head": result.get("selected_head", head),
        "revision": result.get("revision"),
        "query": needle,
        "object_type": checked_type,
    }
    try:
        page = paginate(
            filtered,
            limit=limit,
            cursor=cursor,
            snapshot_items=[snapshot_marker, *filtered],
        )
    except PaginationCursorError as exc:
        raise _page_error(exc) from exc
    result["objects"] = page.items if collection == "objects" else []
    result["memberships"] = page.items if collection == "memberships" else []
    result["pagination"] = {
        "collection": collection,
        "total": page.total,
        "limit": limit,
        "offset": page.offset,
        "page_cursor": page.page_cursor,
        "previous_cursor": page.previous_cursor,
        "next_cursor": page.next_cursor,
        "snapshot_etag": page.snapshot_etag,
    }
    # Ensure every payload remains valid JSON at the HTTP boundary even when a repository
    # implementation returns canonical JSON strings for identity preservation.
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    return result
