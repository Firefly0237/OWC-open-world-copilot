from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import subprocess
import sys
import unicodedata
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from owcopilot.content.hash import content_hash as legacy_content_hash
from owcopilot.contracts.hashing import (
    CONTENT_PAYLOAD_DOMAIN,
    CONTENT_REVISION_DOMAIN,
    OBJECT_VERSION_DOMAIN,
    REVISION_MANIFEST_DOMAIN,
    canonical_json,
    canonical_json_from_text,
    content_payload_hash,
    domain_digest,
    revision_id,
)
from owcopilot.contracts.revisions import (
    ContentObjectVersion,
    ContentRevision,
    ReleasePointer,
    RevisionMembership,
    canonical_manifest,
    revision_manifest_hash,
)

GOLDEN_PAYLOAD = {
    "description": "守门人",
    "metadata": {"rank": 2},
    "name": "阿岚",
    "tags": ["守卫", "港口"],
}
GOLDEN_CANONICAL = (
    '{"description":"守门人","metadata":{"rank":2},"name":"阿岚","tags":["守卫","港口"]}'
)
GOLDEN_CONTENT_HASH = "3fdd1a5691dd236b6ad9fccdfd3ec3b07e976d85d2a26a559089426645533ba6"
GOLDEN_OBJECT_VERSION_ID = "objv_e02689a1e0951839666d379cc66da4a1d78bdf4d88419f30be68bdeb0e81e15d"
GOLDEN_MANIFEST = (
    '[{"logical_id":"npc_guard","object_version_id":"'
    f"{GOLDEN_OBJECT_VERSION_ID}"
    '","tombstone":false},{"logical_id":"quest_old","object_version_id":null,'
    '"tombstone":true}]'
)
GOLDEN_MANIFEST_HASH = "9bdf0fc11c83f5bb9368d23b4b00d88c2374aa23cd772a5d785c197dfcf3b077"
GOLDEN_REVISION_ID = "rev_81b6bfdea11f3e4644a58e845489f4e755cc9d19727e21c5ff4dbb53abd34d20"
CREATED_AT = datetime(2026, 8, 3, 12, 30, tzinfo=UTC)


def golden_object() -> ContentObjectVersion:
    return ContentObjectVersion.from_payload(
        logical_id="npc_guard",
        object_type="entity",
        payload=GOLDEN_PAYLOAD,
    )


def golden_memberships() -> list[RevisionMembership]:
    return [
        RevisionMembership.live(
            logical_id="npc_guard",
            object_version_id=GOLDEN_OBJECT_VERSION_ID,
        ),
        RevisionMembership.deleted(logical_id="quest_old"),
    ]


def golden_revision(**metadata: object) -> ContentRevision:
    values = {"created_at": CREATED_AT, "created_by": "author-1", **metadata}
    return ContentRevision.from_memberships(
        parent_revision=None,
        memberships=golden_memberships(),
        created_at=values["created_at"],  # type: ignore[arg-type]
        created_by=values["created_by"],  # type: ignore[arg-type]
    )


def test_golden_vectors_are_exact_and_independently_recomputed() -> None:
    content_envelope = GOLDEN_CANONICAL.encode("utf-8")
    assert canonical_json(GOLDEN_PAYLOAD) == GOLDEN_CANONICAL
    assert (
        hashlib.sha256(
            CONTENT_PAYLOAD_DOMAIN.encode("ascii") + b"\0" + content_envelope
        ).hexdigest()
        == GOLDEN_CONTENT_HASH
    )

    object_envelope = (
        '{"content_hash":"3fdd1a5691dd236b6ad9fccdfd3ec3b07e976d85d2a26a559089426645533ba6",'
        '"logical_id":"npc_guard","object_type":"entity"}'
    )
    assert (
        "objv_"
        + hashlib.sha256(
            OBJECT_VERSION_DOMAIN.encode("ascii") + b"\0" + object_envelope.encode("utf-8")
        ).hexdigest()
        == GOLDEN_OBJECT_VERSION_ID
    )
    assert (
        hashlib.sha256(
            REVISION_MANIFEST_DOMAIN.encode("ascii") + b"\0" + GOLDEN_MANIFEST.encode("utf-8")
        ).hexdigest()
        == GOLDEN_MANIFEST_HASH
    )
    revision_envelope = (
        '{"manifest_hash":"9bdf0fc11c83f5bb9368d23b4b00d88c2374aa23cd772a5d785c197dfcf3b077",'
        '"parent_revision":null}'
    )
    assert (
        "rev_"
        + hashlib.sha256(
            CONTENT_REVISION_DOMAIN.encode("ascii") + b"\0" + revision_envelope.encode("utf-8")
        ).hexdigest()
        == GOLDEN_REVISION_ID
    )

    version = golden_object()
    assert version.payload_json == GOLDEN_CANONICAL
    assert version.content_hash == GOLDEN_CONTENT_HASH
    assert version.object_version_id == GOLDEN_OBJECT_VERSION_ID
    assert canonical_manifest(golden_memberships()) == GOLDEN_MANIFEST
    assert revision_manifest_hash(golden_memberships()) == GOLDEN_MANIFEST_HASH
    assert golden_revision().revision_id == GOLDEN_REVISION_ID


def test_canonical_json_sorts_objects_but_preserves_array_order() -> None:
    first = {"z": 1, "nested": {"b": 2, "a": 1}, "items": ["first", "second"]}
    permuted = {"items": ["first", "second"], "nested": {"a": 1, "b": 2}, "z": 1}
    reversed_array = {"nested": {"b": 2, "a": 1}, "z": 1, "items": ["second", "first"]}

    assert canonical_json(first) == canonical_json(permuted)
    assert content_payload_hash(canonical_json(first)) == content_payload_hash(
        canonical_json(permuted)
    )
    assert canonical_json(first) != canonical_json(reversed_array)
    assert content_payload_hash(canonical_json(first)) != content_payload_hash(
        canonical_json(reversed_array)
    )


def test_property_all_payload_and_manifest_permutations_preserve_identity() -> None:
    """Deterministically exercise the ordering property, rather than one example."""

    for item_order in itertools.permutations(GOLDEN_PAYLOAD.items()):
        version = ContentObjectVersion.from_payload(
            logical_id="npc_guard",
            object_type="entity",
            payload=dict(item_order),
        )
        assert version.payload_json == GOLDEN_CANONICAL
        assert version.content_hash == GOLDEN_CONTENT_HASH
        assert version.object_version_id == GOLDEN_OBJECT_VERSION_ID

    nested_items = (("rank", 2), ("role", "guard"), ("active", True))
    canonical_nested_payload = {
        "metadata": dict(nested_items),
        "name": "阿岚",
        "tags": ["守卫", "港口"],
    }
    nested_baseline = ContentObjectVersion.from_payload(
        logical_id="npc_guard_nested",
        object_type="entity",
        payload=canonical_nested_payload,
    )
    for nested_order in itertools.permutations(nested_items):
        outer_items = (
            ("metadata", dict(nested_order)),
            ("name", "阿岚"),
            ("tags", ["守卫", "港口"]),
        )
        for outer_order in itertools.permutations(outer_items):
            version = ContentObjectVersion.from_payload(
                logical_id="npc_guard_nested",
                object_type="entity",
                payload=dict(outer_order),
            )
            assert version.payload_json == nested_baseline.payload_json
            assert version.content_hash == nested_baseline.content_hash
            assert version.object_version_id == nested_baseline.object_version_id

    memberships = [
        RevisionMembership.live(
            logical_id="npc_guard",
            object_version_id=GOLDEN_OBJECT_VERSION_ID,
        ),
        RevisionMembership.deleted(logical_id="quest_old"),
        RevisionMembership.deleted(logical_id="scene_old"),
        RevisionMembership.deleted(logical_id="world_old"),
    ]
    baseline_manifest = canonical_manifest(memberships)
    baseline_hash = revision_manifest_hash(memberships)
    for membership_order in itertools.permutations(memberships):
        assert canonical_manifest(membership_order) == baseline_manifest
        assert revision_manifest_hash(membership_order) == baseline_hash


@pytest.mark.parametrize(
    "source",
    [
        '{"a":1,"a":2}',
        '{"outer":{"same":1,"same":2}}',
    ],
)
def test_json_text_rejects_duplicate_object_keys(source: str) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        canonical_json_from_text(source)


@pytest.mark.parametrize("key", [1, False, 1.5, None])
def test_canonical_json_rejects_non_string_object_keys(key: object) -> None:
    with pytest.raises(TypeError, match="non-string"):
        canonical_json({key: "value"})


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_canonical_json_rejects_nonfinite_python_numbers(value: float) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json({"value": value})


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_json_text_rejects_nonfinite_numbers(token: str) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json_from_text(f'{{"value":{token}}}')


@pytest.mark.parametrize(
    "value",
    [
        {"value": "\ud800"},
        {"\udfff": "value"},
    ],
)
def test_canonical_json_rejects_lone_surrogates_in_memory(value: object) -> None:
    with pytest.raises(ValueError, match="surrogate"):
        canonical_json(value)


@pytest.mark.parametrize("source", ['{"value":"\\ud800"}', '{"\\udfff":"value"}'])
def test_json_text_rejects_escaped_lone_surrogates(source: str) -> None:
    with pytest.raises(ValueError, match="surrogate"):
        canonical_json_from_text(source)


@pytest.mark.parametrize("value", [(1, 2), {1, 2}, b"bytes"])
def test_canonical_json_rejects_python_only_container_types(value: object) -> None:
    with pytest.raises(TypeError, match="unsupported JSON"):
        canonical_json(value)


def test_canonical_json_rejects_cyclic_containers_but_allows_shared_values() -> None:
    cyclic_list: list[object] = []
    cyclic_list.append(cyclic_list)
    with pytest.raises(ValueError, match="cyclic JSON"):
        canonical_json(cyclic_list)

    cyclic_dict: dict[str, object] = {}
    cyclic_dict["self"] = cyclic_dict
    with pytest.raises(ValueError, match="cyclic JSON"):
        canonical_json(cyclic_dict)

    shared = {"rank": 2}
    assert canonical_json({"first": shared, "second": shared}) == (
        '{"first":{"rank":2},"second":{"rank":2}}'
    )


def test_unicode_normalization_is_not_silent_identity_normalization() -> None:
    nfc = unicodedata.normalize("NFC", "e\u0301")
    nfd = unicodedata.normalize("NFD", "é")
    assert nfc != nfd
    assert canonical_json({"name": nfc}) != canonical_json({"name": nfd})
    assert content_payload_hash(canonical_json({"name": nfc})) != content_payload_hash(
        canonical_json({"name": nfd})
    )


def test_binary64_float_semantics_are_explicit_and_stable() -> None:
    assert canonical_json_from_text("9007199254740993") == "9007199254740993"
    assert canonical_json_from_text("9007199254740992.0") == "9007199254740992.0"
    assert canonical_json_from_text("9007199254740993.0") == "9007199254740992.0"
    assert canonical_json_from_text("1e-999") == "0.0"

    assert canonical_json(-0.0) == "-0.0"
    assert canonical_json(0.0) == "0.0"
    assert canonical_json(1) == "1"
    assert canonical_json(1.0) == "1.0"
    assert canonical_json(1e-7) == "1e-07"
    assert content_payload_hash(canonical_json(1e-7)) == (
        "d2c0a3ac53d2d716061e3d55690c0a9fd31c91dd0a2aa46588ab450ae809365f"
    )
    assert (
        hashlib.sha256(CONTENT_PAYLOAD_DOMAIN.encode("ascii") + b"\0" + b"1e-07").hexdigest()
        == "d2c0a3ac53d2d716061e3d55690c0a9fd31c91dd0a2aa46588ab450ae809365f"
    )
    assert content_payload_hash(canonical_json(-0.0)) != content_payload_hash(canonical_json(0.0))
    assert content_payload_hash(canonical_json(1)) != content_payload_hash(canonical_json(1.0))


def test_domain_digest_rejects_ambiguous_or_non_ascii_domains_and_noncanonical_input() -> None:
    with pytest.raises(ValueError, match="NUL-free"):
        domain_digest("bad\0domain", "{}")
    with pytest.raises(ValueError, match="ASCII"):
        domain_digest("域", "{}")
    with pytest.raises(ValueError, match="canonical"):
        domain_digest("example/v1", '{"b": 2, "a": 1}')


def test_source_payload_mutation_cannot_change_an_existing_version() -> None:
    source = {"nested": {"rank": 1}, "tags": ["a", "b"]}
    version = ContentObjectVersion.from_payload(
        logical_id="mutable-source",
        object_type="entity",
        payload=source,
    )
    original = version.model_dump()

    source["nested"]["rank"] = 999  # type: ignore[index]
    source["tags"].append("c")  # type: ignore[union-attr]

    assert version.model_dump() == original
    assert "999" not in version.payload_json


def test_object_version_rejects_noncanonical_payload_and_all_identity_tampering() -> None:
    version = golden_object()
    values = version.model_dump()

    with pytest.raises(ValidationError, match="canonical"):
        ContentObjectVersion(**{**values, "payload_json": json.dumps(GOLDEN_PAYLOAD)})
    with pytest.raises(ValidationError, match="content_hash"):
        ContentObjectVersion(**{**values, "content_hash": "0" * 64})
    with pytest.raises(ValidationError, match="object_version_id"):
        ContentObjectVersion(**{**values, "object_version_id": "objv_" + "0" * 64})
    with pytest.raises(ValidationError, match="content_hash"):
        version.model_copy(update={"payload_json": canonical_json({"changed": True})})
    with pytest.raises(ValidationError, match="object_version_id"):
        version.model_copy(update={"logical_id": "different"})


def test_object_version_from_json_canonicalizes_valid_text_and_rejects_unsafe_text() -> None:
    source = json.dumps(GOLDEN_PAYLOAD, ensure_ascii=False, indent=2)
    assert (
        ContentObjectVersion.from_json(
            logical_id="npc_guard",
            object_type="entity",
            payload_json=source,
        )
        == golden_object()
    )

    unsafe_sources = [
        '{"same":1,"same":2}',
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":1e999}',
        '{"value":"\\ud800"}',
    ]
    for unsafe_source in unsafe_sources:
        with pytest.raises(ValueError):
            ContentObjectVersion.from_json(
                logical_id="unsafe",
                object_type="entity",
                payload_json=unsafe_source,
            )


@pytest.mark.parametrize(
    ("object_version_id", "tombstone", "is_valid"),
    [
        (GOLDEN_OBJECT_VERSION_ID, False, True),
        (None, True, True),
        (None, False, False),
        (GOLDEN_OBJECT_VERSION_ID, True, False),
    ],
)
def test_membership_requires_exact_live_or_delete_state(
    object_version_id: str | None,
    tombstone: bool,
    is_valid: bool,
) -> None:
    values = {
        "logical_id": "npc_guard",
        "object_version_id": object_version_id,
        "tombstone": tombstone,
    }
    if is_valid:
        RevisionMembership(**values)
    else:
        with pytest.raises(ValidationError, match="exactly one"):
            RevisionMembership(**values)


def test_manifest_is_permutation_stable_and_rejects_duplicate_logical_ids() -> None:
    memberships = golden_memberships()
    assert canonical_manifest(memberships) == canonical_manifest(reversed(memberships))
    assert revision_manifest_hash(memberships) == revision_manifest_hash(reversed(memberships))

    duplicate = RevisionMembership.deleted(logical_id="npc_guard")
    with pytest.raises(ValueError, match="duplicate manifest logical_id"):
        canonical_manifest([memberships[0], duplicate])


def test_manifest_revalidates_entries_and_rejects_untyped_inputs() -> None:
    with pytest.raises(TypeError, match="RevisionMembership"):
        canonical_manifest(  # type: ignore[list-item]
            [{"logical_id": "x", "object_version_id": None, "tombstone": True}]
        )
    live = golden_memberships()[0]
    with pytest.raises(ValidationError, match="exactly one"):
        live.model_copy(update={"tombstone": True})


def test_revision_identity_binds_parent_and_manifest_but_not_creation_metadata() -> None:
    first = golden_revision()
    different_metadata = golden_revision(
        created_at=datetime(2027, 1, 1, tzinfo=UTC),
        created_by="author-2",
    )
    assert first.revision_id == different_metadata.revision_id

    parent = "rev_" + "1" * 64
    with_parent = ContentRevision.from_memberships(
        parent_revision=parent,
        memberships=golden_memberships(),
        created_at=CREATED_AT,
        created_by="author-1",
    )
    assert with_parent.revision_id != first.revision_id
    assert with_parent.revision_id == revision_id(
        parent_revision=parent,
        manifest_hash_value=GOLDEN_MANIFEST_HASH,
    )


def test_revision_rejects_tamper_and_accepted_by_is_not_part_of_the_contract() -> None:
    revision = golden_revision()
    values = revision.model_dump()

    with pytest.raises(ValidationError, match="revision_id"):
        ContentRevision(**{**values, "manifest_hash": "0" * 64})
    with pytest.raises(ValidationError, match="revision_id"):
        revision.model_copy(update={"parent_revision": "rev_" + "2" * 64})
    with pytest.raises(ValidationError, match="extra inputs"):
        ContentRevision(**values, accepted_by="reviewer")


@pytest.mark.parametrize(
    "created_at",
    [
        datetime(2026, 8, 3, 12, 30),
        datetime(2026, 8, 3, 12, 30, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_revision_requires_timezone_aware_utc_creation_time(created_at: datetime) -> None:
    with pytest.raises(ValidationError, match="UTC"):
        golden_revision(created_at=created_at)


def test_release_pointer_separates_heads_and_forbids_orphan_serving() -> None:
    authoring = "rev_" + "1" * 64
    serving = "rev_" + "2" * 64
    assert ReleasePointer(authoring_head=None, serving_head=None).serving_head is None
    assert ReleasePointer(authoring_head=authoring, serving_head=None).authoring_head == authoring
    pointer = ReleasePointer(authoring_head=authoring, serving_head=serving)
    assert pointer.serving_head == serving

    with pytest.raises(ValidationError, match="cannot exist"):
        ReleasePointer(authoring_head=None, serving_head=serving)
    with pytest.raises(ValidationError, match="cannot exist"):
        pointer.model_copy(update={"authoring_head": None})


def test_contract_field_sets_are_exact_and_round_trip() -> None:
    assert set(ContentObjectVersion.model_fields) == {
        "logical_id",
        "object_version_id",
        "object_type",
        "payload_json",
        "content_hash",
    }
    assert set(RevisionMembership.model_fields) == {
        "logical_id",
        "object_version_id",
        "tombstone",
    }
    assert set(ContentRevision.model_fields) == {
        "revision_id",
        "parent_revision",
        "manifest_hash",
        "created_at",
        "created_by",
    }
    assert set(ReleasePointer.model_fields) == {"authoring_head", "serving_head"}

    version = golden_object()
    revision = golden_revision()
    assert ContentObjectVersion.model_validate_json(version.model_dump_json()) == version
    assert ContentRevision.model_validate_json(revision.model_dump_json()) == revision


def test_explicit_strict_false_cannot_downgrade_model_validation() -> None:
    version = golden_object()
    revision = golden_revision()
    with pytest.raises(ValueError, match="strict=False"):
        ContentObjectVersion.model_validate(version.model_dump(), strict=False)
    with pytest.raises(ValueError, match="strict=False"):
        ContentRevision.model_validate_json(revision.model_dump_json(), strict=False)


def test_schema_level_exact_types_resist_type_adapter_lax_validation() -> None:
    version = golden_object()
    object_values = version.model_dump()
    hostile_object_values = [
        {**object_values, field: object_values[field].encode("utf-8")}
        for field in ContentObjectVersion.model_fields
    ]
    for values in hostile_object_values:
        with pytest.raises(ValidationError, match="without coercion"):
            TypeAdapter(ContentObjectVersion).validate_python(values, strict=False)

    live = RevisionMembership.live(
        logical_id="npc_guard",
        object_version_id=GOLDEN_OBJECT_VERSION_ID,
    )
    live_values = live.model_dump()
    hostile_memberships = [
        {**live_values, "logical_id": b"npc_guard"},
        {**live_values, "object_version_id": GOLDEN_OBJECT_VERSION_ID.encode("ascii")},
        {**live_values, "tombstone": 0},
    ]
    for values in hostile_memberships:
        with pytest.raises(ValidationError, match="without coercion"):
            TypeAdapter(RevisionMembership).validate_python(values, strict=False)

    revision = golden_revision()
    revision_values = revision.model_dump()
    hostile_revisions = [
        {**revision_values, "revision_id": revision.revision_id.encode("ascii")},
        {**revision_values, "manifest_hash": revision.manifest_hash.encode("ascii")},
        {**revision_values, "created_by": b"author-1"},
        {**revision_values, "created_at": revision.created_at.isoformat()},
        {**revision_values, "created_at": 0},
    ]
    for values in hostile_revisions:
        with pytest.raises(ValidationError, match="without coercion"):
            TypeAdapter(ContentRevision).validate_python(values, strict=False)

    parent = "rev_" + "1" * 64
    child = ContentRevision.from_memberships(
        parent_revision=parent,
        memberships=golden_memberships(),
        created_at=CREATED_AT,
        created_by="author-1",
    )
    with pytest.raises(ValidationError, match="without coercion"):
        TypeAdapter(ContentRevision).validate_python(
            {**child.model_dump(), "parent_revision": parent.encode("ascii")},
            strict=False,
        )

    pointer = ReleasePointer(authoring_head=parent, serving_head=child.revision_id)
    for field in ReleasePointer.model_fields:
        with pytest.raises(ValidationError, match="without coercion"):
            TypeAdapter(ReleasePointer).validate_python(
                {**pointer.model_dump(), field: pointer.model_dump()[field].encode("ascii")},
                strict=False,
            )


def test_string_and_json_validation_modes_cannot_coerce_boolean_or_datetime_numbers() -> None:
    with pytest.raises(ValueError, match="model_validate_strings"):
        RevisionMembership.model_validate_strings(
            {
                "logical_id": "npc_guard",
                "object_version_id": GOLDEN_OBJECT_VERSION_ID,
                "tombstone": "false",
            },
            strict=True,
        )

    membership_json = json.dumps(
        {
            "logical_id": "npc_guard",
            "object_version_id": GOLDEN_OBJECT_VERSION_ID,
            "tombstone": 0,
        }
    )
    with pytest.raises(ValidationError, match="without coercion"):
        TypeAdapter(RevisionMembership).validate_json(membership_json, strict=False)

    revision_values = golden_revision().model_dump(mode="json")
    revision_values["created_at"] = 0
    with pytest.raises(ValidationError, match="without coercion"):
        TypeAdapter(ContentRevision).validate_json(json.dumps(revision_values), strict=False)

    hostile_datetime_strings = [
        "0",
        "1700000000",
        "2026-08-03",
        "2026-08-03 12:30:00Z",
        "2026-08-03T12:30:00",
        "2026-08-03T13:30:00+01:00",
    ]
    for hostile_created_at in hostile_datetime_strings:
        revision_values["created_at"] = hostile_created_at
        encoded = json.dumps(revision_values)
        with pytest.raises(ValidationError, match="RFC 3339 UTC"):
            ContentRevision.model_validate_json(encoded)
        with pytest.raises(ValidationError, match="RFC 3339 UTC"):
            TypeAdapter(ContentRevision).validate_json(encoded, strict=False)

    revision_values["created_at"] = "2026-08-03T12:30:00+00:00"
    plus_zero = ContentRevision.model_validate_json(json.dumps(revision_values))
    assert plus_zero.created_at == CREATED_AT


def test_deprecated_copy_cannot_bypass_derived_identity_validation() -> None:
    version = golden_object()
    with pytest.raises(ValidationError, match="content_hash"):
        version.copy(update={"payload_json": canonical_json({"tampered": True})})

    revision = golden_revision()
    with pytest.raises(ValidationError, match="revision_id"):
        revision.copy(update={"parent_revision": "rev_" + "3" * 64})

    membership = golden_memberships()[0]
    with pytest.raises(ValidationError, match="exactly one"):
        membership.copy(update={"tombstone": True})

    pointer = ReleasePointer(
        authoring_head=revision.revision_id,
        serving_head=revision.revision_id,
    )
    with pytest.raises(ValidationError, match="cannot exist"):
        pointer.copy(update={"authoring_head": None})


def test_hashes_are_stable_across_python_hash_seeds() -> None:
    script = """
from owcopilot.contracts.hashing import canonical_json, content_payload_hash
payload = dict({('z', 1), ('a', 2), ('middle', 3)})
payload.update({'rounded': 9007199254740993.0, 'underflow': 1e-999, 'signed_zero': -0.0})
print(content_payload_hash(canonical_json(payload)))
"""
    outputs: list[str] = []
    for seed in ("1", "777"):
        environment = os.environ.copy()
        environment.update({"PYTHONHASHSEED": seed, "PYTHONUTF8": "1"})
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        outputs.append(completed.stdout.strip())
    assert outputs[0] == outputs[1]


def test_legacy_content_hash_contract_is_unchanged() -> None:
    assert (
        legacy_content_hash({"b": 1, "a": "阿"})
        == "ae40057b31696dd56a08ce91d9c846282742ee809a773e71eca56fa3623392e6"
    )
