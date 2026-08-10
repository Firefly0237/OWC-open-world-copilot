"""Idempotently seed one PostgreSQL team tenant, owner, and container."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from owcopilot.teamdb.schema import (
    EXPECTED_ALEMBIC_HEAD,
    MIGRATION_ROLE,
    containers,
    memberships,
    tenants,
)

MIGRATION_DATABASE_URL_ENV = "OWCOPILOT_MIGRATION_DATABASE_URL"


class SeedConfigurationError(ValueError):
    """The requested seed identity or migration connection is unsafe."""


class SeedConflictError(RuntimeError):
    """Existing control-plane state does not exactly match the seed request."""


class SeedSchemaError(RuntimeError):
    """The database role or schema is not the frozen N1.2 seed target."""


@dataclass(frozen=True, slots=True)
class TeamScopeSeed:
    tenant_id: str
    tenant_slug: str
    tenant_display_name: str
    container_id: str
    container_slug: str
    container_display_name: str
    owner_subject: str

    def __post_init__(self) -> None:
        _canonical_uuid(self.tenant_id, label="tenant_id")
        _canonical_uuid(self.container_id, label="container_id")
        _canonical_uuid(self.owner_subject, label="owner_subject")
        _nonempty_text(self.tenant_slug, label="tenant_slug")
        _nonempty_text(self.tenant_display_name, label="tenant_display_name")
        _nonempty_text(self.container_slug, label="container_slug")
        _nonempty_text(self.container_display_name, label="container_display_name")


@dataclass(frozen=True, slots=True)
class TeamScopeSeedResult:
    tenant_id: str
    container_id: str
    owner_subject: str
    tenant: Literal["created", "replayed"]
    membership: Literal["created", "replayed"]
    container: Literal["created", "replayed"]


def _canonical_uuid(value: object, *, label: str) -> UUID:
    if type(value) is not str:
        raise SeedConfigurationError(f"{label} must be a canonical UUID string")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise SeedConfigurationError(f"{label} must be a canonical UUID string") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise SeedConfigurationError(f"{label} must be a canonical non-nil UUID string")
    return parsed


def _nonempty_text(value: object, *, label: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise SeedConfigurationError(
            f"{label} must be non-empty text without surrounding whitespace"
        )
    return value


def migration_database_url(environ: Mapping[str, str] | None = None) -> URL:
    """Load the migration URL without ever reflecting its credential-bearing text."""

    values = os.environ if environ is None else environ
    raw_url = values.get(MIGRATION_DATABASE_URL_ENV)
    if not raw_url:
        raise SeedConfigurationError(f"{MIGRATION_DATABASE_URL_ENV} is required")
    try:
        url = make_url(raw_url)
    except Exception as exc:
        raise SeedConfigurationError("migration database URL is malformed") from exc
    if url.drivername != "postgresql+psycopg":
        raise SeedConfigurationError(
            "migration database URL must use the postgresql+psycopg dialect"
        )
    if url.username != MIGRATION_ROLE:
        raise SeedConfigurationError(
            f"migration database URL must authenticate as {MIGRATION_ROLE}"
        )
    if not url.database:
        raise SeedConfigurationError("migration database URL must name an explicit database")
    return url


def _assert_seed_target(connection: Connection) -> None:
    role = (
        connection.execute(
            text(
                "SELECT r.rolname, r.rolsuper, r.rolbypassrls, r.rolcreatedb, "
                "r.rolcreaterole, r.rolreplication, r.rolinherit, r.rolcanlogin, "
                "EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_auth_members m "
                "WHERE m.member = r.oid OR m.roleid = r.oid"
                ") AS has_role_membership "
                "FROM pg_catalog.pg_roles r WHERE r.rolname = current_user"
            )
        )
        .mappings()
        .one_or_none()
    )
    if role is None or role["rolname"] != MIGRATION_ROLE:
        raise SeedSchemaError("database connection is not using the migration role")
    if (
        bool(role["rolsuper"])
        or bool(role["rolbypassrls"])
        or bool(role["rolcreatedb"])
        or bool(role["rolcreaterole"])
        or bool(role["rolreplication"])
        or bool(role["rolinherit"])
        or not bool(role["rolcanlogin"])
        or bool(role["has_role_membership"])
    ):
        raise SeedSchemaError("migration role has unsafe attributes or membership edges")
    heads = (
        connection.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num"))
        .scalars()
        .all()
    )
    if heads != [EXPECTED_ALEMBIC_HEAD]:
        raise SeedSchemaError("database is not at the exact expected Alembic head")


def _insert_tenant(connection: Connection, seed: TeamScopeSeed) -> Literal["created", "replayed"]:
    tenant_id = _canonical_uuid(seed.tenant_id, label="tenant_id")
    inserted_id = connection.execute(
        pg_insert(tenants)
        .values(
            tenant_id=tenant_id,
            slug=seed.tenant_slug,
            display_name=seed.tenant_display_name,
        )
        .on_conflict_do_nothing()
        .returning(tenants.c.tenant_id)
    ).scalar_one_or_none()
    row = (
        connection.execute(
            select(tenants.c.tenant_id, tenants.c.slug, tenants.c.display_name).where(
                tenants.c.tenant_id == tenant_id
            )
        )
        .mappings()
        .one_or_none()
    )
    expected = {
        "tenant_id": tenant_id,
        "slug": seed.tenant_slug,
        "display_name": seed.tenant_display_name,
    }
    if row is None or any(row[field] != value for field, value in expected.items()):
        raise SeedConflictError("tenant identity or fields conflict with existing state")
    return "created" if inserted_id is not None else "replayed"


def _insert_owner_membership(
    connection: Connection, seed: TeamScopeSeed
) -> Literal["created", "replayed"]:
    tenant_id = _canonical_uuid(seed.tenant_id, label="tenant_id")
    owner_subject = _canonical_uuid(seed.owner_subject, label="owner_subject")
    inserted_subject = connection.execute(
        pg_insert(memberships)
        .values(
            tenant_id=tenant_id,
            subject_id=owner_subject,
            role="owner",
            active=True,
        )
        .on_conflict_do_nothing()
        .returning(memberships.c.subject_id)
    ).scalar_one_or_none()
    row = (
        connection.execute(
            select(
                memberships.c.tenant_id,
                memberships.c.subject_id,
                memberships.c.role,
                memberships.c.active,
            ).where(
                memberships.c.tenant_id == tenant_id,
                memberships.c.subject_id == owner_subject,
            )
        )
        .mappings()
        .one_or_none()
    )
    expected: dict[str, Any] = {
        "tenant_id": tenant_id,
        "subject_id": owner_subject,
        "role": "owner",
        "active": True,
    }
    if row is None or any(row[field] != value for field, value in expected.items()):
        raise SeedConflictError("owner membership role or active state conflicts")
    return "created" if inserted_subject is not None else "replayed"


def _insert_container(
    connection: Connection, seed: TeamScopeSeed
) -> Literal["created", "replayed"]:
    tenant_id = _canonical_uuid(seed.tenant_id, label="tenant_id")
    container_id = _canonical_uuid(seed.container_id, label="container_id")
    inserted_id = connection.execute(
        pg_insert(containers)
        .values(
            tenant_id=tenant_id,
            container_id=container_id,
            slug=seed.container_slug,
            display_name=seed.container_display_name,
        )
        .on_conflict_do_nothing()
        .returning(containers.c.container_id)
    ).scalar_one_or_none()
    row = (
        connection.execute(
            select(
                containers.c.tenant_id,
                containers.c.container_id,
                containers.c.slug,
                containers.c.display_name,
            ).where(
                containers.c.tenant_id == tenant_id,
                containers.c.container_id == container_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    expected = {
        "tenant_id": tenant_id,
        "container_id": container_id,
        "slug": seed.container_slug,
        "display_name": seed.container_display_name,
    }
    if row is None or any(row[field] != value for field, value in expected.items()):
        raise SeedConflictError("container identity or fields conflict with existing state")
    return "created" if inserted_id is not None else "replayed"


def seed_team_scope(connection: Connection, seed: TeamScopeSeed) -> TeamScopeSeedResult:
    """Seed one scope on the caller's transaction-bound connection."""

    _assert_seed_target(connection)
    tenant_status = _insert_tenant(connection, seed)
    membership_status = _insert_owner_membership(connection, seed)
    container_status = _insert_container(connection, seed)
    return TeamScopeSeedResult(
        tenant_id=seed.tenant_id,
        container_id=seed.container_id,
        owner_subject=seed.owner_subject,
        tenant=tenant_status,
        membership=membership_status,
        container=container_status,
    )


def run_seed(engine: Engine, seed: TeamScopeSeed) -> TeamScopeSeedResult:
    """Run all seed checks and writes in exactly one database transaction."""

    with engine.begin() as connection:
        return seed_team_scope(connection, seed)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Idempotently create one team tenant, active owner membership, and container. "
            "Existing rows must match exactly."
        )
    )
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--tenant-slug", required=True)
    parser.add_argument("--tenant-display-name", required=True)
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--container-slug", required=True)
    parser.add_argument("--container-display-name", required=True)
    parser.add_argument("--owner-subject", required=True)
    return parser


def _seed_from_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> TeamScopeSeed:
    try:
        return TeamScopeSeed(
            tenant_id=args.tenant_id,
            tenant_slug=args.tenant_slug,
            tenant_display_name=args.tenant_display_name,
            container_id=args.container_id,
            container_slug=args.container_slug,
            container_display_name=args.container_display_name,
            owner_subject=args.owner_subject,
        )
    except SeedConfigurationError as exc:
        parser.error(str(exc))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    seed = _seed_from_args(parser, args)
    engine: Engine | None = None
    try:
        url = migration_database_url()
        engine = create_engine(
            url,
            poolclass=NullPool,
            connect_args={"connect_timeout": 5},
        )
        result = run_seed(engine, seed)
    except SeedConfigurationError as exc:
        print(json.dumps({"error": "CONFIGURATION_ERROR", "detail": str(exc)}), file=sys.stderr)
        return 2
    except SeedConflictError as exc:
        print(json.dumps({"error": "SEED_CONFLICT", "detail": str(exc)}), file=sys.stderr)
        return 3
    except SeedSchemaError as exc:
        print(json.dumps({"error": "SCHEMA_ERROR", "detail": str(exc)}), file=sys.stderr)
        return 4
    except SQLAlchemyError as exc:
        print(
            json.dumps(
                {
                    "error": "DATABASE_ERROR",
                    "detail": f"database operation failed:{type(exc).__name__}",
                }
            ),
            file=sys.stderr,
        )
        return 5
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps({"status": "READY", **asdict(result)}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
