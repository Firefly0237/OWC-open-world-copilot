from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import RowMapping

from owcopilot.contracts import (
    ContentObjectVersion,
    ContentRevision,
    OwnershipScope,
    RevisionMembership,
)
from owcopilot.teamdb import (
    RevisionConflict,
    TeamAuthorizationError,
    TeamDatabaseConfig,
    TeamDatabaseConfigurationError,
    TeamIntegrityError,
    TeamRepository,
    TeamSchemaMismatch,
)
from owcopilot.teamdb.repository import _validate_revision_bundle
from owcopilot.teamdb.session import TeamDatabase

TENANT_ID = "11111111-1111-4111-8111-111111111111"
CONTAINER_ID = "22222222-2222-4222-8222-222222222222"
SUBJECT_ID = "33333333-3333-4333-8333-333333333333"


def _revision_bundle() -> tuple[
    ContentObjectVersion,
    RevisionMembership,
    ContentRevision,
]:
    object_version = ContentObjectVersion.from_payload(
        logical_id="character:alan",
        object_type="character",
        payload={"name": "阿岚", "rank": 2},
    )
    membership = RevisionMembership.live(
        logical_id=object_version.logical_id,
        object_version_id=object_version.object_version_id,
    )
    revision = ContentRevision.from_memberships(
        parent_revision=None,
        memberships=[membership],
        created_at=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        created_by=SUBJECT_ID,
    )
    return object_version, membership, revision


def test_team_config_requires_real_psycopg_postgresql() -> None:
    with pytest.raises(TeamDatabaseConfigurationError, match="required"):
        TeamDatabaseConfig.from_env({})
    with pytest.raises(TeamDatabaseConfigurationError, match=r"postgresql\+psycopg"):
        TeamDatabaseConfig.from_env({"OWCOPILOT_TEAM_DATABASE_URL": "sqlite+pysqlite:///:memory:"})


def test_team_config_requires_app_role_username() -> None:
    with pytest.raises(TeamDatabaseConfigurationError, match="username must match"):
        TeamDatabaseConfig.from_env(
            {
                "OWCOPILOT_TEAM_DATABASE_URL": (
                    "postgresql+psycopg://owcopilot_migration:secret@localhost/team"
                )
            }
        )


def test_team_config_diagnostic_label_omits_credentials() -> None:
    config = TeamDatabaseConfig.from_env(
        {
            "OWCOPILOT_TEAM_DATABASE_URL": (
                "postgresql+psycopg://owcopilot_app:very-secret@db.internal:5433/team"
            )
        }
    )
    assert config.safe_database_label() == "db.internal:5433/team"
    assert "secret" not in config.safe_database_label()


class _ScopeResult:
    def __init__(self, tenant_id: str, container_id: str) -> None:
        self._row = {"tenant_id": tenant_id, "container_id": container_id}

    def mappings(self) -> _ScopeResult:
        return self

    def one(self) -> dict[str, str]:
        return self._row


class _ScopeConnection:
    def __init__(self) -> None:
        self.statement = ""
        self.statements: list[str] = []
        self.parameters: dict[str, str] = {}

    def execute(self, statement: object, parameters: dict[str, str]) -> _ScopeResult:
        self.statement = str(statement)
        self.statements.append(self.statement)
        if "SET LOCAL TIME ZONE" in self.statement:
            assert parameters == {}
            return _ScopeResult("", "")
        self.parameters = parameters
        return _ScopeResult(parameters["tenant_id"], parameters["container_id"])


def test_scope_install_is_transaction_local_for_both_gucs() -> None:
    connection = _ScopeConnection()
    TeamDatabase._install_scope(  # noqa: SLF001 - direct boundary unit test
        cast(Any, connection),
        tenant_id=TENANT_ID,
        container_id=CONTAINER_ID,
    )
    assert connection.statements[0] == "SET LOCAL TIME ZONE 'UTC'"
    assert "set_config('owcopilot.tenant_id', :tenant_id, true)" in connection.statement
    assert "set_config('owcopilot.container_id', :container_id, true)" in connection.statement
    assert connection.parameters == {
        "tenant_id": TENANT_ID,
        "container_id": CONTAINER_ID,
    }


def test_tenant_scope_explicitly_clears_container_guc() -> None:
    connection = _ScopeConnection()
    TeamDatabase._install_scope(  # noqa: SLF001 - direct boundary unit test
        cast(Any, connection),
        tenant_id=TENANT_ID,
        container_id=None,
    )
    assert connection.parameters["container_id"] == ""


class _NeverBeginEngine:
    def __init__(self) -> None:
        self.begin_called = False

    def begin(self):  # type: ignore[no-untyped-def]
        self.begin_called = True
        raise AssertionError("business transaction opened before readiness")


def test_business_transaction_checks_readiness_before_opening_sql() -> None:
    config = TeamDatabaseConfig.from_env(
        {
            "OWCOPILOT_TEAM_DATABASE_URL": (
                "postgresql+psycopg://owcopilot_app:secret@localhost/team"
            )
        }
    )
    engine = _NeverBeginEngine()
    database = TeamDatabase(config, engine=cast(Any, engine))

    def reject_old_schema() -> None:
        raise TeamSchemaMismatch("old schema")

    database.require_ready = reject_old_schema  # type: ignore[method-assign]
    scope = OwnershipScope(tenant_id=TENANT_ID, container_id=CONTAINER_ID)
    with pytest.raises(TeamSchemaMismatch, match="old schema"):
        with database.transaction(scope):
            raise AssertionError("transaction body must not run")
    assert engine.begin_called is False


def test_revision_bundle_revalidates_all_five_persistence_contracts() -> None:
    object_version, membership, revision = _revision_bundle()
    bundle = _validate_revision_bundle(
        revision=revision,
        memberships=[membership],
        objects=[object_version],
    )
    assert bundle.revision == revision
    assert bundle.memberships == (membership,)
    assert bundle.objects == (object_version,)


def test_revision_bundle_rejects_missing_live_object() -> None:
    _object_version, membership, revision = _revision_bundle()
    with pytest.raises(TeamIntegrityError, match="exactly match"):
        _validate_revision_bundle(
            revision=revision,
            memberships=[membership],
            objects=[],
        )


def test_revision_bundle_rejects_manifest_hash_mismatch() -> None:
    object_version, _membership, revision = _revision_bundle()
    tombstone = RevisionMembership.deleted(logical_id=object_version.logical_id)
    with pytest.raises(TeamIntegrityError, match="manifest_hash"):
        _validate_revision_bundle(
            revision=revision,
            memberships=[tombstone],
            objects=[],
        )


def test_head_selector_rejects_string_like_objects() -> None:
    class _StringLike:
        def __eq__(self, other: object) -> bool:
            return other == "authoring"

    with pytest.raises(ValueError, match="exactly"):
        TeamRepository._validate_head(_StringLike())  # noqa: SLF001


def test_reconstruction_rejects_text_jsonb_disagreement() -> None:
    object_version, _membership, _revision = _revision_bundle()
    row = cast(
        RowMapping,
        {
            **object_version.model_dump(mode="python"),
            "payload_jsonb": {"name": "篡改", "rank": 2},
        },
    )
    with pytest.raises(TeamIntegrityError, match="disagree"):
        TeamRepository._object_from_row(row)  # noqa: SLF001 - corruption boundary unit test


@pytest.mark.parametrize(
    ("payload", "jsonb_projection"),
    [
        ({"number": 1.0}, {"number": 1}),
        ({"number": -0.0}, {"number": 0}),
        ({"number": 1e20}, {"number": 100000000000000000000}),
        ({"number": 1e-7}, {"number": 0.0000001}),
    ],
)
def test_reconstruction_keeps_payload_identity_when_jsonb_normalizes_numbers(
    payload: dict[str, float],
    jsonb_projection: dict[str, int | float],
) -> None:
    object_version = ContentObjectVersion.from_payload(
        logical_id="numeric:identity",
        object_type="numeric",
        payload=payload,
    )
    row = cast(
        RowMapping,
        {
            **object_version.model_dump(mode="python"),
            "payload_jsonb": jsonb_projection,
        },
    )

    reconstructed = TeamRepository._object_from_row(row)  # noqa: SLF001

    assert reconstructed.payload_json == object_version.payload_json
    assert reconstructed.object_version_id == object_version.object_version_id


def test_reconstruction_does_not_confuse_json_boolean_with_number() -> None:
    object_version = ContentObjectVersion.from_payload(
        logical_id="numeric:boolean",
        object_type="numeric",
        payload={"value": True},
    )
    row = cast(
        RowMapping,
        {
            **object_version.model_dump(mode="python"),
            "payload_jsonb": {"value": 1},
        },
    )

    with pytest.raises(TeamIntegrityError, match="disagree"):
        TeamRepository._object_from_row(row)  # noqa: SLF001


class _UpdateResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _UpdateConnection:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount
        self.statement: object | None = None

    def execute(self, statement: object) -> _UpdateResult:
        self.statement = statement
        return _UpdateResult(self.rowcount)


def test_pointer_cas_sql_binds_scope_head_and_generation() -> None:
    _object_version, _membership, revision = _revision_bundle()
    scope = OwnershipScope(tenant_id=TENANT_ID, container_id=CONTAINER_ID)
    connection = _UpdateConnection(rowcount=1)
    TeamRepository._advance_authoring_pointer(  # noqa: SLF001 - SQL contract unit test
        cast(Any, connection),
        scope=scope,
        revision_id=revision.revision_id,
        expected_head=None,
        expected_generation=0,
    )
    assert connection.statement is not None
    compiled = connection.statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    sql = str(compiled)
    assert "release_pointers.tenant_id" in sql
    assert "release_pointers.container_id" in sql
    assert "release_pointers.authoring_head IS NULL" in sql
    assert "release_pointers.lock_version" in sql
    assert UUID(TENANT_ID) in compiled.params.values()
    assert UUID(CONTAINER_ID) in compiled.params.values()
    assert 0 in compiled.params.values()


def test_pointer_cas_zero_rows_is_conflict() -> None:
    _object_version, _membership, revision = _revision_bundle()
    scope = OwnershipScope(tenant_id=TENANT_ID, container_id=CONTAINER_ID)
    connection = _UpdateConnection(rowcount=0)
    with pytest.raises(RevisionConflict):
        TeamRepository._advance_authoring_pointer(  # noqa: SLF001
            cast(Any, connection),
            scope=scope,
            revision_id=revision.revision_id,
            expected_head=None,
            expected_generation=0,
        )


class _MembershipResult:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    def mappings(self) -> _MembershipResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self.row


class _MembershipConnection:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    def execute(self, _statement: object) -> _MembershipResult:
        return _MembershipResult(self.row)


class _MembershipDatabase:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.observed_tenant: str | None = None

    @contextmanager
    def tenant_transaction(self, tenant_id: str):  # type: ignore[no-untyped-def]
        self.observed_tenant = tenant_id
        yield _MembershipConnection(self.row)


def test_inactive_membership_fails_closed() -> None:
    database = _MembershipDatabase(
        {
            "tenant_id": TENANT_ID,
            "subject_id": SUBJECT_ID,
            "role": "viewer",
            "active": False,
        }
    )
    repository = TeamRepository(cast(Any, database))
    with pytest.raises(TeamAuthorizationError, match="no active"):
        repository.authenticate_membership(TENANT_ID, SUBJECT_ID)
    assert database.observed_tenant == TENANT_ID


def test_active_membership_returns_narrow_grant() -> None:
    database = _MembershipDatabase(
        {
            "tenant_id": TENANT_ID,
            "subject_id": SUBJECT_ID,
            "role": "editor",
            "active": True,
        }
    )
    repository = TeamRepository(cast(Any, database))
    grant = repository.authenticate_membership(TENANT_ID, SUBJECT_ID)
    assert grant.as_dict() == {
        "tenant_id": TENANT_ID,
        "subject_id": SUBJECT_ID,
        "role": "editor",
    }
