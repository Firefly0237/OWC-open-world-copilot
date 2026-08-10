"""Opaque ownership and immutable byte-artifact identity contracts."""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Self
from uuid import UUID

from pydantic import field_validator

from ._base import _StrictFrozenModel

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", flags=re.ASCII)
_MIME_RE = re.compile(
    r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+\Z",
    flags=re.ASCII,
)


def _require_string(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be a string")
    return value


def _validate_uuid(value: object) -> str:
    """Accept only the canonical, non-nil textual representation of a UUID."""

    text = _require_string(value, field_name="UUID")
    try:
        parsed = UUID(text)
    except ValueError as exc:
        raise ValueError("must be a canonical UUID string") from exc
    if parsed.int == 0 or str(parsed) != text:
        raise ValueError("must be a canonical non-nil UUID string")
    return text


def _require_bytes(content: bytes) -> None:
    if not isinstance(content, bytes):
        raise TypeError("content must be bytes")


class OwnershipScope(_StrictFrozenModel):
    """The opaque tenant/container pair that defines an isolation boundary."""

    tenant_id: str
    container_id: str

    @field_validator("tenant_id", "container_id", mode="before")
    @classmethod
    def validate_uuid_fields(cls, value: object) -> str:
        return _validate_uuid(value)


class ArtifactIdentity(_StrictFrozenModel):
    """Stable identity for an artifact's exact immutable bytes."""

    artifact_id: str
    content_hash: str
    mime_type: str

    @field_validator("artifact_id", mode="before")
    @classmethod
    def validate_artifact_id(cls, value: object) -> str:
        return _validate_uuid(value)

    @field_validator("content_hash", mode="before")
    @classmethod
    def validate_content_hash(cls, value: object) -> str:
        text = _require_string(value, field_name="content_hash")
        if _SHA256_RE.fullmatch(text) is None:
            raise ValueError("content_hash must be a lowercase SHA-256 digest")
        return text

    @field_validator("mime_type", mode="before")
    @classmethod
    def validate_mime_type(cls, value: object) -> str:
        text = _require_string(value, field_name="mime_type")
        if _MIME_RE.fullmatch(text) is None:
            raise ValueError("mime_type must be a lowercase ASCII type/subtype without parameters")
        return text

    @classmethod
    def from_bytes(
        cls,
        content: bytes,
        *,
        artifact_id: str,
        mime_type: str,
    ) -> Self:
        """Build an identity from the exact bytes supplied by the trusted ingestion boundary."""

        _require_bytes(content)
        return cls(
            artifact_id=artifact_id,
            content_hash=hashlib.sha256(content).hexdigest(),
            mime_type=mime_type,
        )

    def verify_bytes(self, content: bytes) -> bool:
        """Compare supplied bytes to this identity without data-dependent string comparison."""

        _require_bytes(content)
        observed = hashlib.sha256(content).hexdigest()
        return hmac.compare_digest(self.content_hash, observed)


__all__ = ["ArtifactIdentity", "OwnershipScope"]
