"""Real PostgreSQL acceptance tests for the N1.2 team canonical cutover.

These tests never substitute SQLite for the team database.  The core suite is
skipped only when one of the two explicit PostgreSQL URLs is absent; the
privileged role-fault test additionally requires an explicit admin URL.  Once a
URL is configured, an unreachable database or mismatched schema is a failure.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, func, insert, select, text, update
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.exc import DBAPIError

from owcopilot.content.models import (
    ContentBundle,
    Entity,
    EntityType,
    Relation,
    SourceRef,
)
from owcopilot.content.store import ContentStore
from owcopilot.contracts import (
    ContentObjectVersion,
    ContentRevision,
    OwnershipScope,
    RevisionMembership,
)
from owcopilot.contracts.revisions import revision_manifest_hash
from owcopilot.platform import Principal, Role, mint_token
from owcopilot.service.api import create_app
from owcopilot.teamdb import (
    RevisionConflict,
    TeamAuthorizationError,
    TeamDatabase,
    TeamDatabaseConfig,
    TeamNotFoundError,
    TeamRepository,
    TeamSchemaMismatch,
)
from owcopilot.teamdb.bootstrap import (
    PostgresBootstrapRepository,
    bootstrap_from_legacy_archive,
    bootstrap_genesis,
)
from owcopilot.teamdb.canonical_mapping import (
    GenesisPlan,
    map_genesis,
    relation_set_logical_id,
    resolve_legacy_canon,
)
from owcopilot.teamdb.legacy_archive import (
    LegacyArchivePackage,
    create_legacy_archive,
    restore_legacy_archive,
)
from owcopilot.teamdb.schema import (
    EXPECTED_ALEMBIC_HEAD,
    MIGRATION_ROLE,
    RUNTIME_ROLE,
    bootstrap_import_runs,
    containers,
    content_object_versions,
    content_revisions,
    legacy_archive_registry,
    memberships,
    operation_events,
    release_pointers,
    revision_memberships,
    tenants,
)

pytest.importorskip("psycopg")

_MIGRATION_URL_ENV = "OWCOPILOT_MIGRATION_DATABASE_URL"
_APP_URL_ENV = "OWCOPILOT_TEAM_DATABASE_URL"
_ADMIN_URL_ENV = "OWCOPILOT_ADMIN_DATABASE_URL"
_NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
_TABLES = (
    "tenants",
    "memberships",
    "containers",
    "bootstrap_import_runs",
    "legacy_archive_registry",
    "content_object_versions",
    "content_revisions",
    "revision_memberships",
    "release_pointers",
    "operation_events",
    "source_artifacts",
    "artifact_versions",
    "intake_runs",
    "source_segments",
    "raw_triage_outputs",
    "triage_verdicts",
    "human_triage_decisions",
    "rights_declarations",
    "allowed_uses",
    "lineage_anchors",
)
_APP_GRANTS = {
    "tenants": {"SELECT"},
    "memberships": {"SELECT"},
    "containers": {"SELECT"},
    "bootstrap_import_runs": {"SELECT", "INSERT", "UPDATE"},
    "legacy_archive_registry": {"SELECT", "INSERT"},
    "content_object_versions": {"SELECT", "INSERT"},
    "content_revisions": {"SELECT", "INSERT"},
    "revision_memberships": {"SELECT", "INSERT"},
    "release_pointers": {"SELECT", "INSERT", "UPDATE"},
    "operation_events": {"SELECT", "INSERT"},
    "source_artifacts": {"SELECT", "INSERT", "UPDATE"},
    "artifact_versions": {"SELECT", "INSERT"},
    "intake_runs": {"SELECT", "INSERT", "UPDATE"},
    "source_segments": {"SELECT", "INSERT"},
    "raw_triage_outputs": {"SELECT", "INSERT"},
    "triage_verdicts": {"SELECT", "INSERT"},
    "human_triage_decisions": {"SELECT", "INSERT"},
    "rights_declarations": {"SELECT", "INSERT"},
    "allowed_uses": {"SELECT", "INSERT"},
    "lineage_anchors": {"SELECT", "INSERT"},
}


@dataclass(frozen=True, slots=True)
class _PgEnvironment:
    migration_url: URL
    app_url: URL
    migration_engine: Engine
    app_engine: Engine

    def database(self, *, pool_size: int = 2, max_overflow: int = 0) -> TeamDatabase:
        return TeamDatabase(
            TeamDatabaseConfig(
                database_url=self.app_url,
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_timeout_seconds=5,
            )
        )


@dataclass(frozen=True, slots=True)
class _Seed:
    scope: OwnershipScope
    actor: str


@dataclass(frozen=True, slots=True)
class _BootstrapMaterial:
    content_root: Path
    sqlite_path: Path
    archive: LegacyArchivePackage
    plan: GenesisPlan


@pytest.fixture(scope="session")
def pg_environment() -> Iterator[_PgEnvironment]:
    migration_raw = os.environ.get(_MIGRATION_URL_ENV)
    app_raw = os.environ.get(_APP_URL_ENV)
    if not migration_raw or not app_raw:
        pytest.skip(f"real PostgreSQL tests require {_MIGRATION_URL_ENV} and {_APP_URL_ENV}")

    migration_url = make_url(migration_raw)
    app_url = make_url(app_raw)
    if migration_url.drivername != "postgresql+psycopg" or app_url.drivername != (
        "postgresql+psycopg"
    ):
        pytest.fail("N1.2 integration URLs must both use postgresql+psycopg")
    if migration_url.username != MIGRATION_ROLE or app_url.username != RUNTIME_ROLE:
        pytest.fail("N1.2 integration URLs use the wrong PostgreSQL roles")

    migration_engine = create_engine(migration_url, pool_pre_ping=True)
    app_engine = create_engine(app_url, pool_pre_ping=True)
    environment = _PgEnvironment(
        migration_url=migration_url,
        app_url=app_url,
        migration_engine=migration_engine,
        app_engine=app_engine,
    )
    try:
        with migration_engine.connect() as connection:
            observed = connection.execute(
                text(
                    "SELECT current_user, current_database(), "
                    "current_setting('server_version_num')::integer"
                )
            ).one()
        assert observed[0] == MIGRATION_ROLE
        assert int(observed[2]) // 10_000 == 17
        database = environment.database()
        try:
            health = database.health()
            assert health.ready, health.as_dict()
            assert health.schema_head == EXPECTED_ALEMBIC_HEAD
        finally:
            database.close()
        yield environment
    finally:
        app_engine.dispose()
        migration_engine.dispose()


def _uuid() -> str:
    return str(uuid4())


def _seed_scope(
    environment: _PgEnvironment,
    *,
    tenant_id: str | None = None,
    actor: str | None = None,
) -> _Seed:
    resolved_tenant = tenant_id or _uuid()
    resolved_actor = actor or _uuid()
    container_id = _uuid()
    suffix = uuid4().hex
    with environment.migration_engine.begin() as connection:
        if tenant_id is None:
            connection.execute(
                insert(tenants).values(
                    tenant_id=UUID(resolved_tenant),
                    slug=f"pg-it-tenant-{suffix}",
                    display_name=f"Synthetic tenant {suffix}",
                )
            )
            connection.execute(
                insert(memberships).values(
                    tenant_id=UUID(resolved_tenant),
                    subject_id=UUID(resolved_actor),
                    role="owner",
                    active=True,
                )
            )
        connection.execute(
            insert(containers).values(
                tenant_id=UUID(resolved_tenant),
                container_id=UUID(container_id),
                slug=f"pg-it-container-{suffix}",
                display_name=f"Synthetic container {suffix}",
            )
        )
    return _Seed(
        scope=OwnershipScope(
            tenant_id=resolved_tenant,
            container_id=container_id,
        ),
        actor=resolved_actor,
    )


@contextmanager
def _app_scope(
    environment: _PgEnvironment,
    scope: OwnershipScope,
) -> Iterator[Connection]:
    with environment.app_engine.begin() as connection:
        observed = connection.execute(
            text(
                "SELECT "
                "set_config('owcopilot.tenant_id', :tenant_id, true), "
                "set_config('owcopilot.container_id', :container_id, true), "
                "set_config('TimeZone', 'UTC', true)"
            ),
            {
                "tenant_id": scope.tenant_id,
                "container_id": scope.container_id,
            },
        ).one()
        assert tuple(str(item) for item in observed) == (
            scope.tenant_id,
            scope.container_id,
            "UTC",
        )
        yield connection


def _make_terminal_sqlite(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE patches (id TEXT PRIMARY KEY, status TEXT NOT NULL);
            CREATE TABLE telemetry (id INTEGER PRIMARY KEY, task_type TEXT NOT NULL);
            INSERT INTO patches (id, status) VALUES ('patch-1', 'applied');
            INSERT INTO telemetry (id, task_type) VALUES (1, 'draft');
            """
        )
        connection.commit()
    finally:
        connection.close()


def _bootstrap_material(tmp_path: Path, actor: str) -> _BootstrapMaterial:
    content_root = tmp_path / "synthetic-frozen-content"
    store = ContentStore(content_root)
    live_relation = Relation(
        source="keeper",
        target="gate",
        kind="guards",
        metadata={"rank": 2},
        source_ref=SourceRef(path="synthetic/import.xlsx", sheet="Relations", row=7),
    )
    removed_relation = Relation(source="keeper", target="removed", kind="knows")
    baseline = ContentBundle(
        entities={
            "keeper": Entity(id="keeper", name="守门人", type=EntityType.NPC),
            "removed": Entity(id="removed", name="旧人", type=EntityType.NPC),
        },
        relations=[live_relation, live_relation.model_copy(deep=True), removed_relation],
    )
    store.save(baseline)
    store.create_version("v2", base_version="v1")
    resolved = baseline.model_copy(deep=True)
    del resolved.entities["removed"]
    resolved.relations = resolved.relations[:2]
    store.save_scoped(resolved, version="v2")

    sqlite_path = tmp_path / "synthetic-terminal.sqlite3"
    _make_terminal_sqlite(sqlite_path)
    archive = create_legacy_archive(
        content_root=content_root,
        sqlite_databases={"runtime": sqlite_path},
        archive_path=tmp_path / "synthetic-legacy.zip",
    )
    snapshot = resolve_legacy_canon(ContentStore(content_root), version="v2")
    plan = map_genesis(
        snapshot,
        created_at=_NOW,
        created_by=actor,
        source_manifest_hash=archive.manifest.source_manifest_hash,
    )
    return _BootstrapMaterial(
        content_root=content_root,
        sqlite_path=sqlite_path,
        archive=archive,
        plan=plan,
    )


def _bootstrap(
    environment: _PgEnvironment,
    seed: _Seed,
    material: _BootstrapMaterial,
):
    database = environment.database()
    try:
        repository = PostgresBootstrapRepository(database, actor_subject=seed.actor)
        return bootstrap_from_legacy_archive(
            repository=repository,
            scope=seed.scope,
            archive=material.archive,
            legacy_version="v2",
            created_at=_NOW,
            created_by=seed.actor,
            scratch_parent=material.content_root.parent,
            now=lambda: _NOW,
        )
    finally:
        database.close()


def _read_canonical(environment: _PgEnvironment, seed: _Seed):
    database = environment.database()
    try:
        return TeamRepository(database).read_canonical(
            seed.scope,
            subject_id=seed.actor,
        )
    finally:
        database.close()


def _write_followup_revision(environment: _PgEnvironment, seed: _Seed):
    database = environment.database()
    repository = TeamRepository(database)
    try:
        current = repository.read_canonical(seed.scope, subject_id=seed.actor)
        added = ContentObjectVersion.from_payload(
            logical_id="entity:followup",
            object_type="entity",
            payload={"id": "followup", "name": "后续对象"},
        )
        retained_memberships = list(current.memberships)
        retained_memberships.append(
            RevisionMembership.live(
                logical_id=added.logical_id,
                object_version_id=added.object_version_id,
            )
        )
        revision = ContentRevision.from_memberships(
            parent_revision=current.revision.revision_id,
            memberships=retained_memberships,
            created_at=_NOW + timedelta(minutes=1),
            created_by=seed.actor,
        )
        state = repository.write_revision(
            seed.scope,
            revision=revision,
            memberships=retained_memberships,
            objects=[*current.objects, added],
            expected_authoring_head=current.revision.revision_id,
            expected_generation=current.release.generation,
            actor_subject=seed.actor,
        )
        return repository, database, current, revision, retained_memberships, added, state
    except BaseException:
        database.close()
        raise


def test_schema_roles_force_rls_policies_and_grants_match_registry(
    pg_environment: _PgEnvironment,
) -> None:
    with pg_environment.migration_engine.connect() as connection:
        roles = {
            str(row.rolname): row
            for row in connection.execute(
                text(
                    "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, "
                    "rolcanlogin, rolreplication, rolbypassrls "
                    "FROM pg_catalog.pg_roles "
                    "WHERE rolname IN ('owcopilot_migration', 'owcopilot_app')"
                )
            )
        }
        assert set(roles) == {MIGRATION_ROLE, RUNTIME_ROLE}
        for role in roles.values():
            assert role.rolcanlogin is True
            assert role.rolsuper is False
            assert role.rolinherit is False
            assert role.rolcreaterole is False
            assert role.rolcreatedb is False
            assert role.rolreplication is False
            assert role.rolbypassrls is False

        for role_name in (MIGRATION_ROLE, RUNTIME_ROLE):
            role_oid = connection.execute(
                text("SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :role"),
                {"role": role_name},
            ).scalar_one()
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_catalog.pg_auth_members "
                        "WHERE member = :oid OR roleid = :oid"
                    ),
                    {"oid": role_oid},
                ).scalar_one()
                == 0
            )

        table_rows = {
            str(row.relname): row
            for row in connection.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                    "owner_role.rolname AS owner_name "
                    "FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "JOIN pg_catalog.pg_roles owner_role ON owner_role.oid = c.relowner "
                    "WHERE n.nspname = current_schema() AND c.relkind = 'r'"
                )
            )
            if str(row.relname) in _TABLES
        }
        assert set(table_rows) == set(_TABLES)
        for row in table_rows.values():
            assert row.relrowsecurity is True
            assert row.relforcerowsecurity is True
            assert row.owner_name == MIGRATION_ROLE

        policy_pairs = {
            (str(row.tablename), str(row.policyname))
            for row in connection.execute(
                text(
                    "SELECT tablename, policyname FROM pg_catalog.pg_policies "
                    "WHERE schemaname = current_schema()"
                )
            )
            if str(row.tablename) in _TABLES
        }
        assert policy_pairs == {
            *((table, f"{table}_ownership_scope") for table in _TABLES),
            *((table, f"{table}_migration_owner") for table in _TABLES),
        }

        observed_grants: dict[str, set[str]] = {table: set() for table in _TABLES}
        for row in connection.execute(
            text(
                "SELECT table_name, privilege_type "
                "FROM information_schema.role_table_grants "
                "WHERE table_schema = current_schema() AND grantee = :role"
            ),
            {"role": RUNTIME_ROLE},
        ):
            if str(row.table_name) in observed_grants:
                observed_grants[str(row.table_name)].add(str(row.privilege_type))
        assert observed_grants == _APP_GRANTS
        alembic = connection.execute(
            text(
                "SELECT tableowner FROM pg_catalog.pg_tables "
                "WHERE schemaname = current_schema() AND tablename = 'alembic_version'"
            )
        ).scalar_one()
        assert alembic == MIGRATION_ROLE

    with pg_environment.app_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == EXPECTED_ALEMBIC_HEAD
        )


def test_health_fails_closed_for_role_inherit_and_both_membership_edge_directions(
    pg_environment: _PgEnvironment,
) -> None:
    raw_admin_url = os.environ.get(_ADMIN_URL_ENV)
    if not raw_admin_url:
        pytest.skip(f"real role fault injection requires {_ADMIN_URL_ENV}")
    try:
        admin_url = make_url(raw_admin_url)
    except Exception:
        pytest.fail(f"{_ADMIN_URL_ENV} is malformed", pytrace=False)
    if admin_url.drivername != "postgresql+psycopg":
        pytest.fail(f"{_ADMIN_URL_ENV} must use postgresql+psycopg", pytrace=False)

    admin_engine = create_engine(admin_url, pool_pre_ping=True)
    database = pg_environment.database(pool_size=1, max_overflow=0)
    temporary_role = f"owcopilot_health_it_{uuid4().hex[:24]}"

    def execute_admin(statement: str) -> None:
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(statement)

    def assert_health_rejected() -> None:
        health = database.health()
        assert health.database_available is True
        assert health.ready is False
        assert health.detail == "unsafe_runtime_role"
        with pytest.raises(TeamSchemaMismatch, match="unsafe_runtime_role"):
            database.require_ready()

    try:
        try:
            baseline = database.health()
            assert baseline.ready, baseline.as_dict()
            with admin_engine.connect() as connection:
                admin_capability = connection.execute(
                    text(
                        "SELECT rolsuper OR rolcreaterole FROM pg_catalog.pg_roles "
                        "WHERE rolname = current_user"
                    )
                ).scalar_one()
            assert admin_capability is True

            execute_admin(f"ALTER ROLE {RUNTIME_ROLE} INHERIT")
            try:
                assert_health_rejected()
            finally:
                execute_admin(f"ALTER ROLE {RUNTIME_ROLE} NOINHERIT")
            assert database.health().ready

            execute_admin(
                f"CREATE ROLE {temporary_role} NOLOGIN NOINHERIT NOSUPERUSER "
                "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            )

            # app is the member; the temporary role is the granted role.
            execute_admin(f"GRANT {temporary_role} TO {RUNTIME_ROLE}")
            try:
                assert_health_rejected()
            finally:
                execute_admin(f"REVOKE {temporary_role} FROM {RUNTIME_ROLE}")
            assert database.health().ready

            # app is roleid; the temporary role is the member.  This direction
            # cannot elevate app today, but health rejects all unexpected role
            # graph edges so a later role change cannot silently widen trust.
            execute_admin(f"GRANT {RUNTIME_ROLE} TO {temporary_role}")
            try:
                assert_health_rejected()
            finally:
                execute_admin(f"REVOKE {RUNTIME_ROLE} FROM {temporary_role}")
            assert database.health().ready
        finally:
            with admin_engine.begin() as connection:
                connection.exec_driver_sql(f"ALTER ROLE {RUNTIME_ROLE} NOINHERIT")
                temporary_exists = connection.execute(
                    text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
                    {"role": temporary_role},
                ).scalar_one()
                if temporary_exists:
                    connection.exec_driver_sql(f"REVOKE {temporary_role} FROM {RUNTIME_ROLE}")
                    connection.exec_driver_sql(f"REVOKE {RUNTIME_ROLE} FROM {temporary_role}")
                    connection.exec_driver_sql(f"DROP ROLE {temporary_role}")
            final_health = database.health()
            assert final_health.ready, final_health.as_dict()
    finally:
        database.close()
        admin_engine.dispose()


def test_membership_auth_same_tenant(pg_environment: _PgEnvironment) -> None:
    seed = _seed_scope(pg_environment)
    database = pg_environment.database()
    try:
        grant = TeamRepository(database).authenticate_membership(
            seed.scope.tenant_id,
            seed.actor,
        )
    finally:
        database.close()
    assert grant.as_dict() == {
        "tenant_id": seed.scope.tenant_id,
        "subject_id": seed.actor,
        "role": "owner",
    }


def test_membership_auth_cross_tenant_denied(pg_environment: _PgEnvironment) -> None:
    first = _seed_scope(pg_environment)
    second = _seed_scope(pg_environment)
    database = pg_environment.database()
    try:
        repository = TeamRepository(database)
        with pytest.raises(TeamAuthorizationError, match="no active membership"):
            repository.authenticate_membership(second.scope.tenant_id, first.actor)
    finally:
        database.close()


def test_repository_mixed_tenant_container_read_and_write_are_rejected(
    pg_environment: _PgEnvironment,
    tmp_path: Path,
) -> None:
    first = _seed_scope(pg_environment)
    second = _seed_scope(pg_environment, actor=first.actor)
    first_material = _bootstrap_material(tmp_path / "tenant-a", first.actor)
    second_material = _bootstrap_material(tmp_path / "tenant-b", second.actor)
    _bootstrap(pg_environment, first, first_material)
    _bootstrap(pg_environment, second, second_material)
    database = pg_environment.database()
    repository = TeamRepository(database)
    try:
        first_state = repository.read_canonical(first.scope, subject_id=first.actor)
        second_state = repository.read_canonical(second.scope, subject_id=second.actor)
        assert first_state.revision == first_material.plan.revision
        assert second_state.revision == second_material.plan.revision

        mixed_scope = OwnershipScope(
            tenant_id=first.scope.tenant_id,
            container_id=second.scope.container_id,
        )
        # The subject is an active owner in tenant A, so this reaches the
        # container/pointer lookup instead of being rejected by an earlier
        # missing-membership check.
        with pytest.raises(TeamNotFoundError):
            repository.read_canonical(mixed_scope, subject_id=first.actor)

        tombstone = RevisionMembership.deleted(logical_id="entity:mixed-scope")
        attempted = ContentRevision.from_memberships(
            parent_revision=first_state.revision.revision_id,
            memberships=[tombstone],
            created_at=_NOW + timedelta(minutes=2),
            created_by=first.actor,
        )
        with pytest.raises(TeamNotFoundError):
            repository.write_revision(
                mixed_scope,
                revision=attempted,
                memberships=[tombstone],
                objects=[],
                expected_authoring_head=first_state.revision.revision_id,
                expected_generation=first_state.release.generation,
                actor_subject=first.actor,
            )
    finally:
        database.close()


def test_same_scope_canonical_round_trip(
    pg_environment: _PgEnvironment,
    tmp_path: Path,
) -> None:
    seed = _seed_scope(pg_environment)
    material = _bootstrap_material(tmp_path, seed.actor)
    result = _bootstrap(pg_environment, seed, material)
    state = _read_canonical(pg_environment, seed)

    assert not result.idempotent_replay
    assert result.completion.genesis_revision_id == material.plan.revision.revision_id
    assert state.revision == material.plan.revision
    assert state.memberships == material.plan.memberships
    assert state.objects == material.plan.object_versions
    assert state.release.pointer.authoring_head == material.plan.revision.revision_id
    assert state.release.pointer.serving_head is None
    assert state.release.generation == 0
    assert state.revision.manifest_hash == revision_manifest_hash(state.memberships)
    tombstones = {item.logical_id for item in state.memberships if item.tombstone}
    assert "entity:removed" in tombstones
    assert relation_set_logical_id("relation:keeper:knows:removed") in tombstones
    relation_object = next(item for item in state.objects if item.object_type == "relation_set")
    relation_payload = json.loads(relation_object.payload_json)
    assert len(relation_payload["relations"]) == 2
    assert relation_payload["relations"][0]["source_ref"] == {
        "path": "synthetic/import.xlsx",
        "row": 7,
        "sheet": "Relations",
    }


def test_cross_container_isolation(pg_environment: _PgEnvironment) -> None:
    first = _seed_scope(pg_environment)
    second = _seed_scope(
        pg_environment,
        tenant_id=first.scope.tenant_id,
        actor=first.actor,
    )
    with _app_scope(pg_environment, first.scope) as connection:
        visible = connection.execute(select(containers.c.container_id)).scalars().all()
        explicit_cross_scope = (
            connection.execute(
                select(containers.c.container_id).where(
                    containers.c.container_id == UUID(second.scope.container_id)
                )
            )
            .scalars()
            .all()
        )
    assert visible == [UUID(first.scope.container_id)]
    assert explicit_cross_scope == []


def test_bootstrap_idempotent_replay(
    pg_environment: _PgEnvironment,
    tmp_path: Path,
) -> None:
    seed = _seed_scope(pg_environment)
    material = _bootstrap_material(tmp_path, seed.actor)
    first = _bootstrap(pg_environment, seed, material)
    with _app_scope(pg_environment, seed.scope) as connection:
        before = {
            table.name: connection.execute(select(func.count()).select_from(table)).scalar_one()
            for table in (
                bootstrap_import_runs,
                legacy_archive_registry,
                content_object_versions,
                content_revisions,
                revision_memberships,
                release_pointers,
                operation_events,
            )
        }
    second = _bootstrap(pg_environment, seed, material)
    with _app_scope(pg_environment, seed.scope) as connection:
        after = {
            table.name: connection.execute(select(func.count()).select_from(table)).scalar_one()
            for table in (
                bootstrap_import_runs,
                legacy_archive_registry,
                content_object_versions,
                content_revisions,
                revision_memberships,
                release_pointers,
                operation_events,
            )
        }
    assert not first.idempotent_replay
    assert second.idempotent_replay
    assert second.completion == first.completion
    assert after == before


def test_bootstrap_process_kill_before_commit_never_exposes_partial_genesis(
    pg_environment: _PgEnvironment,
    tmp_path: Path,
) -> None:
    seed = _seed_scope(pg_environment)
    material = _bootstrap_material(tmp_path, seed.actor)
    child = textwrap.dedent(
        """
        import os
        import sys
        from datetime import datetime
        from pathlib import Path

        from owcopilot.content.store import ContentStore
        from owcopilot.contracts import OwnershipScope
        from owcopilot.teamdb.bootstrap import (
            BootstrapPhase,
            PostgresBootstrapRepository,
            bootstrap_genesis,
        )
        from owcopilot.teamdb.canonical_mapping import map_genesis, resolve_legacy_canon
        from owcopilot.teamdb.legacy_archive import LegacyArchivePackage, verify_legacy_archive
        from owcopilot.teamdb.session import TeamDatabase

        content, archive_path, archive_id, archive_sha, tenant, container, actor = sys.argv[1:]
        archive = Path(archive_path)
        manifest = verify_legacy_archive(archive, expected_archive_sha256=archive_sha)
        package = LegacyArchivePackage(
            archive_path=archive,
            archive_id=archive_id,
            archive_sha256=archive_sha,
            byte_size=archive.stat().st_size,
            manifest=manifest,
        )
        plan = map_genesis(
            resolve_legacy_canon(ContentStore(Path(content)), version="v2"),
            created_at=datetime.fromisoformat("2026-08-03T12:00:00+00:00"),
            created_by=actor,
            source_manifest_hash=manifest.source_manifest_hash,
        )
        database = TeamDatabase.from_env()
        repository = PostgresBootstrapRepository(database, actor_subject=actor)

        def kill(phase):
            if phase is BootstrapPhase.MEMBERSHIPS_STAGED:
                os._exit(97)

        bootstrap_genesis(
            repository=repository,
            scope=OwnershipScope(tenant_id=tenant, container_id=container),
            genesis=plan,
            archive=package,
            kill_hook=kill,
            now=lambda: datetime.fromisoformat("2026-08-03T12:00:00+00:00"),
        )
        raise SystemExit(98)
        """
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            str(material.content_root),
            str(material.archive.archive_path),
            material.archive.archive_id,
            material.archive.archive_sha256,
            seed.scope.tenant_id,
            seed.scope.container_id,
            seed.actor,
        ],
        check=False,
        cwd=Path(__file__).resolve().parents[2],
        env=os.environ.copy(),
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == 97, completed.stderr.decode("utf-8", errors="replace")

    with _app_scope(pg_environment, seed.scope) as connection:
        assert (
            connection.execute(select(func.count()).select_from(bootstrap_import_runs)).scalar_one()
            == 1
        )
        status = connection.execute(select(bootstrap_import_runs.c.status)).scalar_one()
        assert status == "building"
        for table in (
            legacy_archive_registry,
            content_object_versions,
            content_revisions,
            revision_memberships,
            release_pointers,
            operation_events,
        ):
            assert connection.execute(select(func.count()).select_from(table)).scalar_one() == 0

    database = pg_environment.database()
    try:
        recovered = bootstrap_genesis(
            repository=PostgresBootstrapRepository(database, actor_subject=seed.actor),
            scope=seed.scope,
            genesis=material.plan,
            archive=material.archive,
            now=lambda: _NOW,
        )
    finally:
        database.close()
    assert not recovered.idempotent_replay
    assert _read_canonical(pg_environment, seed).revision == material.plan.revision


def test_bootstrap_identity_fields_cannot_mutate(
    pg_environment: _PgEnvironment,
    tmp_path: Path,
) -> None:
    seed = _seed_scope(pg_environment)
    material = _bootstrap_material(tmp_path, seed.actor)
    _bootstrap(pg_environment, seed, material)
    with pytest.raises(DBAPIError):
        with _app_scope(pg_environment, seed.scope) as connection:
            connection.execute(
                update(bootstrap_import_runs).values(idempotency_key="mutated-identity")
            )


def test_legacy_archive_registry_round_trip(
    pg_environment: _PgEnvironment,
    tmp_path: Path,
) -> None:
    seed = _seed_scope(pg_environment)
    material = _bootstrap_material(tmp_path, seed.actor)
    _bootstrap(pg_environment, seed, material)
    with _app_scope(pg_environment, seed.scope) as connection:
        rows = (
            connection.execute(
                select(legacy_archive_registry).order_by(legacy_archive_registry.c.relative_path)
            )
            .mappings()
            .all()
        )
    expected_count = (
        2 + len(material.archive.manifest.files) + len(material.archive.manifest.sqlite_records)
    )
    assert len(rows) == expected_count
    identities = {str(row["relative_path"]): str(row["sha256"]) for row in rows}
    assert identities["legacy-archive.zip"] == material.archive.archive_sha256
    assert identities["legacy-manifest.json"] == (material.archive.manifest.source_manifest_hash)
    for entry in material.archive.manifest.files:
        assert identities[entry.relative_path] == entry.sha256

    restored = tmp_path / "restored-custody"
    restored_manifest = restore_legacy_archive(
        material.archive.archive_path,
        restored,
        expected_archive_sha256=material.archive.archive_sha256,
    )
    assert restored_manifest == material.archive.manifest
    assert (restored / "sqlite" / "runtime.sqlite3").read_bytes() == (
        material.sqlite_path.read_bytes()
    )


def test_legacy_archive_registry_is_immutable(
    pg_environment: _PgEnvironment,
    tmp_path: Path,
) -> None:
    seed = _seed_scope(pg_environment)
    material = _bootstrap_material(tmp_path, seed.actor)
    _bootstrap(pg_environment, seed, material)
    for statement in (
        update(legacy_archive_registry)
        .where(
            legacy_archive_registry.c.tenant_id == UUID(seed.scope.tenant_id),
            legacy_archive_registry.c.container_id == UUID(seed.scope.container_id),
        )
        .values(source_status="mutated"),
        legacy_archive_registry.delete().where(
            legacy_archive_registry.c.tenant_id == UUID(seed.scope.tenant_id),
            legacy_archive_registry.c.container_id == UUID(seed.scope.container_id),
        ),
    ):
        with pytest.raises(DBAPIError):
            with pg_environment.migration_engine.begin() as connection:
                connection.execute(statement)


def test_cross_tenant_object_version_rejected(pg_environment: _PgEnvironment) -> None:
    first = _seed_scope(pg_environment)
    second = _seed_scope(pg_environment)
    item = ContentObjectVersion.from_payload(
        logical_id="entity:cross-tenant",
        object_type="entity",
        payload={"name": "must be rejected"},
    )
    with pytest.raises(DBAPIError):
        with _app_scope(pg_environment, first.scope) as connection:
            connection.execute(
                insert(content_object_versions).values(
                    tenant_id=UUID(second.scope.tenant_id),
                    container_id=UUID(second.scope.container_id),
                    logical_id=item.logical_id,
                    object_version_id=item.object_version_id,
                    object_type=item.object_type,
                    payload_json=item.payload_json,
                    payload_jsonb=json.loads(item.payload_json),
                    content_hash=item.content_hash,
                )
            )


def test_cross_tenant_revision_rejected(pg_environment: _PgEnvironment) -> None:
    first = _seed_scope(pg_environment)
    second = _seed_scope(pg_environment)
    tombstone = RevisionMembership.deleted(logical_id="entity:gone")
    revision = ContentRevision.from_memberships(
        parent_revision=None,
        memberships=[tombstone],
        created_at=_NOW,
        created_by=second.actor,
    )
    with pytest.raises(DBAPIError):
        with _app_scope(pg_environment, first.scope) as connection:
            connection.execute(
                insert(content_revisions).values(
                    tenant_id=UUID(second.scope.tenant_id),
                    container_id=UUID(second.scope.container_id),
                    revision_id=revision.revision_id,
                    parent_revision=None,
                    manifest_hash=revision.manifest_hash,
                    created_at=revision.created_at,
                    created_by=revision.created_by,
                    import_run_id=None,
                )
            )


def test_membership_object_fk_rejects_cross_scope(pg_environment: _PgEnvironment) -> None:
    first = _seed_scope(pg_environment)
    second = _seed_scope(pg_environment)
    item = ContentObjectVersion.from_payload(
        logical_id="entity:foreign-object",
        object_type="entity",
        payload={"name": "foreign"},
    )
    member = RevisionMembership.live(
        logical_id=item.logical_id,
        object_version_id=item.object_version_id,
    )
    revision = ContentRevision.from_memberships(
        parent_revision=None,
        memberships=[member],
        created_at=_NOW,
        created_by=first.actor,
    )
    with pg_environment.migration_engine.begin() as connection:
        connection.execute(
            insert(content_object_versions).values(
                tenant_id=UUID(second.scope.tenant_id),
                container_id=UUID(second.scope.container_id),
                logical_id=item.logical_id,
                object_version_id=item.object_version_id,
                object_type=item.object_type,
                payload_json=item.payload_json,
                payload_jsonb=json.loads(item.payload_json),
                content_hash=item.content_hash,
            )
        )
        connection.execute(
            insert(content_revisions).values(
                tenant_id=UUID(first.scope.tenant_id),
                container_id=UUID(first.scope.container_id),
                revision_id=revision.revision_id,
                parent_revision=None,
                manifest_hash=revision.manifest_hash,
                created_at=revision.created_at,
                created_by=revision.created_by,
                import_run_id=None,
            )
        )
    with pytest.raises(DBAPIError):
        with _app_scope(pg_environment, first.scope) as connection:
            connection.execute(
                insert(revision_memberships).values(
                    tenant_id=UUID(first.scope.tenant_id),
                    container_id=UUID(first.scope.container_id),
                    revision_id=revision.revision_id,
                    logical_id=member.logical_id,
                    object_version_id=member.object_version_id,
                    tombstone=False,
                )
            )


def test_release_pointer_cas(
    pg_environment: _PgEnvironment,
    tmp_path: Path,
) -> None:
    seed = _seed_scope(pg_environment)
    material = _bootstrap_material(tmp_path, seed.actor)
    _bootstrap(pg_environment, seed, material)
    repository, database, previous, revision, members, added, state = _write_followup_revision(
        pg_environment, seed
    )
    try:
        assert state.release.pointer.authoring_head == revision.revision_id
        assert state.release.generation == previous.release.generation + 1
        with pytest.raises(RevisionConflict):
            repository.write_revision(
                seed.scope,
                revision=revision,
                memberships=members,
                objects=[*previous.objects, added],
                expected_authoring_head=previous.revision.revision_id,
                expected_generation=previous.release.generation,
                actor_subject=seed.actor,
            )
    finally:
        database.close()


def test_cross_scope_release_pointer_rejected(pg_environment: _PgEnvironment) -> None:
    first = _seed_scope(pg_environment)
    second = _seed_scope(
        pg_environment,
        tenant_id=first.scope.tenant_id,
        actor=first.actor,
    )
    with pg_environment.migration_engine.begin() as connection:
        connection.execute(
            insert(release_pointers).values(
                tenant_id=UUID(second.scope.tenant_id),
                container_id=UUID(second.scope.container_id),
                authoring_head=None,
                serving_head=None,
                lock_version=0,
            )
        )
    with _app_scope(pg_environment, first.scope) as connection:
        result = connection.execute(
            update(release_pointers)
            .where(
                release_pointers.c.tenant_id == UUID(second.scope.tenant_id),
                release_pointers.c.container_id == UUID(second.scope.container_id),
            )
            .values(lock_version=9)
        )
        assert result.rowcount == 0
    with pg_environment.migration_engine.connect() as connection:
        observed = connection.execute(
            select(release_pointers.c.lock_version).where(
                release_pointers.c.tenant_id == UUID(second.scope.tenant_id),
                release_pointers.c.container_id == UUID(second.scope.container_id),
            )
        ).scalar_one()
    assert observed == 0


def test_operation_event_written_with_revision(
    pg_environment: _PgEnvironment,
    tmp_path: Path,
) -> None:
    seed = _seed_scope(pg_environment)
    material = _bootstrap_material(tmp_path, seed.actor)
    _bootstrap(pg_environment, seed, material)
    _repository, database, _previous, revision, _members, _added, _state = _write_followup_revision(
        pg_environment, seed
    )
    database.close()
    with _app_scope(pg_environment, seed.scope) as connection:
        event = (
            connection.execute(
                select(operation_events).where(
                    operation_events.c.revision_id == revision.revision_id,
                    operation_events.c.event_type == "canonical.revision.created",
                )
            )
            .mappings()
            .one()
        )
    assert str(event["actor_id"]) == seed.actor
    assert str(event["subject_id"]) == seed.actor
    assert event["payload_jsonb"]["previous_lock_version"] == 0


def test_operation_event_update_delete_rejected(
    pg_environment: _PgEnvironment,
    tmp_path: Path,
) -> None:
    seed = _seed_scope(pg_environment)
    material = _bootstrap_material(tmp_path, seed.actor)
    _bootstrap(pg_environment, seed, material)
    for statement in (
        update(operation_events)
        .where(
            operation_events.c.tenant_id == UUID(seed.scope.tenant_id),
            operation_events.c.container_id == UUID(seed.scope.container_id),
        )
        .values(event_type="mutated"),
        operation_events.delete().where(
            operation_events.c.tenant_id == UUID(seed.scope.tenant_id),
            operation_events.c.container_id == UUID(seed.scope.container_id),
        ),
    ):
        with pytest.raises(DBAPIError):
            with pg_environment.migration_engine.begin() as connection:
                connection.execute(statement)


def test_pool_reuse_sets_local_scope_and_utc_and_cleans_up_after_rollback(
    pg_environment: _PgEnvironment,
    tmp_path: Path,
) -> None:
    first = _seed_scope(pg_environment)
    second = _seed_scope(pg_environment)
    material = _bootstrap_material(tmp_path, first.actor)
    _bootstrap(pg_environment, first, material)
    database = pg_environment.database(pool_size=1, max_overflow=0)
    repository = TeamRepository(database)
    try:
        with database.engine.connect() as connection:
            connection.execute(text("SET SESSION TIME ZONE 'Europe/London'"))
            connection.commit()
            primed_pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()

        reconstructed = repository.read_canonical(first.scope, subject_id=first.actor)
        assert reconstructed.revision.created_at.utcoffset() == timedelta(0)
        with pytest.raises(RuntimeError, match="injected rollback"):
            with database.transaction(first.scope) as connection:
                assert (
                    connection.execute(text("SELECT current_setting('TimeZone')")).scalar_one()
                    == "UTC"
                )
                assert connection.execute(text("SELECT pg_backend_pid()")).scalar_one() == (
                    primed_pid
                )
                assert (
                    connection.execute(
                        text("SELECT current_setting('owcopilot.tenant_id', true)")
                    ).scalar_one()
                    == first.scope.tenant_id
                )
                raise RuntimeError("injected rollback")

        with database.transaction(second.scope) as connection:
            assert connection.execute(text("SELECT pg_backend_pid()")).scalar_one() == (primed_pid)
            assert (
                connection.execute(text("SELECT current_setting('TimeZone')")).scalar_one() == "UTC"
            )
            assert (
                connection.execute(
                    text("SELECT current_setting('owcopilot.tenant_id', true)")
                ).scalar_one()
                == second.scope.tenant_id
            )
            assert (
                connection.execute(
                    text("SELECT current_setting('owcopilot.container_id', true)")
                ).scalar_one()
                == second.scope.container_id
            )

        with database.engine.connect() as connection:
            residual = connection.execute(
                text(
                    "SELECT current_setting('owcopilot.tenant_id', true), "
                    "current_setting('owcopilot.container_id', true), "
                    "current_setting('TimeZone')"
                )
            ).one()
        assert residual[0] in {None, ""}
        assert residual[1] in {None, ""}
        assert residual[2] == "Europe/London"
    finally:
        database.close()


def test_postgres_jsonb_numeric_normalization_preserves_payload_identity(
    pg_environment: _PgEnvironment,
) -> None:
    seed = _seed_scope(pg_environment)
    with pg_environment.migration_engine.begin() as connection:
        connection.execute(
            insert(release_pointers).values(
                tenant_id=UUID(seed.scope.tenant_id),
                container_id=UUID(seed.scope.container_id),
                authoring_head=None,
                serving_head=None,
                lock_version=0,
            )
        )
    objects = (
        ContentObjectVersion.from_payload(
            logical_id="numeric:signed-zero",
            object_type="numeric",
            payload={"value": -0.0},
        ),
        ContentObjectVersion.from_payload(
            logical_id="numeric:exponent",
            object_type="numeric",
            payload={"value": 1e20},
        ),
        ContentObjectVersion.from_payload(
            logical_id="numeric:large-integer",
            object_type="numeric",
            payload={"value": 1234567890123456789012345678901234567890},
        ),
    )
    members = tuple(
        RevisionMembership.live(
            logical_id=item.logical_id,
            object_version_id=item.object_version_id,
        )
        for item in objects
    )
    revision = ContentRevision.from_memberships(
        parent_revision=None,
        memberships=members,
        created_at=_NOW,
        created_by=seed.actor,
    )
    database = pg_environment.database()
    try:
        state = TeamRepository(database).write_revision(
            seed.scope,
            revision=revision,
            memberships=members,
            objects=objects,
            expected_authoring_head=None,
            expected_generation=0,
            actor_subject=seed.actor,
        )
    finally:
        database.close()
    assert {item.object_version_id: item.payload_json for item in state.objects} == {
        item.object_version_id: item.payload_json for item in objects
    }


def test_repository_read_rechecks_active_membership_in_scoped_transaction(
    pg_environment: _PgEnvironment,
    tmp_path: Path,
) -> None:
    seed = _seed_scope(pg_environment)
    material = _bootstrap_material(tmp_path, seed.actor)
    _bootstrap(pg_environment, seed, material)
    assert _read_canonical(pg_environment, seed).revision == material.plan.revision
    with pg_environment.migration_engine.begin() as connection:
        connection.execute(
            update(memberships)
            .where(
                memberships.c.tenant_id == UUID(seed.scope.tenant_id),
                memberships.c.subject_id == UUID(seed.actor),
            )
            .values(active=False)
        )
    database = pg_environment.database()
    try:
        with pytest.raises(TeamAuthorizationError, match="no active membership"):
            TeamRepository(database).read_canonical(seed.scope, subject_id=seed.actor)
    finally:
        database.close()


def test_team_api_uses_database_membership_not_jwt_role(
    pg_environment: _PgEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    first = _seed_scope(pg_environment)
    second = _seed_scope(pg_environment, actor=first.actor)
    first_material = _bootstrap_material(tmp_path / "tenant-a", first.actor)
    second_material = _bootstrap_material(tmp_path / "tenant-b", second.actor)
    _bootstrap(pg_environment, first, first_material)
    _bootstrap(pg_environment, second, second_material)
    secret = "synthetic-n12-integration-secret"
    monkeypatch.setenv("OWCOPILOT_PROFILE", "team")
    monkeypatch.setenv("OWCOPILOT_JWT_SECRET", secret)
    database = pg_environment.database()
    repository = TeamRepository(database)
    token = mint_token(
        Principal(
            user_id=first.actor,
            tenant_id=first.scope.tenant_id,
            role=Role.VIEWER,
        ),
        secret=secret,
    )
    second_token = mint_token(
        Principal(
            user_id=second.actor,
            tenant_id=second.scope.tenant_id,
            role=Role.VIEWER,
        ),
        secret=secret,
    )
    outsider = _uuid()
    untrusted_owner_token = mint_token(
        Principal(
            user_id=outsider,
            tenant_id=first.scope.tenant_id,
            role=Role.OWNER,
        ),
        secret=secret,
    )
    app = create_app(team_database=database, team_repository=repository)
    with fastapi_testclient.TestClient(app) as client:
        first_accepted = client.get(
            f"/team/containers/{first.scope.container_id}/canonical",
            headers={"Authorization": f"Bearer {token}"},
        )
        second_accepted = client.get(
            f"/team/containers/{second.scope.container_id}/canonical",
            headers={"Authorization": f"Bearer {second_token}"},
        )
        # The token supplies tenant A while the path supplies tenant B's
        # container. Both memberships are active, so this is a genuine mixed
        # tenant/container negative rather than a missing-membership shortcut.
        mixed_scope = client.get(
            f"/team/containers/{second.scope.container_id}/canonical",
            headers={"Authorization": f"Bearer {token}"},
        )
        denied = client.get(
            f"/team/containers/{first.scope.container_id}/canonical",
            headers={"Authorization": f"Bearer {untrusted_owner_token}"},
        )
    assert first_accepted.status_code == 200, first_accepted.text
    assert second_accepted.status_code == 200, second_accepted.text
    assert first_accepted.json()["authorization"] == {
        "subject_id": first.actor,
        "membership_role": "owner",
        "jwt_role_is_authoritative": False,
    }
    assert mixed_scope.status_code == 404
    assert denied.status_code == 403
