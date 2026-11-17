"""RLS-scoped PostgreSQL repository for immutable canonical revisions."""

from __future__ import annotations

import hmac
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, TypeAlias, cast
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import Table, and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.sql.elements import ColumnElement

from ..contracts import (
    ContentObjectVersion,
    ContentRevision,
    OwnershipScope,
    ReleasePointer,
    RevisionMembership,
)
from ..contracts.revisions import revision_manifest_hash
from .errors import (
    RevisionConflict,
    TeamAuthorizationError,
    TeamCanonicalUnavailableError,
    TeamDatabaseError,
    TeamIntegrityError,
    TeamNotFoundError,
)
from .schema import (
    containers,
    content_object_versions,
    content_revisions,
    operation_events,
    release_pointers,
    revision_memberships,
)
from .schema import (
    memberships as membership_table,
)
from .session import TeamDatabase

HeadName: TypeAlias = Literal["authoring", "serving"]
_MEMBERSHIP_ROLES = frozenset({"owner", "editor", "reviewer", "viewer"})
_WRITE_ROLES = frozenset({"owner", "editor"})


def _strict_nonempty_text(value: object, *, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be a non-empty string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} contains a lone Unicode surrogate") from exc
    return value


def _canonical_uuid(value: object, *, label: str) -> str:
    text = _strict_nonempty_text(value, label=label)
    try:
        parsed = UUID(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical UUID string") from exc
    if parsed.int == 0 or str(parsed) != text:
        raise ValueError(f"{label} must be a canonical non-nil UUID string")
    return text


def _serialize_datetime(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _jsonb_semantically_equal(source: object, projection: object) -> bool:
    """Compare JSON values using PostgreSQL jsonb semantics without erasing identity bytes.

    ``payload_json`` remains the immutable identity source. PostgreSQL ``jsonb`` is a query
    projection and may normalize numeric spelling (for example signed zero or exponent form), so
    byte-for-byte reserialization would reject valid rows. Booleans remain distinct from numbers.
    """

    if source is None or projection is None:
        return source is None and projection is None
    if type(source) is bool or type(projection) is bool:
        return type(source) is bool and type(projection) is bool and source == projection
    numeric_types = (int, float, Decimal)
    if isinstance(source, numeric_types) and isinstance(projection, numeric_types):
        return Decimal(str(source)) == Decimal(str(projection))
    if type(source) is str or type(projection) is str:
        return type(source) is str and type(projection) is str and source == projection
    if type(source) is list or type(projection) is list:
        if type(source) is not list or type(projection) is not list:
            return False
        return len(source) == len(projection) and all(
            _jsonb_semantically_equal(left, right)
            for left, right in zip(source, projection, strict=True)
        )
    if type(source) is dict or type(projection) is dict:
        if type(source) is not dict or type(projection) is not dict:
            return False
        if set(source) != set(projection):
            return False
        return all(_jsonb_semantically_equal(source[key], projection[key]) for key in source)
    return False


@dataclass(frozen=True, slots=True)
class MembershipGrant:
    tenant_id: str
    subject_id: str
    role: str

    def as_dict(self) -> dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "subject_id": self.subject_id,
            "role": self.role,
        }


@dataclass(frozen=True, slots=True)
class PointerState:
    pointer: ReleasePointer
    generation: int

    def as_dict(self) -> dict[str, object]:
        return {
            "pointer": self.pointer.model_dump(mode="json"),
            "generation": self.generation,
        }


@dataclass(frozen=True, slots=True)
class CanonicalState:
    scope: OwnershipScope
    selected_head: HeadName
    revision: ContentRevision
    memberships: tuple[RevisionMembership, ...]
    objects: tuple[ContentObjectVersion, ...]
    release: PointerState

    def as_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope.model_dump(mode="json"),
            "selected_head": self.selected_head,
            "revision": {
                **self.revision.model_dump(mode="json", exclude={"created_at"}),
                "created_at": _serialize_datetime(self.revision.created_at),
            },
            "memberships": [item.model_dump(mode="json") for item in self.memberships],
            "objects": [item.model_dump(mode="json") for item in self.objects],
            "release": self.release.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class _RevisionBundle:
    revision: ContentRevision
    memberships: tuple[RevisionMembership, ...]
    objects: tuple[ContentObjectVersion, ...]


def _validate_revision_bundle(
    *,
    revision: ContentRevision,
    memberships: Iterable[RevisionMembership],
    objects: Iterable[ContentObjectVersion],
) -> _RevisionBundle:
    """Revalidate a complete manifest and its exact live object set."""

    try:
        checked_revision = ContentRevision.model_validate(revision)
        checked_memberships = tuple(RevisionMembership.model_validate(item) for item in memberships)
        checked_objects = tuple(ContentObjectVersion.model_validate(item) for item in objects)
    except (TypeError, ValueError, ValidationError) as exc:
        raise TeamIntegrityError("revision bundle contains an invalid N1.1 contract") from exc

    memberships_by_logical: dict[str, RevisionMembership] = {}
    for membership in checked_memberships:
        if membership.logical_id in memberships_by_logical:
            raise TeamIntegrityError("revision bundle contains a duplicate logical_id")
        memberships_by_logical[membership.logical_id] = membership

    objects_by_id: dict[str, ContentObjectVersion] = {}
    for object_version in checked_objects:
        if object_version.object_version_id in objects_by_id:
            raise TeamIntegrityError("revision bundle contains a duplicate object_version_id")
        objects_by_id[object_version.object_version_id] = object_version

    live_memberships = [item for item in checked_memberships if not item.tombstone]
    live_ids = {cast(str, item.object_version_id) for item in live_memberships}
    if live_ids != set(objects_by_id):
        raise TeamIntegrityError(
            "revision bundle objects must exactly match all live manifest memberships"
        )
    for membership in live_memberships:
        object_version = objects_by_id[cast(str, membership.object_version_id)]
        if object_version.logical_id != membership.logical_id:
            raise TeamIntegrityError("live membership and object logical_id disagree")

    try:
        observed_manifest_hash = revision_manifest_hash(checked_memberships)
    except (TypeError, ValueError) as exc:
        raise TeamIntegrityError("revision bundle manifest cannot be canonicalized") from exc
    if not hmac.compare_digest(checked_revision.manifest_hash, observed_manifest_hash):
        raise TeamIntegrityError("revision manifest_hash does not match its complete memberships")

    return _RevisionBundle(
        revision=checked_revision,
        memberships=tuple(sorted(checked_memberships, key=lambda item: item.logical_id)),
        objects=tuple(sorted(checked_objects, key=lambda item: item.object_version_id)),
    )


class TeamRepository:
    """Canonical read/write repository; PostgreSQL and RLS are mandatory."""

    def __init__(self, database: TeamDatabase) -> None:
        self.database = database

    def authenticate_membership(self, tenant_id: str, subject_id: str) -> MembershipGrant:
        tenant = _canonical_uuid(tenant_id, label="tenant_id")
        subject = _canonical_uuid(subject_id, label="subject_id")
        tenant_uuid = UUID(tenant)
        subject_uuid = UUID(subject)
        try:
            with self.database.tenant_transaction(tenant) as connection:
                row = (
                    connection.execute(
                        select(
                            membership_table.c.tenant_id,
                            membership_table.c.subject_id,
                            membership_table.c.role,
                            membership_table.c.active,
                        ).where(
                            and_(
                                membership_table.c.tenant_id == tenant_uuid,
                                membership_table.c.subject_id == subject_uuid,
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
        except TeamDatabaseError:
            raise
        except SQLAlchemyError as exc:
            raise TeamDatabaseError("team membership lookup failed") from exc
        if row is None or type(row["active"]) is not bool or not row["active"]:
            raise TeamAuthorizationError("no active membership for the requested tenant")
        role = str(row["role"])
        if role not in _MEMBERSHIP_ROLES:
            raise TeamAuthorizationError("membership has an unsupported role")
        return MembershipGrant(tenant_id=tenant, subject_id=subject, role=role)

    def read_canonical(
        self,
        scope: OwnershipScope,
        *,
        subject_id: str,
        head: HeadName = "authoring",
    ) -> CanonicalState:
        checked_scope = OwnershipScope.model_validate(scope)
        subject = _canonical_uuid(subject_id, label="subject_id")
        checked_head = self._validate_head(head)
        try:
            with self.database.transaction(checked_scope) as connection:
                self._require_read_membership(
                    connection,
                    tenant_id=checked_scope.tenant_id,
                    subject_id=subject,
                )
                return self._read_canonical_connection(
                    connection,
                    scope=checked_scope,
                    head=checked_head,
                    pointer_for_update=False,
                )
        except TeamDatabaseError:
            raise
        except (TypeError, ValueError, ValidationError) as exc:
            raise TeamIntegrityError(
                "stored canonical state failed contract reconstruction"
            ) from exc
        except SQLAlchemyError as exc:
            raise TeamDatabaseError("team canonical read failed") from exc

    def write_revision(
        self,
        scope: OwnershipScope,
        *,
        revision: ContentRevision,
        memberships: Iterable[RevisionMembership],
        objects: Iterable[ContentObjectVersion],
        expected_authoring_head: str | None,
        expected_generation: int,
        actor_subject: str,
    ) -> CanonicalState:
        checked_scope = OwnershipScope.model_validate(scope)
        bundle = _validate_revision_bundle(
            revision=revision,
            memberships=memberships,
            objects=objects,
        )
        expected_head = self._validate_expected_head(expected_authoring_head)
        if type(expected_generation) is not int or expected_generation < 0:
            raise ValueError("expected_generation must be a non-negative integer")
        actor = _canonical_uuid(actor_subject, label="actor_subject")
        if bundle.revision.parent_revision != expected_head:
            raise TeamIntegrityError(
                "new revision parent_revision must equal the expected authoring head"
            )
        if bundle.revision.created_by != actor:
            raise TeamIntegrityError("revision created_by must equal the authenticated actor")

        try:
            with self.database.transaction(checked_scope) as connection:
                self._require_write_membership(
                    connection,
                    tenant_id=checked_scope.tenant_id,
                    subject_id=actor,
                )
                current = self._load_pointer(
                    connection,
                    scope=checked_scope,
                    for_update=True,
                )
                if (
                    current.pointer.authoring_head != expected_head
                    or current.generation != expected_generation
                ):
                    raise RevisionConflict("authoring head or lock generation is stale")

                self._insert_object_versions(connection, scope=checked_scope, bundle=bundle)
                self._insert_revision(connection, scope=checked_scope, bundle=bundle)
                self._insert_memberships(connection, scope=checked_scope, bundle=bundle)
                self._insert_operation_event(
                    connection,
                    scope=checked_scope,
                    revision_id=bundle.revision.revision_id,
                    actor_subject=actor,
                    previous_head=expected_head,
                    previous_generation=expected_generation,
                )
                self._advance_authoring_pointer(
                    connection,
                    scope=checked_scope,
                    revision_id=bundle.revision.revision_id,
                    expected_head=expected_head,
                    expected_generation=expected_generation,
                )
                return self._read_canonical_connection(
                    connection,
                    scope=checked_scope,
                    head="authoring",
                    pointer_for_update=False,
                )
        except TeamDatabaseError:
            raise
        except IntegrityError as exc:
            raise TeamIntegrityError(
                "PostgreSQL rejected the immutable revision transaction"
            ) from exc
        except (TypeError, ValueError, ValidationError) as exc:
            raise TeamIntegrityError("revision transaction failed contract validation") from exc
        except SQLAlchemyError as exc:
            raise TeamDatabaseError("team canonical write failed") from exc

    @staticmethod
    def _validate_head(head: object) -> HeadName:
        if type(head) is not str:
            raise ValueError("head must be exactly 'authoring' or 'serving'")
        if head == "authoring":
            return "authoring"
        if head == "serving":
            return "serving"
        raise ValueError("head must be exactly 'authoring' or 'serving'")

    @staticmethod
    def _validate_expected_head(head: object) -> str | None:
        if head is None:
            return None
        if type(head) is not str:
            raise ValueError("expected_authoring_head must be a revision id or null")
        # ReleasePointer supplies the frozen revision-id validation without
        # manufacturing a serving head.
        return ReleasePointer(authoring_head=head, serving_head=None).authoring_head

    @staticmethod
    def _scope_predicate(table: Table, scope: OwnershipScope) -> ColumnElement[bool]:
        columns = table.c
        return and_(
            columns.tenant_id == UUID(scope.tenant_id),
            columns.container_id == UUID(scope.container_id),
        )

    def _require_container(self, connection: Connection, scope: OwnershipScope) -> None:
        row = connection.execute(
            select(containers.c.container_id).where(self._scope_predicate(containers, scope))
        ).one_or_none()
        if row is None:
            raise TeamNotFoundError("container does not exist in the requested ownership scope")

    def _load_pointer(
        self,
        connection: Connection,
        *,
        scope: OwnershipScope,
        for_update: bool,
    ) -> PointerState:
        self._require_container(connection, scope)
        statement = select(
            release_pointers.c.authoring_head,
            release_pointers.c.serving_head,
            release_pointers.c.lock_version,
        ).where(self._scope_predicate(release_pointers, scope))
        if for_update:
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            raise TeamNotFoundError("release pointer does not exist for the container")
        try:
            generation = row["lock_version"]
            if type(generation) is not int or generation < 0:
                raise ValueError("release pointer lock_version is invalid")
            pointer = ReleasePointer(
                authoring_head=row["authoring_head"],
                serving_head=row["serving_head"],
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise TeamIntegrityError("stored release pointer violates its contract") from exc
        return PointerState(pointer=pointer, generation=generation)

    def _read_canonical_connection(
        self,
        connection: Connection,
        *,
        scope: OwnershipScope,
        head: HeadName,
        pointer_for_update: bool,
    ) -> CanonicalState:
        pointer = self._load_pointer(connection, scope=scope, for_update=pointer_for_update)
        revision_id = (
            pointer.pointer.authoring_head if head == "authoring" else pointer.pointer.serving_head
        )
        if revision_id is None:
            raise TeamCanonicalUnavailableError(f"the requested {head} head is not available")

        revision_row = (
            connection.execute(
                select(
                    content_revisions.c.revision_id,
                    content_revisions.c.parent_revision,
                    content_revisions.c.manifest_hash,
                    content_revisions.c.created_at,
                    content_revisions.c.created_by,
                ).where(
                    and_(
                        self._scope_predicate(content_revisions, scope),
                        content_revisions.c.revision_id == revision_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if revision_row is None:
            raise TeamIntegrityError("release pointer refers to a missing revision")
        revision = self._revision_from_row(revision_row)

        membership_rows = (
            connection.execute(
                select(
                    revision_memberships.c.logical_id,
                    revision_memberships.c.object_version_id,
                    revision_memberships.c.tombstone,
                )
                .where(
                    and_(
                        self._scope_predicate(revision_memberships, scope),
                        revision_memberships.c.revision_id == revision_id,
                    )
                )
                .order_by(revision_memberships.c.logical_id)
            )
            .mappings()
            .all()
        )
        reconstructed_memberships = tuple(self._membership_from_row(row) for row in membership_rows)

        observed_manifest_hash = revision_manifest_hash(reconstructed_memberships)
        if not hmac.compare_digest(revision.manifest_hash, observed_manifest_hash):
            raise TeamIntegrityError("stored revision memberships do not match manifest_hash")

        live_ids = [
            cast(str, membership.object_version_id)
            for membership in reconstructed_memberships
            if not membership.tombstone
        ]
        object_rows: list[RowMapping]
        if live_ids:
            object_rows = list(
                connection.execute(
                    select(
                        content_object_versions.c.logical_id,
                        content_object_versions.c.object_version_id,
                        content_object_versions.c.object_type,
                        content_object_versions.c.payload_json,
                        content_object_versions.c.payload_jsonb,
                        content_object_versions.c.content_hash,
                    )
                    .where(
                        and_(
                            self._scope_predicate(content_object_versions, scope),
                            content_object_versions.c.object_version_id.in_(live_ids),
                        )
                    )
                    .order_by(content_object_versions.c.object_version_id)
                )
                .mappings()
                .all()
            )
        else:
            object_rows = []
        reconstructed_objects = tuple(self._object_from_row(row) for row in object_rows)
        objects_by_id = {item.object_version_id: item for item in reconstructed_objects}
        if len(objects_by_id) != len(reconstructed_objects) or set(objects_by_id) != set(live_ids):
            raise TeamIntegrityError(
                "stored revision has missing or duplicate live object versions"
            )
        for membership in reconstructed_memberships:
            if membership.tombstone:
                continue
            object_version = objects_by_id[cast(str, membership.object_version_id)]
            if object_version.logical_id != membership.logical_id:
                raise TeamIntegrityError("stored membership and object logical_id disagree")

        return CanonicalState(
            scope=scope,
            selected_head=head,
            revision=revision,
            memberships=reconstructed_memberships,
            objects=reconstructed_objects,
            release=pointer,
        )

    @staticmethod
    def _revision_from_row(row: RowMapping) -> ContentRevision:
        try:
            return ContentRevision.model_validate(
                {
                    "revision_id": row["revision_id"],
                    "parent_revision": row["parent_revision"],
                    "manifest_hash": row["manifest_hash"],
                    "created_at": row["created_at"],
                    "created_by": row["created_by"],
                }
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise TeamIntegrityError("stored revision row violates ContentRevision") from exc

    @staticmethod
    def _membership_from_row(row: RowMapping) -> RevisionMembership:
        try:
            return RevisionMembership.model_validate(
                {
                    "logical_id": row["logical_id"],
                    "object_version_id": row["object_version_id"],
                    "tombstone": row["tombstone"],
                }
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise TeamIntegrityError("stored membership row violates RevisionMembership") from exc

    @staticmethod
    def _object_from_row(row: RowMapping) -> ContentObjectVersion:
        try:
            object_version = ContentObjectVersion.model_validate(
                {
                    "logical_id": row["logical_id"],
                    "object_version_id": row["object_version_id"],
                    "object_type": row["object_type"],
                    "payload_json": row["payload_json"],
                    "content_hash": row["content_hash"],
                }
            )
            identity_value = json.loads(object_version.payload_json)
        except (TypeError, ValueError, ValidationError) as exc:
            raise TeamIntegrityError("stored object row violates ContentObjectVersion") from exc
        if not _jsonb_semantically_equal(identity_value, row["payload_jsonb"]):
            raise TeamIntegrityError("stored payload_json and payload_jsonb disagree")
        return object_version

    @staticmethod
    def _require_read_membership(
        connection: Connection,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> MembershipGrant:
        row = (
            connection.execute(
                select(membership_table.c.role, membership_table.c.active).where(
                    and_(
                        membership_table.c.tenant_id == UUID(tenant_id),
                        membership_table.c.subject_id == UUID(subject_id),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or type(row["active"]) is not bool or not row["active"]:
            raise TeamAuthorizationError("no active membership for the requested tenant")
        role = str(row["role"])
        if role not in _MEMBERSHIP_ROLES:
            raise TeamAuthorizationError("membership has an unsupported role")
        return MembershipGrant(tenant_id=tenant_id, subject_id=subject_id, role=role)

    @staticmethod
    def _require_write_membership(
        connection: Connection,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> None:
        row = (
            connection.execute(
                select(membership_table.c.role, membership_table.c.active).where(
                    and_(
                        membership_table.c.tenant_id == UUID(tenant_id),
                        membership_table.c.subject_id == UUID(subject_id),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            row is None
            or type(row["active"]) is not bool
            or not row["active"]
            or str(row["role"]) not in _WRITE_ROLES
        ):
            raise TeamAuthorizationError("an active editor or owner membership is required")

    @staticmethod
    def _insert_object_versions(
        connection: Connection,
        *,
        scope: OwnershipScope,
        bundle: _RevisionBundle,
    ) -> None:
        if not bundle.objects:
            return
        values = [
            {
                "tenant_id": UUID(scope.tenant_id),
                "container_id": UUID(scope.container_id),
                "logical_id": item.logical_id,
                "object_version_id": item.object_version_id,
                "object_type": item.object_type,
                "payload_json": item.payload_json,
                "payload_jsonb": json.loads(item.payload_json),
                "content_hash": item.content_hash,
            }
            for item in bundle.objects
        ]
        statement = pg_insert(content_object_versions).values(values)
        statement = statement.on_conflict_do_nothing(
            index_elements=[
                content_object_versions.c.tenant_id,
                content_object_versions.c.container_id,
                content_object_versions.c.logical_id,
                content_object_versions.c.object_version_id,
            ]
        )
        connection.execute(statement)

        stored_rows = (
            connection.execute(
                select(
                    content_object_versions.c.logical_id,
                    content_object_versions.c.object_version_id,
                    content_object_versions.c.object_type,
                    content_object_versions.c.payload_json,
                    content_object_versions.c.payload_jsonb,
                    content_object_versions.c.content_hash,
                ).where(
                    and_(
                        content_object_versions.c.tenant_id == UUID(scope.tenant_id),
                        content_object_versions.c.container_id == UUID(scope.container_id),
                        content_object_versions.c.object_version_id.in_(
                            [item.object_version_id for item in bundle.objects]
                        ),
                    )
                )
            )
            .mappings()
            .all()
        )
        stored = {
            item.object_version_id: item
            for item in map(TeamRepository._object_from_row, stored_rows)
        }
        expected = {item.object_version_id: item for item in bundle.objects}
        if stored != expected:
            raise TeamIntegrityError("an existing object version has conflicting immutable bytes")

    @staticmethod
    def _insert_revision(
        connection: Connection,
        *,
        scope: OwnershipScope,
        bundle: _RevisionBundle,
    ) -> None:
        connection.execute(
            content_revisions.insert().values(
                tenant_id=UUID(scope.tenant_id),
                container_id=UUID(scope.container_id),
                revision_id=bundle.revision.revision_id,
                parent_revision=bundle.revision.parent_revision,
                manifest_hash=bundle.revision.manifest_hash,
                created_at=bundle.revision.created_at,
                created_by=bundle.revision.created_by,
                import_run_id=None,
            )
        )

    @staticmethod
    def _insert_memberships(
        connection: Connection,
        *,
        scope: OwnershipScope,
        bundle: _RevisionBundle,
    ) -> None:
        if not bundle.memberships:
            return
        connection.execute(
            revision_memberships.insert(),
            [
                {
                    "tenant_id": UUID(scope.tenant_id),
                    "container_id": UUID(scope.container_id),
                    "revision_id": bundle.revision.revision_id,
                    "logical_id": item.logical_id,
                    "object_version_id": item.object_version_id,
                    "tombstone": item.tombstone,
                }
                for item in bundle.memberships
            ],
        )

    @staticmethod
    def _insert_operation_event(
        connection: Connection,
        *,
        scope: OwnershipScope,
        revision_id: str,
        actor_subject: str,
        previous_head: str | None,
        previous_generation: int,
    ) -> None:
        connection.execute(
            operation_events.insert().values(
                tenant_id=UUID(scope.tenant_id),
                container_id=UUID(scope.container_id),
                event_id=uuid4(),
                event_type="canonical.revision.created",
                subject_id=UUID(actor_subject),
                revision_id=revision_id,
                actor_id=UUID(actor_subject),
                payload_jsonb={
                    "previous_authoring_head": previous_head,
                    "previous_lock_version": previous_generation,
                },
            )
        )

    @staticmethod
    def _advance_authoring_pointer(
        connection: Connection,
        *,
        scope: OwnershipScope,
        revision_id: str,
        expected_head: str | None,
        expected_generation: int,
    ) -> None:
        head_predicate = (
            release_pointers.c.authoring_head.is_(None)
            if expected_head is None
            else release_pointers.c.authoring_head == expected_head
        )
        result = connection.execute(
            update(release_pointers)
            .where(
                and_(
                    release_pointers.c.tenant_id == UUID(scope.tenant_id),
                    release_pointers.c.container_id == UUID(scope.container_id),
                    head_predicate,
                    release_pointers.c.lock_version == expected_generation,
                )
            )
            .values(
                authoring_head=revision_id,
                lock_version=expected_generation + 1,
                updated_at=func.now(),
            )
        )
        if result.rowcount != 1:
            raise RevisionConflict("authoring release-pointer CAS lost")


__all__ = [
    "CanonicalState",
    "HeadName",
    "MembershipGrant",
    "PointerState",
    "TeamRepository",
]
