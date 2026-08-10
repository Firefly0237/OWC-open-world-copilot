"""Immutable content-object, revision-manifest, and release-pointer contracts."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Self

from pydantic import TypeAdapter, ValidationInfo, field_validator, model_validator

from ._base import _StrictFrozenModel
from .hashing import (
    canonical_json,
    canonical_json_from_text,
    content_payload_hash,
    require_canonical_json,
)
from .hashing import (
    manifest_hash as _derive_manifest_hash,
)
from .hashing import (
    object_version_id as _derive_object_version_id,
)
from .hashing import (
    revision_id as _derive_revision_id,
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", flags=re.ASCII)
_OBJECT_VERSION_RE = re.compile(r"objv_[0-9a-f]{64}\Z", flags=re.ASCII)
_REVISION_RE = re.compile(r"rev_[0-9a-f]{64}\Z", flags=re.ASCII)
_RFC3339_UTC_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|\+00:00)\Z",
    flags=re.ASCII,
)
_STRICT_JSON_DATETIME = TypeAdapter(datetime)


def _validate_identity_text(value: str, *, label: str) -> str:
    if not value:
        raise ValueError(f"{label} must not be empty")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} contains a lone Unicode surrogate") from exc
    return value


def _require_exact_string(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a string without coercion")
    return value


def _require_optional_exact_string(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _require_exact_string(value, label=label)


def _validate_sha256(value: str, *, label: str) -> str:
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _validate_object_version_id(value: str) -> str:
    if _OBJECT_VERSION_RE.fullmatch(value) is None:
        raise ValueError("object_version_id must have the form objv_<lowercase SHA-256>")
    return value


def _validate_revision_id(value: str, *, label: str = "revision_id") -> str:
    if _REVISION_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must have the form rev_<lowercase SHA-256>")
    return value


class ContentObjectVersion(_StrictFrozenModel):
    """A logical object's immutable, content-addressed payload version."""

    logical_id: str
    object_version_id: str
    object_type: str
    payload_json: str
    content_hash: str

    @field_validator(
        "logical_id",
        "object_version_id",
        "object_type",
        "payload_json",
        "content_hash",
        mode="before",
    )
    @classmethod
    def require_exact_string_fields(cls, value: object, info: ValidationInfo) -> str:
        return _require_exact_string(value, label=info.field_name or "string field")

    @field_validator("logical_id", "object_type")
    @classmethod
    def validate_identity_fields(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "identity")
        return _validate_identity_text(value, label=field_name)

    @field_validator("object_version_id")
    @classmethod
    def validate_object_version_id(cls, value: str) -> str:
        return _validate_object_version_id(value)

    @field_validator("content_hash")
    @classmethod
    def validate_content_hash(cls, value: str) -> str:
        return _validate_sha256(value, label="content_hash")

    @model_validator(mode="after")
    def validate_derived_identity(self) -> Self:
        require_canonical_json(self.payload_json)
        expected_hash = content_payload_hash(self.payload_json)
        if self.content_hash != expected_hash:
            raise ValueError("content_hash does not match payload_json")
        expected_id = _derive_object_version_id(
            logical_id=self.logical_id,
            object_type=self.object_type,
            content_hash=self.content_hash,
        )
        if self.object_version_id != expected_id:
            raise ValueError("object_version_id does not match its identity envelope")
        return self

    @classmethod
    def from_payload(
        cls,
        *,
        logical_id: str,
        object_type: str,
        payload: object,
    ) -> Self:
        """Create a version while retaining only immutable canonical JSON."""

        payload_json = canonical_json(payload)
        content_hash = content_payload_hash(payload_json)
        version_id = _derive_object_version_id(
            logical_id=logical_id,
            object_type=object_type,
            content_hash=content_hash,
        )
        return cls(
            logical_id=logical_id,
            object_version_id=version_id,
            object_type=object_type,
            payload_json=payload_json,
            content_hash=content_hash,
        )

    @classmethod
    def from_json(
        cls,
        *,
        logical_id: str,
        object_type: str,
        payload_json: str,
    ) -> Self:
        """Create a version from untrusted JSON text after strict canonical parsing."""

        canonical_payload = canonical_json_from_text(payload_json)
        content_hash = content_payload_hash(canonical_payload)
        version_id = _derive_object_version_id(
            logical_id=logical_id,
            object_type=object_type,
            content_hash=content_hash,
        )
        return cls(
            logical_id=logical_id,
            object_version_id=version_id,
            object_type=object_type,
            payload_json=canonical_payload,
            content_hash=content_hash,
        )


class RevisionMembership(_StrictFrozenModel):
    """One complete-manifest entry: either a live version or a tombstone."""

    logical_id: str
    object_version_id: str | None
    tombstone: bool

    @field_validator("logical_id", mode="before")
    @classmethod
    def require_exact_logical_id(cls, value: object) -> str:
        return _require_exact_string(value, label="logical_id")

    @field_validator("object_version_id", mode="before")
    @classmethod
    def require_exact_optional_object_version_id(cls, value: object) -> str | None:
        return _require_optional_exact_string(value, label="object_version_id")

    @field_validator("tombstone", mode="before")
    @classmethod
    def require_exact_tombstone_bool(cls, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError("tombstone must be a boolean without coercion")
        return value

    @field_validator("logical_id")
    @classmethod
    def validate_logical_id(cls, value: str) -> str:
        return _validate_identity_text(value, label="logical_id")

    @field_validator("object_version_id")
    @classmethod
    def validate_optional_object_version_id(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_object_version_id(value)
        return value

    @model_validator(mode="after")
    def validate_live_delete_xor(self) -> Self:
        is_live = self.object_version_id is not None and not self.tombstone
        is_delete = self.object_version_id is None and self.tombstone
        if not (is_live or is_delete):
            raise ValueError(
                "membership must be exactly one of live(object_version_id, tombstone=false) "
                "or delete(object_version_id=null, tombstone=true)"
            )
        return self

    @classmethod
    def live(cls, *, logical_id: str, object_version_id: str) -> Self:
        return cls(
            logical_id=logical_id,
            object_version_id=object_version_id,
            tombstone=False,
        )

    @classmethod
    def deleted(cls, *, logical_id: str) -> Self:
        return cls(logical_id=logical_id, object_version_id=None, tombstone=True)


def canonical_manifest(memberships: Iterable[RevisionMembership]) -> str:
    """Serialize a complete manifest sorted by logical ID, rejecting duplicates."""

    validated: list[RevisionMembership] = []
    for membership in memberships:
        if not isinstance(membership, RevisionMembership):
            raise TypeError("manifest entries must be RevisionMembership instances")
        validated.append(RevisionMembership.model_validate(membership))

    ordered = sorted(validated, key=lambda item: item.logical_id)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if previous.logical_id == current.logical_id:
            raise ValueError(f"duplicate manifest logical_id: {current.logical_id!r}")

    payload = [entry.model_dump(mode="json") for entry in ordered]
    return canonical_json(payload)


def revision_manifest_hash(memberships: Iterable[RevisionMembership]) -> str:
    """Hash the complete canonical membership set."""

    return _derive_manifest_hash(canonical_manifest(memberships))


class ContentRevision(_StrictFrozenModel):
    """Immutable revision identity plus non-identifying creation metadata."""

    revision_id: str
    parent_revision: str | None
    manifest_hash: str
    created_at: datetime
    created_by: str

    @field_validator("revision_id", "manifest_hash", "created_by", mode="before")
    @classmethod
    def require_exact_string_fields(cls, value: object, info: ValidationInfo) -> str:
        return _require_exact_string(value, label=info.field_name or "string field")

    @field_validator("parent_revision", mode="before")
    @classmethod
    def require_exact_optional_parent_revision(cls, value: object) -> str | None:
        return _require_optional_exact_string(value, label="parent_revision")

    @field_validator("created_at", mode="before")
    @classmethod
    def require_exact_created_at_input(
        cls,
        value: object,
        info: ValidationInfo,
    ) -> datetime | str:
        if info.mode == "json":
            source = _require_exact_string(value, label="created_at JSON value")
            if _RFC3339_UTC_RE.fullmatch(source) is None:
                raise ValueError(
                    "created_at JSON value must be an RFC 3339 UTC datetime using Z or +00:00"
                )
            # A JSON string is datetime's native serialized representation.  Parse it
            # in strict JSON mode here so the following strict Python node receives a
            # datetime, while numeric timestamps remain impossible.
            return _STRICT_JSON_DATETIME.validate_json(canonical_json(source), strict=True)
        if type(value) is not datetime:
            raise ValueError("created_at must be a datetime without coercion")
        return value

    @field_validator("revision_id")
    @classmethod
    def validate_revision_id(cls, value: str) -> str:
        return _validate_revision_id(value)

    @field_validator("parent_revision")
    @classmethod
    def validate_parent_revision(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_revision_id(value, label="parent_revision")
        return value

    @field_validator("manifest_hash")
    @classmethod
    def validate_manifest_hash(cls, value: str) -> str:
        return _validate_sha256(value, label="manifest_hash")

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ValueError("created_at must be timezone-aware UTC")
        if offset != timedelta(0):
            raise ValueError("created_at must be UTC")
        return value

    @field_validator("created_by")
    @classmethod
    def validate_created_by(cls, value: str) -> str:
        return _validate_identity_text(value, label="created_by")

    @model_validator(mode="after")
    def validate_derived_identity(self) -> Self:
        expected = _derive_revision_id(
            parent_revision=self.parent_revision,
            manifest_hash_value=self.manifest_hash,
        )
        if self.revision_id != expected:
            raise ValueError("revision_id does not match parent_revision and manifest_hash")
        return self

    @classmethod
    def from_memberships(
        cls,
        *,
        parent_revision: str | None,
        memberships: Iterable[RevisionMembership],
        created_at: datetime,
        created_by: str,
    ) -> Self:
        """Create a revision from its complete membership manifest."""

        manifest_hash_value = revision_manifest_hash(memberships)
        revision_id_value = _derive_revision_id(
            parent_revision=parent_revision,
            manifest_hash_value=manifest_hash_value,
        )
        return cls(
            revision_id=revision_id_value,
            parent_revision=parent_revision,
            manifest_hash=manifest_hash_value,
            created_at=created_at,
            created_by=created_by,
        )


class ReleasePointer(_StrictFrozenModel):
    """Separate mutable heads for authoring and externally served content."""

    authoring_head: str | None
    serving_head: str | None

    @field_validator("authoring_head", "serving_head", mode="before")
    @classmethod
    def require_exact_optional_heads(
        cls,
        value: object,
        info: ValidationInfo,
    ) -> str | None:
        return _require_optional_exact_string(value, label=info.field_name or "revision head")

    @field_validator("authoring_head", "serving_head")
    @classmethod
    def validate_heads(cls, value: str | None, info: object) -> str | None:
        if value is not None:
            field_name = getattr(info, "field_name", "revision head")
            _validate_revision_id(value, label=field_name)
        return value

    @model_validator(mode="after")
    def forbid_orphan_serving_head(self) -> Self:
        if self.serving_head is not None and self.authoring_head is None:
            raise ValueError("serving_head cannot exist without an authoring_head")
        return self


__all__ = [
    "ContentObjectVersion",
    "ContentRevision",
    "ReleasePointer",
    "RevisionMembership",
    "canonical_manifest",
    "revision_manifest_hash",
]
