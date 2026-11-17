"""Crash-safe protocol for publishing one legacy genesis revision."""

from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol

from sqlalchemy import Table, and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.sql.elements import ColumnElement

from ..content.store import ContentStore
from ..contracts import (
    ContentObjectVersion,
    ContentRevision,
    OwnershipScope,
    ReleasePointer,
    RevisionMembership,
)
from ..contracts.hashing import canonical_json
from ..contracts.revisions import revision_manifest_hash
from .canonical_mapping import GenesisPlan, map_genesis, resolve_legacy_canon
from .legacy_archive import (
    LegacyArchivePackage,
    restore_legacy_archive,
    verify_legacy_archive,
)
from .schema import (
    bootstrap_import_runs,
    containers,
    content_object_versions,
    content_revisions,
    legacy_archive_registry,
    operation_events,
    release_pointers,
    revision_memberships,
)
from .schema import memberships as membership_table
from .session import TeamDatabase

_RUN_NAMESPACE = uuid.UUID("fe9f8f39-8740-5c03-8e37-5270288f1919")
_EVENT_NAMESPACE = uuid.UUID("b64f5674-9ca6-5ad5-99fd-cbf0da7d42f1")


class BootstrapProtocolError(RuntimeError):
    """Repository state violates the frozen bootstrap state machine."""


class BootstrapPhase(str, Enum):
    """Stable fault-injection boundaries for the BUILDING -> COMPLETED protocol."""

    BUILDING_COMMITTED = "building_committed"
    ARCHIVE_REGISTERED = "archive_registered"
    OBJECT_VERSIONS_STAGED = "object_versions_staged"
    REVISION_STAGED = "revision_staged"
    MEMBERSHIPS_STAGED = "memberships_staged"
    AUTHORING_HEAD_STAGED = "authoring_head_staged"
    OPERATION_EVENT_STAGED = "operation_event_staged"
    COMPLETED_STAGED = "completed_staged"
    TRANSACTION_COMMITTED = "transaction_committed"


@dataclass(frozen=True, slots=True)
class BootstrapImportRun:
    import_run_id: str
    idempotency_key: str
    source_manifest_hash: str
    status: Literal["BUILDING", "COMPLETED"]
    started_at: datetime
    finished_at: datetime | None = None
    genesis_revision_id: str | None = None


@dataclass(frozen=True, slots=True)
class LegacyRegistryEntry:
    archive_id: str
    relative_path: str
    entry_kind: Literal["directory_manifest", "file", "sqlite_record"]
    sha256: str
    byte_size: int
    record_class: str
    disposition: str
    source_status: str


@dataclass(frozen=True, slots=True)
class BootstrapCompletion:
    import_run_id: str
    source_manifest_hash: str
    resolved_legacy_version: str
    genesis_revision_id: str
    genesis_manifest_hash: str
    archive_id: str
    archive_sha256: str
    object_version_count: int
    membership_count: int
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    completion: BootstrapCompletion
    idempotent_replay: bool


class BootstrapTransaction(Protocol):
    """The main import transaction; every mutating method rolls back together."""

    def lock_import_run(self, import_run_id: str) -> BootstrapImportRun:
        """Lock and return the run row (``SELECT ... FOR UPDATE`` in PG)."""

    def assert_genesis_target_empty(self) -> None:
        """Fail when the container already has a release pointer or visible revision."""

    def register_legacy_archive(self, entries: Iterable[LegacyRegistryEntry]) -> None:
        """Insert the manifest, file checksums and per-record dispositions."""

    def put_object_versions(self, versions: Iterable[ContentObjectVersion]) -> None:
        """Insert immutable object versions idempotently, rejecting identity mismatch."""

    def put_revision(self, revision: ContentRevision, *, import_run_id: str) -> None:
        """Stage the parentless revision, still invisible without a release pointer."""

    def put_memberships(
        self,
        revision_id: str,
        memberships: Iterable[RevisionMembership],
    ) -> None:
        """Insert the complete manifest and verify its hash against the revision."""

    def initialize_release_pointer(self, pointer: ReleasePointer) -> None:
        """Create the first authoring head; serving remains unset."""

    def record_genesis_imported(
        self,
        completion: BootstrapCompletion,
        *,
        actor_id: str,
    ) -> None:
        """Append the deterministic ``canonical.genesis.imported`` operation event."""

    def complete_import_run(self, completion: BootstrapCompletion) -> None:
        """Transition the locked BUILDING row to COMPLETED exactly once."""


class BootstrapRepository(Protocol):
    """Narrow adapter boundary implemented by the PostgreSQL team repository."""

    def authorize_import(self, scope: OwnershipScope, *, actor_subject: str) -> None:
        """Require the configured actor to be a current owner before BUILDING."""

    def begin_import_run(
        self,
        scope: OwnershipScope,
        run: BootstrapImportRun,
    ) -> BootstrapImportRun:
        """Commit BUILDING or return the existing row for this idempotency key.

        Implementations must serialize competing bootstrap keys per container and
        reject a second source while another BUILDING run is active.
        """

    def import_transaction(
        self,
        scope: OwnershipScope,
    ) -> AbstractContextManager[BootstrapTransaction]:
        """Open one all-or-nothing transaction with tenant context already SET LOCAL."""


KillHook = Callable[[BootstrapPhase], None]


class PostgresBootstrapRepository:
    """Real PostgreSQL adapter backed by :class:`TeamDatabase` scoped transactions."""

    def __init__(self, database: TeamDatabase, *, actor_subject: str) -> None:
        checked_actor = _uuid_value(actor_subject, label="actor_subject")
        self._database = database
        self._actor_subject = str(checked_actor)

    def authorize_import(self, scope: OwnershipScope, *, actor_subject: str) -> None:
        if actor_subject != self._actor_subject:
            raise BootstrapProtocolError("genesis created_by does not match bootstrap actor")
        checked_scope = OwnershipScope.model_validate(scope)
        with self._database.transaction(checked_scope) as connection:
            _require_owner(connection, checked_scope, self._actor_subject)

    def begin_import_run(
        self,
        scope: OwnershipScope,
        run: BootstrapImportRun,
    ) -> BootstrapImportRun:
        checked_scope = OwnershipScope.model_validate(scope)
        with self._database.transaction(checked_scope) as connection:
            scope_predicate = _scope_predicate(bootstrap_import_runs, checked_scope)
            # Runtime deliberately has no UPDATE grant on the container control
            # plane, so SELECT FOR UPDATE is unavailable.  A transaction-scoped
            # advisory lock serializes all bootstrap keys for this exact scope
            # without widening table privileges.
            connection.execute(select(func.pg_advisory_xact_lock(_advisory_key(checked_scope))))
            _require_owner(connection, checked_scope, self._actor_subject)
            container = connection.execute(
                select(containers.c.container_id).where(_scope_predicate(containers, checked_scope))
            ).one_or_none()
            if container is None:
                raise BootstrapProtocolError("bootstrap container does not exist or is not visible")
            existing = (
                connection.execute(
                    select(bootstrap_import_runs)
                    .where(
                        and_(
                            scope_predicate,
                            bootstrap_import_runs.c.idempotency_key == run.idempotency_key,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                return _run_from_pg(existing)
            other = (
                connection.execute(
                    select(bootstrap_import_runs.c.import_run_id).where(
                        and_(
                            scope_predicate,
                            bootstrap_import_runs.c.status.in_(("building", "committed")),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if other is not None:
                raise BootstrapProtocolError(
                    "container already has a live or completed bootstrap source"
                )
            connection.execute(
                insert(bootstrap_import_runs).values(
                    **_scope_values(checked_scope),
                    import_run_id=_uuid_value(run.import_run_id, label="import_run_id"),
                    idempotency_key=run.idempotency_key,
                    source_manifest_hash=run.source_manifest_hash,
                    status="building",
                    started_at=run.started_at,
                )
            )
            return run

    @contextmanager
    def import_transaction(
        self,
        scope: OwnershipScope,
    ) -> Iterator[BootstrapTransaction]:
        checked_scope = OwnershipScope.model_validate(scope)
        with self._database.transaction(checked_scope) as connection:
            _require_owner(connection, checked_scope, self._actor_subject)
            yield _PostgresBootstrapTransaction(
                connection,
                checked_scope,
                actor_subject=self._actor_subject,
            )


class _PostgresBootstrapTransaction:
    def __init__(
        self,
        connection: Connection,
        scope: OwnershipScope,
        *,
        actor_subject: str,
    ) -> None:
        self._connection = connection
        self._scope = scope
        self._actor_subject = actor_subject

    def lock_import_run(self, import_run_id: str) -> BootstrapImportRun:
        row = (
            self._connection.execute(
                select(bootstrap_import_runs)
                .where(
                    and_(
                        _scope_predicate(bootstrap_import_runs, self._scope),
                        bootstrap_import_runs.c.import_run_id
                        == _uuid_value(import_run_id, label="import_run_id"),
                    )
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise BootstrapProtocolError("BUILDING import run disappeared before publication")
        return _run_from_pg(row)

    def assert_genesis_target_empty(self) -> None:
        revision_exists = self._connection.execute(
            select(content_revisions.c.revision_id)
            .where(_scope_predicate(content_revisions, self._scope))
            .limit(1)
        ).first()
        pointer = (
            self._connection.execute(
                select(release_pointers.c.authoring_head, release_pointers.c.serving_head)
                .where(_scope_predicate(release_pointers, self._scope))
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if revision_exists is not None or pointer is not None:
            raise BootstrapProtocolError("container already has visible canonical history")

    def register_legacy_archive(self, entries: Iterable[LegacyRegistryEntry]) -> None:
        values = [
            {
                **_scope_values(self._scope),
                "archive_id": _uuid_value(entry.archive_id, label="archive_id"),
                "relative_path": entry.relative_path,
                "entry_kind": entry.entry_kind,
                "sha256": entry.sha256,
                "byte_size": entry.byte_size,
                "record_class": entry.record_class,
                "disposition": entry.disposition,
                "source_status": entry.source_status,
            }
            for entry in entries
        ]
        if values:
            self._connection.execute(insert(legacy_archive_registry), values)

    def put_object_versions(self, versions: Iterable[ContentObjectVersion]) -> None:
        checked = tuple(versions)
        values = [
            {
                **_scope_values(self._scope),
                "logical_id": item.logical_id,
                "object_version_id": item.object_version_id,
                "object_type": item.object_type,
                "payload_json": item.payload_json,
                "payload_jsonb": json.loads(item.payload_json),
                "content_hash": item.content_hash,
            }
            for item in checked
        ]
        if values:
            statement = pg_insert(content_object_versions).values(values)
            self._connection.execute(
                statement.on_conflict_do_nothing(
                    index_elements=(
                        content_object_versions.c.tenant_id,
                        content_object_versions.c.container_id,
                        content_object_versions.c.object_version_id,
                    )
                )
            )
            rows = (
                self._connection.execute(
                    select(content_object_versions).where(
                        and_(
                            _scope_predicate(content_object_versions, self._scope),
                            content_object_versions.c.object_version_id.in_(
                                [item.object_version_id for item in checked]
                            ),
                        )
                    )
                )
                .mappings()
                .all()
            )
            observed = {str(row["object_version_id"]): row for row in rows}
            if set(observed) != {item.object_version_id for item in checked}:
                raise BootstrapProtocolError("object-version insert/readback cardinality mismatch")
            for item in checked:
                row = observed[item.object_version_id]
                if (
                    str(row["logical_id"]) != item.logical_id
                    or str(row["object_type"]) != item.object_type
                    or str(row["payload_json"]) != item.payload_json
                    or str(row["content_hash"]) != item.content_hash
                ):
                    raise BootstrapProtocolError(
                        "object_version_id collision has a different immutable identity"
                    )

    def put_revision(self, revision: ContentRevision, *, import_run_id: str) -> None:
        if revision.created_by != self._actor_subject:
            raise BootstrapProtocolError("genesis revision actor is not the authorized owner")
        self._connection.execute(
            insert(content_revisions).values(
                **_scope_values(self._scope),
                revision_id=revision.revision_id,
                parent_revision=revision.parent_revision,
                manifest_hash=revision.manifest_hash,
                created_at=revision.created_at,
                created_by=revision.created_by,
                import_run_id=_uuid_value(import_run_id, label="import_run_id"),
            )
        )

    def put_memberships(
        self,
        revision_id: str,
        memberships: Iterable[RevisionMembership],
    ) -> None:
        values = [
            {
                **_scope_values(self._scope),
                "revision_id": revision_id,
                "logical_id": item.logical_id,
                "object_version_id": item.object_version_id,
                "tombstone": item.tombstone,
            }
            for item in memberships
        ]
        if values:
            self._connection.execute(insert(revision_memberships), values)

    def initialize_release_pointer(self, pointer: ReleasePointer) -> None:
        self._connection.execute(
            insert(release_pointers).values(
                **_scope_values(self._scope),
                authoring_head=pointer.authoring_head,
                serving_head=pointer.serving_head,
                lock_version=0,
            )
        )

    def complete_import_run(self, completion: BootstrapCompletion) -> None:
        run_uuid = _uuid_value(completion.import_run_id, label="import_run_id")
        archive_uuid = _uuid_value(completion.archive_id, label="archive_id")
        revision = (
            self._connection.execute(
                select(
                    content_revisions.c.manifest_hash,
                    content_revisions.c.import_run_id,
                ).where(
                    and_(
                        _scope_predicate(content_revisions, self._scope),
                        content_revisions.c.revision_id == completion.genesis_revision_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if revision is None or (
            str(revision["manifest_hash"]) != completion.genesis_manifest_hash
            or revision["import_run_id"] != run_uuid
        ):
            raise BootstrapProtocolError("staged genesis revision does not match completion")
        counts = (
            self._connection.execute(
                select(
                    func.count().label("memberships"),
                    func.count(revision_memberships.c.object_version_id).label("objects"),
                ).where(
                    and_(
                        _scope_predicate(revision_memberships, self._scope),
                        revision_memberships.c.revision_id == completion.genesis_revision_id,
                    )
                )
            )
            .mappings()
            .one()
        )
        if (
            int(counts["memberships"]) != completion.membership_count
            or int(counts["objects"]) != completion.object_version_count
        ):
            raise BootstrapProtocolError("staged genesis cardinalities do not match completion")
        archive_rows = (
            self._connection.execute(
                select(
                    legacy_archive_registry.c.relative_path,
                    legacy_archive_registry.c.sha256,
                ).where(
                    and_(
                        _scope_predicate(legacy_archive_registry, self._scope),
                        legacy_archive_registry.c.archive_id == archive_uuid,
                        legacy_archive_registry.c.relative_path.in_(
                            ("legacy-archive.zip", "legacy-manifest.json")
                        ),
                    )
                )
            )
            .mappings()
            .all()
        )
        archive_identity = {str(row["relative_path"]): str(row["sha256"]) for row in archive_rows}
        if archive_identity != {
            "legacy-archive.zip": completion.archive_sha256,
            "legacy-manifest.json": completion.source_manifest_hash,
        }:
            raise BootstrapProtocolError("staged legacy archive identities do not match completion")
        pointer = (
            self._connection.execute(
                select(release_pointers.c.authoring_head, release_pointers.c.serving_head).where(
                    _scope_predicate(release_pointers, self._scope)
                )
            )
            .mappings()
            .one_or_none()
        )
        if pointer is None or (
            str(pointer["authoring_head"]) != completion.genesis_revision_id
            or pointer["serving_head"] is not None
        ):
            raise BootstrapProtocolError("staged release pointer does not match genesis")
        event_id = uuid.uuid5(
            _EVENT_NAMESPACE,
            f"{completion.import_run_id}/canonical.genesis.imported",
        )
        if (
            self._connection.execute(
                select(operation_events.c.event_id).where(
                    and_(
                        _scope_predicate(operation_events, self._scope),
                        operation_events.c.event_id == event_id,
                        operation_events.c.revision_id == completion.genesis_revision_id,
                        operation_events.c.event_type == "canonical.genesis.imported",
                    )
                )
            ).one_or_none()
            is None
        ):
            raise BootstrapProtocolError("genesis operation event is missing")
        result = self._connection.execute(
            update(bootstrap_import_runs)
            .where(
                and_(
                    _scope_predicate(bootstrap_import_runs, self._scope),
                    bootstrap_import_runs.c.import_run_id == run_uuid,
                    bootstrap_import_runs.c.source_manifest_hash == completion.source_manifest_hash,
                    bootstrap_import_runs.c.status == "building",
                )
            )
            .values(
                status="committed",
                finished_at=completion.completed_at,
                genesis_revision_id=completion.genesis_revision_id,
                error_code=None,
            )
        )
        if result.rowcount != 1:
            raise BootstrapProtocolError("BUILDING -> COMPLETED transition lost its lock")

    def record_genesis_imported(
        self,
        completion: BootstrapCompletion,
        *,
        actor_id: str,
    ) -> None:
        if actor_id != self._actor_subject:
            raise BootstrapProtocolError("genesis operation event actor is not authorized")
        event_id = uuid.uuid5(
            _EVENT_NAMESPACE,
            f"{completion.import_run_id}/canonical.genesis.imported",
        )
        self._connection.execute(
            insert(operation_events).values(
                **_scope_values(self._scope),
                event_id=event_id,
                event_type="canonical.genesis.imported",
                subject_id=actor_id,
                revision_id=completion.genesis_revision_id,
                actor_id=actor_id,
                occurred_at=completion.completed_at,
                payload_jsonb={
                    "archive_id": completion.archive_id,
                    "archive_sha256": completion.archive_sha256,
                    "import_run_id": completion.import_run_id,
                    "resolved_legacy_version": completion.resolved_legacy_version,
                    "source_manifest_hash": completion.source_manifest_hash,
                },
            )
        )


def _scope_values(scope: OwnershipScope) -> dict[str, uuid.UUID]:
    return {
        "tenant_id": _uuid_value(scope.tenant_id, label="tenant_id"),
        "container_id": _uuid_value(scope.container_id, label="container_id"),
    }


def _scope_predicate(table: Table, scope: OwnershipScope) -> ColumnElement[bool]:
    columns = table.c
    return and_(
        columns.tenant_id == _uuid_value(scope.tenant_id, label="tenant_id"),
        columns.container_id == _uuid_value(scope.container_id, label="container_id"),
    )


def _uuid_value(value: str, *, label: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise BootstrapProtocolError(f"{label} must be a canonical UUID") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise BootstrapProtocolError(f"{label} must be a canonical non-nil UUID")
    return parsed


def _run_from_pg(row: RowMapping) -> BootstrapImportRun:
    database_status = str(row["status"])
    if database_status == "building":
        status: Literal["BUILDING", "COMPLETED"] = "BUILDING"
    elif database_status == "committed":
        status = "COMPLETED"
    else:
        raise BootstrapProtocolError(f"bootstrap run is terminally unusable: {database_status}")
    started_at = row["started_at"]
    finished_at = row["finished_at"]
    if not isinstance(started_at, datetime) or (
        finished_at is not None and not isinstance(finished_at, datetime)
    ):
        raise BootstrapProtocolError("bootstrap run timestamps have invalid database types")
    return BootstrapImportRun(
        import_run_id=str(row["import_run_id"]),
        idempotency_key=str(row["idempotency_key"]),
        source_manifest_hash=str(row["source_manifest_hash"]),
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        genesis_revision_id=(
            None if row["genesis_revision_id"] is None else str(row["genesis_revision_id"])
        ),
    )


def _require_owner(
    connection: Connection,
    scope: OwnershipScope,
    actor_subject: str,
) -> None:
    role = connection.execute(
        select(membership_table.c.role).where(
            and_(
                membership_table.c.tenant_id == _uuid_value(scope.tenant_id, label="tenant_id"),
                membership_table.c.subject_id == _uuid_value(actor_subject, label="actor_subject"),
                membership_table.c.role == "owner",
                membership_table.c.active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if role != "owner":
        raise BootstrapProtocolError("bootstrap actor is not a current tenant owner")


def bootstrap_genesis(
    *,
    repository: BootstrapRepository,
    scope: OwnershipScope,
    genesis: GenesisPlan,
    archive: LegacyArchivePackage,
    kill_hook: KillHook | None = None,
    now: Callable[[], datetime] | None = None,
) -> BootstrapResult:
    """Publish genesis atomically, with only BUILDING visible after an injected kill.

    Archive preflight and all N1.1 contract construction happen before BUILDING.
    The BUILDING row is the sole separately committed write.  Object versions,
    revision, complete memberships, archive registry, authoring pointer and the
    COMPLETED transition share the following transaction.  Therefore an error at
    any in-transaction hook cannot expose a partial authoring revision.
    """

    archive.require_cutover_safe()
    if genesis.source_manifest_hash != archive.manifest.source_manifest_hash:
        raise BootstrapProtocolError("genesis plan is not bound to this archive source manifest")
    verified_manifest = verify_legacy_archive(
        archive.archive_path,
        expected_archive_sha256=archive.archive_sha256,
    )
    if verified_manifest.to_json() != archive.manifest.to_json():
        raise BootstrapProtocolError("archive package manifest changed after preflight")
    _validate_genesis(genesis)
    repository.authorize_import(scope, actor_subject=genesis.revision.created_by)

    clock = now or (lambda: datetime.now(UTC))
    source_hash = archive.manifest.source_manifest_hash
    intent_hash = _bootstrap_intent_hash(genesis=genesis, archive=archive)
    run_id = _run_id(scope, source_hash, intent_hash)
    idempotency_key = f"legacy-genesis:{source_hash}:{intent_hash}"
    proposed = BootstrapImportRun(
        import_run_id=run_id,
        idempotency_key=idempotency_key,
        source_manifest_hash=source_hash,
        status="BUILDING",
        started_at=_utc_now(clock),
    )
    completion: BootstrapCompletion | None = None
    observed = repository.begin_import_run(scope, proposed)
    _validate_observed_run(observed, proposed)
    if observed.status == "COMPLETED":
        completion = _completion_from_run(observed, genesis=genesis, archive=archive)
        return BootstrapResult(completion=completion, idempotent_replay=True)

    _hit(kill_hook, BootstrapPhase.BUILDING_COMMITTED)
    registry_entries = _registry_entries(archive)
    replay = False
    with repository.import_transaction(scope) as transaction:
        locked = transaction.lock_import_run(run_id)
        _validate_observed_run(locked, proposed)
        if locked.status == "COMPLETED":
            completion = _completion_from_run(locked, genesis=genesis, archive=archive)
            replay = True
        else:
            transaction.assert_genesis_target_empty()
            transaction.register_legacy_archive(registry_entries)
            _hit(kill_hook, BootstrapPhase.ARCHIVE_REGISTERED)

            transaction.put_object_versions(genesis.object_versions)
            _hit(kill_hook, BootstrapPhase.OBJECT_VERSIONS_STAGED)

            transaction.put_revision(genesis.revision, import_run_id=run_id)
            _hit(kill_hook, BootstrapPhase.REVISION_STAGED)

            transaction.put_memberships(genesis.revision.revision_id, genesis.memberships)
            _hit(kill_hook, BootstrapPhase.MEMBERSHIPS_STAGED)

            transaction.initialize_release_pointer(genesis.release_pointer)
            _hit(kill_hook, BootstrapPhase.AUTHORING_HEAD_STAGED)

            completion = BootstrapCompletion(
                import_run_id=run_id,
                source_manifest_hash=source_hash,
                resolved_legacy_version=genesis.resolved_legacy_version,
                genesis_revision_id=genesis.revision.revision_id,
                genesis_manifest_hash=genesis.revision.manifest_hash,
                archive_id=archive.archive_id,
                archive_sha256=archive.archive_sha256,
                object_version_count=len(genesis.object_versions),
                membership_count=len(genesis.memberships),
                completed_at=_utc_now(clock),
            )
            transaction.record_genesis_imported(
                completion,
                actor_id=genesis.revision.created_by,
            )
            _hit(kill_hook, BootstrapPhase.OPERATION_EVENT_STAGED)
            transaction.complete_import_run(completion)
            _hit(kill_hook, BootstrapPhase.COMPLETED_STAGED)

    if completion is None:  # pragma: no cover - defensive protocol assertion
        raise BootstrapProtocolError("import transaction returned without a completion")
    _hit(kill_hook, BootstrapPhase.TRANSACTION_COMMITTED)
    return BootstrapResult(completion=completion, idempotent_replay=replay)


def bootstrap_from_legacy_archive(
    *,
    repository: BootstrapRepository,
    scope: OwnershipScope,
    archive: LegacyArchivePackage,
    legacy_version: str | None,
    created_at: datetime,
    created_by: str,
    kill_hook: KillHook | None = None,
    now: Callable[[], datetime] | None = None,
    scratch_parent: str | Path | None = None,
) -> BootstrapResult:
    """Formal cutover entry: derive genesis only from verified restored bytes.

    This is the CLI/product path.  It makes an archive/plan A/B substitution
    impossible by restoring the already checksummed archive, resolving its
    ``content/`` tree, and binding the resulting plan to the archive manifest
    hash before any BUILDING row is created.
    """

    archive.require_cutover_safe()
    parent = (
        Path(scratch_parent).resolve()
        if scratch_parent is not None
        else archive.archive_path.parent
    )
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bootstrap-restore-", dir=parent) as temporary:
        restored = Path(temporary) / "verified"
        restore_legacy_archive(
            archive.archive_path,
            restored,
            expected_archive_sha256=archive.archive_sha256,
        )
        snapshot = resolve_legacy_canon(
            ContentStore(restored / "content"),
            version=legacy_version,
        )
        genesis = map_genesis(
            snapshot,
            created_at=created_at,
            created_by=created_by,
            source_manifest_hash=archive.manifest.source_manifest_hash,
        )
        return bootstrap_genesis(
            repository=repository,
            scope=scope,
            genesis=genesis,
            archive=archive,
            kill_hook=kill_hook,
            now=now,
        )


def _validate_genesis(genesis: GenesisPlan) -> None:
    if type(genesis.resolved_legacy_version) is not str or not genesis.resolved_legacy_version:
        raise BootstrapProtocolError("genesis resolved legacy version must be non-empty")
    if genesis.revision.parent_revision is not None:
        raise BootstrapProtocolError("bootstrap revision must be a parentless genesis")
    if genesis.release_pointer.authoring_head != genesis.revision.revision_id:
        raise BootstrapProtocolError("genesis authoring pointer must target the imported revision")
    if genesis.release_pointer.serving_head is not None:
        raise BootstrapProtocolError("genesis must not become serving content during bootstrap")
    try:
        checked_revision = ContentRevision.model_validate(genesis.revision)
        checked_memberships = tuple(
            RevisionMembership.model_validate(item) for item in genesis.memberships
        )
        checked_objects = tuple(
            ContentObjectVersion.model_validate(item) for item in genesis.object_versions
        )
    except (TypeError, ValueError) as exc:
        raise BootstrapProtocolError("genesis contains an invalid N1.1 contract") from exc
    membership_ids = [item.logical_id for item in checked_memberships]
    if membership_ids != sorted(membership_ids) or len(membership_ids) != len(set(membership_ids)):
        raise BootstrapProtocolError("genesis memberships must be unique and canonically ordered")
    version_ids = [item.logical_id for item in checked_objects]
    if version_ids != sorted(version_ids) or len(version_ids) != len(set(version_ids)):
        raise BootstrapProtocolError(
            "genesis object versions must be unique and canonically ordered"
        )
    if revision_manifest_hash(checked_memberships) != checked_revision.manifest_hash:
        raise BootstrapProtocolError("genesis revision manifest_hash does not match memberships")
    objects_by_id = {item.object_version_id: item for item in checked_objects}
    live = [item for item in checked_memberships if not item.tombstone]
    live_version_ids = {item.object_version_id for item in live}
    if None in live_version_ids or live_version_ids != set(objects_by_id):
        raise BootstrapProtocolError(
            "genesis objects must exactly equal all live membership object versions"
        )
    for membership in live:
        if membership.object_version_id is None:  # guarded by the strict contract; narrows type
            raise BootstrapProtocolError("live genesis membership has no object version")
        object_version = objects_by_id[membership.object_version_id]
        if object_version.logical_id != membership.logical_id:
            raise BootstrapProtocolError("genesis live membership logical_id does not match object")


def _bootstrap_intent_hash(
    *,
    genesis: GenesisPlan,
    archive: LegacyArchivePackage,
) -> str:
    """Bind retry identity to every caller-controlled immutable outcome fact."""

    payload = {
        "archive_id": archive.archive_id,
        "archive_sha256": archive.archive_sha256,
        "membership_count": len(genesis.memberships),
        "object_version_count": len(genesis.object_versions),
        "resolved_legacy_version": genesis.resolved_legacy_version,
        "revision": genesis.revision.model_dump(mode="json"),
        "source_manifest_hash": archive.manifest.source_manifest_hash,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _run_id(
    scope: OwnershipScope,
    source_manifest_hash: str,
    intent_hash: str,
) -> str:
    name = f"{scope.tenant_id}/{scope.container_id}/{source_manifest_hash}/{intent_hash}"
    return str(uuid.uuid5(_RUN_NAMESPACE, name))


def _advisory_key(scope: OwnershipScope) -> int:
    payload = f"owcopilot/bootstrap/{scope.tenant_id}/{scope.container_id}".encode("ascii")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big", signed=True)


def _validate_observed_run(
    observed: BootstrapImportRun,
    proposed: BootstrapImportRun,
) -> None:
    if (
        observed.import_run_id != proposed.import_run_id
        or observed.idempotency_key != proposed.idempotency_key
        or observed.source_manifest_hash != proposed.source_manifest_hash
    ):
        raise BootstrapProtocolError("repository returned a run for a different bootstrap intent")
    if observed.status not in {"BUILDING", "COMPLETED"}:
        raise BootstrapProtocolError(f"unsupported bootstrap status: {observed.status!r}")
    if observed.status == "COMPLETED":
        if observed.finished_at is None or observed.genesis_revision_id is None:
            raise BootstrapProtocolError("COMPLETED import run is missing completion fields")
    elif observed.finished_at is not None or observed.genesis_revision_id is not None:
        raise BootstrapProtocolError("BUILDING import run carries impossible completion fields")


def _completion_from_run(
    run: BootstrapImportRun,
    *,
    genesis: GenesisPlan,
    archive: LegacyArchivePackage,
) -> BootstrapCompletion:
    if run.genesis_revision_id != genesis.revision.revision_id:
        raise BootstrapProtocolError(
            "same source manifest previously completed with a different genesis revision"
        )
    if run.finished_at is None:
        raise BootstrapProtocolError("completed import run has no finished_at")
    return BootstrapCompletion(
        import_run_id=run.import_run_id,
        source_manifest_hash=run.source_manifest_hash,
        resolved_legacy_version=genesis.resolved_legacy_version,
        genesis_revision_id=genesis.revision.revision_id,
        genesis_manifest_hash=genesis.revision.manifest_hash,
        archive_id=archive.archive_id,
        archive_sha256=archive.archive_sha256,
        object_version_count=len(genesis.object_versions),
        membership_count=len(genesis.memberships),
        completed_at=run.finished_at,
    )


def _registry_entries(archive: LegacyArchivePackage) -> tuple[LegacyRegistryEntry, ...]:
    manifest_json = archive.manifest.to_json()
    rows: list[LegacyRegistryEntry] = [
        LegacyRegistryEntry(
            archive_id=archive.archive_id,
            relative_path="legacy-archive.zip",
            entry_kind="file",
            sha256=archive.archive_sha256,
            byte_size=archive.byte_size,
            record_class="archive-only",
            disposition="restorable_archive_bundle",
            source_status="",
        ),
        LegacyRegistryEntry(
            archive_id=archive.archive_id,
            relative_path="legacy-manifest.json",
            entry_kind="directory_manifest",
            sha256=archive.manifest.source_manifest_hash,
            byte_size=len(manifest_json.encode("utf-8")),
            record_class="archive-only",
            disposition="deterministic_directory_manifest",
            source_status="",
        ),
    ]
    rows.extend(
        LegacyRegistryEntry(
            archive_id=archive.archive_id,
            relative_path=entry.relative_path,
            entry_kind="file",
            sha256=entry.sha256,
            byte_size=entry.byte_size,
            record_class="archive-only",
            disposition="preserve_read_only",
            source_status="",
        )
        for entry in archive.manifest.files
    )
    for index, record in enumerate(archive.manifest.sqlite_records):
        primary_key_hash = hashlib.sha256(record.primary_key_json.encode("utf-8")).hexdigest()
        relative = (
            f"sqlite-record/{record.database_label}/{record.table}/{index:08d}-{primary_key_hash}"
        )
        rows.append(
            LegacyRegistryEntry(
                archive_id=archive.archive_id,
                relative_path=relative,
                entry_kind="sqlite_record",
                sha256=record.record_sha256,
                byte_size=0,
                record_class=record.record_class,
                disposition=record.disposition,
                source_status=record.source_status,
            )
        )
    ordered = tuple(sorted(rows, key=lambda item: item.relative_path))
    paths = [entry.relative_path for entry in ordered]
    if len(paths) != len(set(paths)):
        raise BootstrapProtocolError("archive registry contains duplicate relative paths")
    return ordered


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise BootstrapProtocolError("bootstrap clock must return a timezone-aware UTC datetime")
    return value


def _hit(hook: KillHook | None, phase: BootstrapPhase) -> None:
    if hook is not None:
        hook(phase)


__all__ = [
    "BootstrapCompletion",
    "BootstrapImportRun",
    "BootstrapPhase",
    "BootstrapProtocolError",
    "BootstrapRepository",
    "BootstrapResult",
    "BootstrapTransaction",
    "LegacyRegistryEntry",
    "PostgresBootstrapRepository",
    "bootstrap_from_legacy_archive",
    "bootstrap_genesis",
]
