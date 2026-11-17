"""N1.1 identity-contract invariants and byte-level golden vectors."""

from __future__ import annotations

import inspect
import json
from datetime import datetime
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError, ValidationInfo, field_validator

from owcopilot.contracts._base import _StrictFrozenModel
from owcopilot.contracts.identity import ArtifactIdentity, OwnershipScope

_TENANT_ID = "123e4567-e89b-12d3-a456-426614174000"
_CONTAINER_ID = "123e4567-e89b-12d3-a456-426614174001"
_ARTIFACT_ID = "123e4567-e89b-12d3-a456-426614174002"
_GOLDEN_BYTES = "策划原件\r\n".encode("utf-8")  # noqa: UP012 - encoding is part of vector
_GOLDEN_SHA256 = "c62fce414db9a8db77499169051b5ba00d4667be1586fb99139f9fcf49e4e55a"


class _StrictProbe(_StrictFrozenModel):
    count: int
    label: str
    observed_at: datetime

    @field_validator("observed_at", mode="before")
    @classmethod
    def validate_observed_at(cls, value: object, info: ValidationInfo) -> datetime:
        if info.mode == "json":
            if type(value) is not str:
                raise ValueError("JSON observed_at must be a string")
            text = value[:-1] + "+00:00" if value.endswith("Z") else value
            return datetime.fromisoformat(text)
        if type(value) is not datetime:
            raise ValueError("Python observed_at must be a datetime without coercion")
        return value


def _scope() -> OwnershipScope:
    return OwnershipScope(tenant_id=_TENANT_ID, container_id=_CONTAINER_ID)


def _artifact() -> ArtifactIdentity:
    return ArtifactIdentity.from_bytes(
        _GOLDEN_BYTES,
        artifact_id=_ARTIFACT_ID,
        mime_type="text/plain",
    )


def test_ownership_scope_round_trips_without_coercion() -> None:
    scope = _scope()
    assert OwnershipScope.model_validate_json(scope.model_dump_json()) == scope
    assert scope.model_dump() == {
        "tenant_id": _TENANT_ID,
        "container_id": _CONTAINER_ID,
    }


@pytest.mark.parametrize(
    "hostile",
    [
        "00000000-0000-0000-0000-000000000000",
        "123E4567-E89B-12D3-A456-426614174000",
        "urn:uuid:123e4567-e89b-12d3-a456-426614174000",
        "{123e4567-e89b-12d3-a456-426614174000}",
        " 123e4567-e89b-12d3-a456-426614174000",
        "123e4567-e89b-12d3-a456-426614174000 ",
        "123e4567e89b12d3a456426614174000",
        "not-a-uuid",
        "",
    ],
)
def test_uuid_fields_reject_noncanonical_or_nil_text(hostile: str) -> None:
    with pytest.raises(ValidationError):
        OwnershipScope(tenant_id=hostile, container_id=_CONTAINER_ID)
    with pytest.raises(ValidationError):
        ArtifactIdentity(
            artifact_id=hostile,
            content_hash=_GOLDEN_SHA256,
            mime_type="text/plain",
        )


@pytest.mark.parametrize(
    "hostile",
    [1, UUID(_TENANT_ID), b"123e4567-e89b-12d3-a456-426614174000"],
)
def test_uuid_fields_are_strict_strings(hostile: object) -> None:
    with pytest.raises(ValidationError):
        OwnershipScope(tenant_id=hostile, container_id=_CONTAINER_ID)  # type: ignore[arg-type]


def test_uuid_contract_does_not_artificially_require_version_four() -> None:
    version_one = "d9428888-122b-11e1-b85c-61cd3cbb3210"
    assert (
        OwnershipScope(tenant_id=version_one, container_id=_CONTAINER_ID).tenant_id == version_one
    )


@pytest.mark.parametrize(
    "content_hash",
    ["A" * 64, "a" * 63, "a" * 65, "g" * 64, " a" * 32, b"a" * 64],
)
def test_content_hash_is_exact_lowercase_sha256_text(content_hash: object) -> None:
    with pytest.raises(ValidationError):
        ArtifactIdentity(
            artifact_id=_ARTIFACT_ID,
            content_hash=content_hash,  # type: ignore[arg-type]
            mime_type="application/octet-stream",
        )


@pytest.mark.parametrize(
    "mime_type",
    [
        "Text/Plain",
        "text/PLAIN",
        "text/plain; charset=utf-8",
        "*/*",
        "text/*",
        "text/pl*ain",
        "text/pl%ain",
        "text/pl'ain",
        "text/pl`ain",
        "text/pl|ain",
        "text/pl~ain",
        "text",
        "/plain",
        "text/",
        " text/plain",
        "text/plain ",
        "tèxt/plain",
        b"text/plain",
    ],
)
def test_mime_type_is_lowercase_ascii_parameter_free_and_concrete(mime_type: object) -> None:
    with pytest.raises(ValidationError):
        ArtifactIdentity(
            artifact_id=_ARTIFACT_ID,
            content_hash=_GOLDEN_SHA256,
            mime_type=mime_type,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "mime_type",
    ["text/plain", "application/json", "application/vnd.owcopilot+json", "image/svg+xml"],
)
def test_registered_and_vendor_mime_shapes_are_accepted(mime_type: str) -> None:
    assert _artifact().model_copy(update={"mime_type": mime_type}).mime_type == mime_type


def test_model_validate_strict_override_cannot_coerce_identity_fields() -> None:
    with pytest.raises(ValueError, match="strict=False"):
        OwnershipScope.model_validate(
            {"tenant_id": _TENANT_ID.encode(), "container_id": _CONTAINER_ID},
            strict=False,
        )


def test_type_adapter_lax_override_cannot_coerce_identity_fields() -> None:
    scope_adapter = TypeAdapter(OwnershipScope)
    with pytest.raises(ValidationError, match="UUID must be a string"):
        scope_adapter.validate_python(
            {"tenant_id": _TENANT_ID.encode(), "container_id": _CONTAINER_ID},
            strict=False,
        )

    artifact_adapter = TypeAdapter(ArtifactIdentity)
    base = {
        "artifact_id": _ARTIFACT_ID,
        "content_hash": _GOLDEN_SHA256,
        "mime_type": "text/plain",
    }
    with pytest.raises(ValidationError, match="content_hash must be a string"):
        artifact_adapter.validate_python(
            {**base, "content_hash": _GOLDEN_SHA256.encode()}, strict=False
        )
    with pytest.raises(ValidationError, match="mime_type must be a string"):
        artifact_adapter.validate_python({**base, "mime_type": b"text/plain"}, strict=False)
    with pytest.raises(ValueError, match="strict=False"):
        ArtifactIdentity.model_validate(
            {
                "artifact_id": _ARTIFACT_ID,
                "content_hash": _GOLDEN_SHA256.encode(),
                "mime_type": "text/plain",
            },
            strict=False,
        )
    with pytest.raises(ValueError, match="strict=False"):
        ArtifactIdentity.model_validate(
            {
                "artifact_id": _ARTIFACT_ID,
                "content_hash": _GOLDEN_SHA256,
                "mime_type": b"text/plain",
            },
            strict=False,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"count": True, "label": "ok", "observed_at": datetime(2026, 8, 3)},
        {"count": 1, "label": b"bytes", "observed_at": datetime(2026, 8, 3)},
        {"count": 1, "label": 7, "observed_at": datetime(2026, 8, 3)},
        {"count": 1, "label": "ok", "observed_at": "2026-08-03T12:00:00Z"},
    ],
)
def test_python_validation_cannot_request_lax_scalar_coercion(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="strict=False"):
        _StrictProbe.model_validate(payload, strict=False)
    with pytest.raises(ValidationError):
        _StrictProbe.model_validate(payload)


def test_json_validation_rejects_lax_override_but_keeps_native_datetime_round_trip() -> None:
    hostile_json = json.dumps({"count": 1, "label": 7, "observed_at": "2026-08-03T12:00:00Z"})
    with pytest.raises(ValueError, match="strict=False"):
        _StrictProbe.model_validate_json(hostile_json, strict=False)
    with pytest.raises(ValidationError):
        _StrictProbe.model_validate_json(hostile_json)

    probe = _StrictProbe(
        count=1,
        label="ok",
        observed_at=datetime.fromisoformat("2026-08-03T12:00:00+00:00"),
    )
    assert _StrictProbe.model_validate_json(probe.model_dump_json()) == probe


@pytest.mark.parametrize("extra", ["allow", "ignore"])
def test_validation_cannot_override_extra_forbid(extra: str) -> None:
    payload = {
        "tenant_id": _TENANT_ID,
        "container_id": _CONTAINER_ID,
        "version": "work__0",
    }
    with pytest.raises(ValueError, match="extra inputs"):
        OwnershipScope.model_validate(payload, extra=extra)  # type: ignore[arg-type]


def test_type_adapter_cannot_override_extra_forbid_for_python_or_json() -> None:
    payload = {
        "tenant_id": _TENANT_ID,
        "container_id": _CONTAINER_ID,
        "version": "work__0",
    }
    adapter = TypeAdapter(OwnershipScope)
    extra_override_available = all(
        "extra" in inspect.signature(method).parameters
        for method in (adapter.validate_python, adapter.validate_json)
    )
    if not extra_override_available:
        pytest.skip("installed Pydantic has no TypeAdapter extra override surface")
    with pytest.raises(ValidationError, match="extra inputs are forbidden"):
        adapter.validate_python(payload, extra="allow")
    with pytest.raises(ValidationError, match="extra inputs are forbidden"):
        adapter.validate_json(json.dumps(payload), extra="allow")


def test_attribute_carriers_are_rejected_by_direct_and_type_adapter_validation() -> None:
    class AttributeCarrier:
        tenant_id = _TENANT_ID
        container_id = _CONTAINER_ID

    carrier = AttributeCarrier()
    with pytest.raises(ValueError, match="from_attributes=True"):
        OwnershipScope.model_validate(carrier, from_attributes=True)
    with pytest.raises(ValidationError, match="input must be a mapping"):
        TypeAdapter(OwnershipScope).validate_python(carrier, from_attributes=True)


def test_alias_validation_overrides_are_forbidden() -> None:
    with pytest.raises(ValueError, match="alias validation overrides"):
        OwnershipScope.model_validate(
            {"tenant_id": _TENANT_ID, "container_id": _CONTAINER_ID},
            by_alias=True,
        )


def test_coercive_string_validation_entrypoint_is_forbidden() -> None:
    with pytest.raises(ValueError, match="model_validate_strings"):
        OwnershipScope.model_validate_strings(
            {"tenant_id": _TENANT_ID, "container_id": _CONTAINER_ID}
        )


def test_contracts_forbid_extra_fields_and_are_frozen() -> None:
    with pytest.raises(ValidationError, match="extra inputs are forbidden"):
        OwnershipScope.model_validate(
            {"tenant_id": _TENANT_ID, "container_id": _CONTAINER_ID, "world": "work__0"}
        )

    scope = _scope()
    with pytest.raises(ValidationError, match="frozen"):
        scope.tenant_id = _CONTAINER_ID
    assert scope.tenant_id == _TENANT_ID


def test_model_copy_update_revalidates_every_field() -> None:
    scope = _scope()
    with pytest.raises(ValidationError):
        scope.model_copy(update={"tenant_id": 1})
    with pytest.raises(ValidationError):
        scope.model_copy(update={"container_id": "00000000-0000-0000-0000-000000000000"})
    with pytest.raises(ValidationError, match="extra inputs are forbidden"):
        scope.model_copy(update={"version": "work__0"})
    assert scope.model_copy(update={"tenant_id": _ARTIFACT_ID}) == OwnershipScope(
        tenant_id=_ARTIFACT_ID,
        container_id=_CONTAINER_ID,
    )


def test_deprecated_copy_cannot_bypass_validation_or_make_partial_contracts() -> None:
    scope = _scope()
    with pytest.raises(ValidationError):
        scope.copy(update={"tenant_id": 1})
    with pytest.raises(ValueError, match="partial copies"):
        scope.copy(include={"tenant_id"})
    assert scope.copy(update={"tenant_id": _ARTIFACT_ID}) == OwnershipScope(
        tenant_id=_ARTIFACT_ID,
        container_id=_CONTAINER_ID,
    )


def test_revalidation_rejects_a_constructed_invalid_instance() -> None:
    forged = OwnershipScope.model_construct(tenant_id="../tenant", container_id=_CONTAINER_ID)
    with pytest.raises(ValidationError):
        OwnershipScope.model_validate(forged)


def test_json_schemas_have_only_the_declared_identity_fields() -> None:
    ownership_schema = OwnershipScope.model_json_schema()
    artifact_schema = ArtifactIdentity.model_json_schema()
    assert ownership_schema["additionalProperties"] is False
    assert artifact_schema["additionalProperties"] is False
    assert set(ownership_schema["properties"]) == {"tenant_id", "container_id"}
    assert set(artifact_schema["properties"]) == {"artifact_id", "content_hash", "mime_type"}
    assert ownership_schema["required"] == ["tenant_id", "container_id"]
    assert artifact_schema["required"] == ["artifact_id", "content_hash", "mime_type"]


def test_artifact_identity_uses_the_frozen_utf8_crlf_golden_vector() -> None:
    artifact = _artifact()
    assert artifact.content_hash == _GOLDEN_SHA256
    assert artifact.verify_bytes(_GOLDEN_BYTES)
    assert ArtifactIdentity.model_validate_json(artifact.model_dump_json()) == artifact


def test_artifact_byte_verification_detects_one_byte_changes_and_empty_bytes() -> None:
    artifact = _artifact()
    changed = _GOLDEN_BYTES[:-1] + b"\x00"
    assert not artifact.verify_bytes(changed)
    assert not artifact.verify_bytes(b"")

    empty = ArtifactIdentity.from_bytes(
        b"",
        artifact_id=_ARTIFACT_ID,
        mime_type="application/octet-stream",
    )
    assert empty.content_hash == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert empty.verify_bytes(b"")
    assert not empty.verify_bytes(b"\x00")


@pytest.mark.parametrize("not_bytes", [bytearray(b"x"), memoryview(b"x"), "x"])
def test_byte_identity_helpers_require_immutable_bytes(not_bytes: object) -> None:
    with pytest.raises(TypeError, match="content must be bytes"):
        ArtifactIdentity.from_bytes(  # type: ignore[arg-type]
            not_bytes,
            artifact_id=_ARTIFACT_ID,
            mime_type="application/octet-stream",
        )
    with pytest.raises(TypeError, match="content must be bytes"):
        _artifact().verify_bytes(not_bytes)  # type: ignore[arg-type]
