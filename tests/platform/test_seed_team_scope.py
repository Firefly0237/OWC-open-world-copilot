from __future__ import annotations

import os
from contextlib import nullcontext
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from scripts import seed_team_scope as subject
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

TENANT_ID = "11111111-1111-4111-8111-111111111111"
CONTAINER_ID = "22222222-2222-4222-8222-222222222222"
OWNER_SUBJECT = "33333333-3333-4333-8333-333333333333"


class _Result:
    def __init__(self, *, value: object = None, rowcount: int = -1) -> None:
        self.value = value
        self.rowcount = rowcount

    def scalar_one(self) -> object:
        return self.value

    def scalar_one_or_none(self) -> object:
        return self.value

    def scalars(self) -> _Result:
        return self

    def all(self) -> object:
        return self.value

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> object:
        return self.value


class _Connection:
    def __init__(self, results: list[_Result]) -> None:
        self.results = iter(results)
        self.statements: list[object] = []

    def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        return next(self.results)


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.begin_calls = 0

    def begin(self) -> nullcontext[_Connection]:
        self.begin_calls += 1
        return nullcontext(self.connection)


def _seed() -> subject.TeamScopeSeed:
    return subject.TeamScopeSeed(
        tenant_id=TENANT_ID,
        tenant_slug="acme",
        tenant_display_name="Acme Studio",
        container_id=CONTAINER_ID,
        container_slug="world-one",
        container_display_name="World One",
        owner_subject=OWNER_SUBJECT,
    )


def _safe_migration_role() -> dict[str, object]:
    return {
        "rolname": "owcopilot_migration",
        "rolsuper": False,
        "rolbypassrls": False,
        "rolcreatedb": False,
        "rolcreaterole": False,
        "rolreplication": False,
        "rolinherit": False,
        "rolcanlogin": True,
        "has_role_membership": False,
    }


def _happy_results(*, rowcount: int) -> list[_Result]:
    tenant = {
        "tenant_id": UUID(TENANT_ID),
        "slug": "acme",
        "display_name": "Acme Studio",
    }
    membership = {
        "tenant_id": UUID(TENANT_ID),
        "subject_id": UUID(OWNER_SUBJECT),
        "role": "owner",
        "active": True,
    }
    container = {
        "tenant_id": UUID(TENANT_ID),
        "container_id": UUID(CONTAINER_ID),
        "slug": "world-one",
        "display_name": "World One",
    }
    return [
        _Result(value=_safe_migration_role()),
        _Result(value=[subject.EXPECTED_ALEMBIC_HEAD]),
        _Result(value=UUID(TENANT_ID) if rowcount == 1 else None),
        _Result(value=tenant),
        _Result(value=UUID(OWNER_SUBJECT) if rowcount == 1 else None),
        _Result(value=membership),
        _Result(value=UUID(CONTAINER_ID) if rowcount == 1 else None),
        _Result(value=container),
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", "00000000-0000-0000-0000-000000000000"),
        ("container_id", "22222222222242228222222222222222"),
        ("owner_subject", "NOT-A-UUID"),
        ("tenant_slug", ""),
        ("tenant_display_name", "   "),
        ("container_slug", " world-one"),
        ("container_display_name", "World One "),
    ],
)
def test_seed_requires_explicit_canonical_identity_and_nonempty_fields(
    field: str, value: str
) -> None:
    values = {
        "tenant_id": TENANT_ID,
        "tenant_slug": "acme",
        "tenant_display_name": "Acme Studio",
        "container_id": CONTAINER_ID,
        "container_slug": "world-one",
        "container_display_name": "World One",
        "owner_subject": OWNER_SUBJECT,
    }
    values[field] = value
    with pytest.raises(subject.SeedConfigurationError):
        subject.TeamScopeSeed(**values)


def test_migration_url_is_required_and_bound_to_psycopg_migration_role() -> None:
    with pytest.raises(subject.SeedConfigurationError, match="is required"):
        subject.migration_database_url({})
    with pytest.raises(subject.SeedConfigurationError, match=r"postgresql\+psycopg"):
        subject.migration_database_url(
            {subject.MIGRATION_DATABASE_URL_ENV: "postgresql://owcopilot_migration:x@db/team"}
        )
    secret_url = "postgresql+psycopg://owcopilot_app:do-not-reflect@db/team"
    with pytest.raises(subject.SeedConfigurationError) as caught:
        subject.migration_database_url({subject.MIGRATION_DATABASE_URL_ENV: secret_url})
    assert "do-not-reflect" not in str(caught.value)
    assert "owcopilot_migration" in str(caught.value)


@pytest.mark.parametrize(("rowcount", "expected"), [(1, "created"), (0, "replayed")])
def test_seed_uses_one_transaction_and_is_exactly_replayable(rowcount: int, expected: str) -> None:
    connection = _Connection(_happy_results(rowcount=rowcount))
    engine = _Engine(connection)

    result = subject.run_seed(engine, _seed())  # type: ignore[arg-type]

    assert engine.begin_calls == 1
    assert len(connection.statements) == 8
    assert (result.tenant, result.membership, result.container) == (
        expected,
        expected,
        expected,
    )


def test_existing_tenant_field_conflict_fails_closed_before_later_writes() -> None:
    conflicting_tenant = {
        "tenant_id": UUID(TENANT_ID),
        "slug": "acme",
        "display_name": "Different Studio",
    }
    connection = _Connection(
        [
            _Result(value=_safe_migration_role()),
            _Result(value=[subject.EXPECTED_ALEMBIC_HEAD]),
            _Result(value=None),
            _Result(value=conflicting_tenant),
        ]
    )

    with pytest.raises(subject.SeedConflictError, match="tenant identity or fields"):
        subject.seed_team_scope(connection, _seed())  # type: ignore[arg-type]

    assert len(connection.statements) == 4


@pytest.mark.parametrize(("role", "active"), [("viewer", True), ("owner", False)])
def test_existing_membership_role_or_active_conflict_fails_closed(role: str, active: bool) -> None:
    results = _happy_results(rowcount=0)
    results[5] = _Result(
        value={
            "tenant_id": UUID(TENANT_ID),
            "subject_id": UUID(OWNER_SUBJECT),
            "role": role,
            "active": active,
        }
    )
    connection = _Connection(results)

    with pytest.raises(subject.SeedConflictError, match="role or active"):
        subject.seed_team_scope(connection, _seed())  # type: ignore[arg-type]

    assert len(connection.statements) == 6


def test_existing_container_field_conflict_fails_closed() -> None:
    results = _happy_results(rowcount=0)
    results[7] = _Result(
        value={
            "tenant_id": UUID(TENANT_ID),
            "container_id": UUID(CONTAINER_ID),
            "slug": "world-one",
            "display_name": "Different World",
        }
    )
    connection = _Connection(results)

    with pytest.raises(subject.SeedConflictError, match="container identity or fields"):
        subject.seed_team_scope(connection, _seed())  # type: ignore[arg-type]


def test_schema_and_role_must_match_exactly() -> None:
    wrong_role_row = _safe_migration_role()
    wrong_role_row["rolname"] = "owcopilot_app"
    wrong_role = _Connection([_Result(value=wrong_role_row)])
    with pytest.raises(subject.SeedSchemaError, match="migration role"):
        subject.seed_team_scope(wrong_role, _seed())  # type: ignore[arg-type]

    wrong_head = _Connection([_Result(value=_safe_migration_role()), _Result(value=["n12_0001"])])
    with pytest.raises(subject.SeedSchemaError, match="Alembic head"):
        subject.seed_team_scope(wrong_head, _seed())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rolsuper", True),
        ("rolbypassrls", True),
        ("rolcreatedb", True),
        ("rolcreaterole", True),
        ("rolreplication", True),
        ("rolinherit", True),
        ("rolcanlogin", False),
        ("has_role_membership", True),
    ],
)
def test_unsafe_migration_role_attributes_and_membership_fail_closed(
    field: str, value: bool
) -> None:
    role = _safe_migration_role()
    role[field] = value
    connection = _Connection([_Result(value=role)])

    with pytest.raises(subject.SeedSchemaError, match="unsafe attributes"):
        subject.seed_team_scope(connection, _seed())  # type: ignore[arg-type]

    rendered_query = str(connection.statements[0])
    assert "m.member = r.oid OR m.roleid = r.oid" in rendered_query


def test_database_error_output_never_reflects_url_or_password(monkeypatch, capsys) -> None:
    secret = "this-password-must-not-appear"
    monkeypatch.setenv(
        subject.MIGRATION_DATABASE_URL_ENV,
        f"postgresql+psycopg://owcopilot_migration:{secret}@db/team",
    )

    def fail_create_engine(*args: object, **kwargs: object) -> None:
        raise OperationalError("connect", {}, RuntimeError(secret))

    monkeypatch.setattr(subject, "create_engine", fail_create_engine)
    status = subject.main(
        [
            "--tenant-id",
            TENANT_ID,
            "--tenant-slug",
            "acme",
            "--tenant-display-name",
            "Acme Studio",
            "--container-id",
            CONTAINER_ID,
            "--container-slug",
            "world-one",
            "--container-display-name",
            "World One",
            "--owner-subject",
            OWNER_SUBJECT,
        ]
    )

    captured = capsys.readouterr()
    assert status == 5
    assert secret not in captured.err
    assert "db/team" not in captured.err
    assert "DATABASE_ERROR" in captured.err


def test_real_postgres_seed_and_replay_when_migration_url_is_available() -> None:
    if not os.getenv(subject.MIGRATION_DATABASE_URL_ENV):
        pytest.skip("real PostgreSQL migration URL is not configured")
    url = subject.migration_database_url()
    engine = create_engine(url, poolclass=NullPool)
    tenant_id = str(uuid4())
    real_seed = subject.TeamScopeSeed(
        tenant_id=tenant_id,
        tenant_slug=f"seed-test-{tenant_id}",
        tenant_display_name="Seed Integration Tenant",
        container_id=str(uuid4()),
        container_slug="seed-integration-container",
        container_display_name="Seed Integration Container",
        owner_subject=str(uuid4()),
    )
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                created = subject.seed_team_scope(connection, real_seed)
                replayed = subject.seed_team_scope(connection, real_seed)
                assert (created.tenant, created.membership, created.container) == (
                    "created",
                    "created",
                    "created",
                )
                assert (replayed.tenant, replayed.membership, replayed.container) == (
                    "replayed",
                    "replayed",
                    "replayed",
                )
                with pytest.raises(subject.SeedConflictError):
                    subject.seed_team_scope(
                        connection,
                        replace(real_seed, container_display_name="Conflicting Container"),
                    )
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
