"""Deterministic JSON and domain-separated hashes for immutable contracts.

This module intentionally does not reuse :mod:`owcopilot.content.hash`.  That
legacy helper has a different compatibility contract (including ASCII escaped
JSON and permissive Python-to-JSON conversions).  Revision identities need a
smaller, explicit input language and stable domain separation.

Numbers deliberately follow Python's JSON classes: integers retain arbitrary
precision, while floats use IEEE-754 binary64.  Consequently ``1`` and ``1.0``
remain different canonical values, signed zero is preserved, and ordinary
binary64 rounding/underflow occurs while NaN and infinities are rejected.
Rendering is the frozen Python-compatible ``json.dumps`` form configured by
``canonical_json`` below; it is not claimed to be an external JSON
canonicalization standard, and spellings such as ``1e-07`` are identity bytes.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from typing import Final, TypeAlias, cast

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

CONTENT_PAYLOAD_DOMAIN: Final = "owcopilot/content-object-payload/v1"
OBJECT_VERSION_DOMAIN: Final = "owcopilot/content-object-version-id/v1"
REVISION_MANIFEST_DOMAIN: Final = "owcopilot/revision-manifest/v1"
CONTENT_REVISION_DOMAIN: Final = "owcopilot/content-revision-id/v1"


def _require_utf8_text(value: str, *, label: str) -> None:
    """Reject lone surrogates without changing Unicode normalization."""

    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} contains a lone Unicode surrogate") from exc


def _validate_json_value(
    value: object,
    *,
    path: str = "$",
    active_containers: set[int] | None = None,
) -> None:
    """Validate the exact JSON data model, without Python coercions."""

    if active_containers is None:
        active_containers = set()
    value_type = type(value)
    if value is None or value_type is bool or value_type is int:
        return
    if value_type is float:
        if not math.isfinite(cast(float, value)):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if value_type is str:
        _require_utf8_text(cast(str, value), label=path)
        return
    if value_type is list:
        container_id = id(value)
        if container_id in active_containers:
            raise ValueError(f"{path} contains a cyclic JSON container")
        active_containers.add(container_id)
        try:
            for index, item in enumerate(cast(list[object], value)):
                _validate_json_value(
                    item,
                    path=f"{path}[{index}]",
                    active_containers=active_containers,
                )
        finally:
            active_containers.remove(container_id)
        return
    if value_type is dict:
        container_id = id(value)
        if container_id in active_containers:
            raise ValueError(f"{path} contains a cyclic JSON container")
        active_containers.add(container_id)
        try:
            for key, item in cast(dict[object, object], value).items():
                if type(key) is not str:
                    raise TypeError(f"{path} contains a non-string object key")
                _require_utf8_text(key, label=f"{path} object key")
                _validate_json_value(
                    item,
                    path=f"{path}.{key}",
                    active_containers=active_containers,
                )
        finally:
            active_containers.remove(container_id)
        return
    raise TypeError(f"{path} contains unsupported JSON value type {value_type.__name__}")


def canonical_json(value: object) -> str:
    """Return canonical UTF-8 JSON for a value in the strict JSON data model.

    Object keys are sorted, whitespace is removed, and array order remains
    significant.  Unicode is emitted as supplied: NFC and NFD inputs therefore
    remain different identities.
    """

    _validate_json_value(value)
    result = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    _require_utf8_text(result, label="canonical JSON")
    return result


def _reject_duplicate_pairs(pairs: Iterable[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_constant(token: str) -> object:
    raise ValueError(f"non-finite JSON number: {token}")


def _parse_finite_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError(f"non-finite JSON number: {token}")
    return value


def canonical_json_from_text(source: str) -> str:
    """Parse JSON text under the strict contract and return canonical JSON."""

    if type(source) is not str:
        raise TypeError("JSON source must be a string")
    _require_utf8_text(source, label="JSON source")
    try:
        value = json.loads(
            source,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite_constant,
            parse_float=_parse_finite_float,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc.msg}") from exc
    return canonical_json(value)


def require_canonical_json(source: str) -> None:
    """Require that text is already in this module's canonical form."""

    if canonical_json_from_text(source) != source:
        raise ValueError("payload_json must already be canonical JSON")


def domain_digest(domain: str, canonical_payload: str) -> str:
    """Hash a canonical JSON payload in an unambiguous ASCII domain."""

    if type(domain) is not str or not domain or "\0" in domain:
        raise ValueError("hash domain must be a non-empty NUL-free string")
    try:
        domain_bytes = domain.encode("ascii", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("hash domain must be ASCII") from exc
    require_canonical_json(canonical_payload)
    payload_bytes = canonical_payload.encode("utf-8", errors="strict")
    return hashlib.sha256(domain_bytes + b"\0" + payload_bytes).hexdigest()


def content_payload_hash(payload_json: str) -> str:
    """Return the immutable content hash for canonical payload JSON."""

    return domain_digest(CONTENT_PAYLOAD_DOMAIN, payload_json)


def object_version_id(*, logical_id: str, object_type: str, content_hash: str) -> str:
    """Derive an object-version identifier from its stable identity envelope."""

    envelope = canonical_json(
        {
            "content_hash": content_hash,
            "logical_id": logical_id,
            "object_type": object_type,
        }
    )
    return f"objv_{domain_digest(OBJECT_VERSION_DOMAIN, envelope)}"


def manifest_hash(manifest_json: str) -> str:
    """Return the digest for a complete, canonical revision manifest."""

    return domain_digest(REVISION_MANIFEST_DOMAIN, manifest_json)


def revision_id(*, parent_revision: str | None, manifest_hash_value: str) -> str:
    """Derive a revision identifier; creation metadata is deliberately absent."""

    envelope = canonical_json(
        {
            "manifest_hash": manifest_hash_value,
            "parent_revision": parent_revision,
        }
    )
    return f"rev_{domain_digest(CONTENT_REVISION_DOMAIN, envelope)}"
