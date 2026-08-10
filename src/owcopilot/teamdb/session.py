"""PostgreSQL engine lifecycle and transaction-local RLS scope."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import InterfaceError, OperationalError, SQLAlchemyError

from ..contracts import OwnershipScope
from .config import TeamDatabaseConfig
from .errors import TeamDatabaseUnavailable, TeamSchemaMismatch
from .schema import EXPECTED_ALEMBIC_HEAD, RUNTIME_ROLE

_SAFE_ERROR_TOKEN = re.compile(r"[^A-Za-z0-9_.:-]+")


def _canonical_uuid(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a canonical UUID string")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical UUID string") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError(f"{label} must be a canonical non-nil UUID string")
    return value


def _safe_error_kind(exc: BaseException) -> str:
    """Class-only diagnostics avoid reflecting DSNs, SQL, or scoped identifiers."""

    return _SAFE_ERROR_TOKEN.sub("_", type(exc).__name__)[:80]


@dataclass(frozen=True, slots=True)
class DatabaseHealth:
    ready: bool
    database_available: bool
    schema_head: str | None
    expected_schema_head: str
    detail: str | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class TeamDatabase:
    """One app-role engine; every content operation enters a scoped transaction."""

    def __init__(self, config: TeamDatabaseConfig, *, engine: Engine | None = None) -> None:
        self.config = config
        self._engine = engine or create_engine(
            config.database_url,
            pool_pre_ping=True,
            pool_reset_on_return="rollback",
            pool_size=config.pool_size,
            max_overflow=config.max_overflow,
            pool_timeout=config.pool_timeout_seconds,
            connect_args={"connect_timeout": config.connect_timeout_seconds},
        )

    @classmethod
    def from_env(cls) -> TeamDatabase:
        return cls(TeamDatabaseConfig.from_env())

    @property
    def engine(self) -> Engine:
        return self._engine

    def close(self) -> None:
        self._engine.dispose()

    def health(self) -> DatabaseHealth:
        """Probe PostgreSQL, exact Alembic head, and non-bypass runtime role."""

        try:
            with self._engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                schema_rows = (
                    connection.execute(
                        text("SELECT version_num FROM alembic_version ORDER BY version_num")
                    )
                    .scalars()
                    .all()
                )
                role = (
                    connection.execute(
                        text(
                            "SELECT r.rolname, r.rolsuper, r.rolbypassrls, "
                            "r.rolcreatedb, r.rolcreaterole, r.rolreplication, "
                            "r.rolinherit, r.rolcanlogin, "
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
        except (OperationalError, InterfaceError) as exc:
            return DatabaseHealth(
                ready=False,
                database_available=False,
                schema_head=None,
                expected_schema_head=EXPECTED_ALEMBIC_HEAD,
                detail=f"database_unavailable:{_safe_error_kind(exc)}",
            )
        except SQLAlchemyError as exc:
            return DatabaseHealth(
                ready=False,
                database_available=True,
                schema_head=None,
                expected_schema_head=EXPECTED_ALEMBIC_HEAD,
                detail=f"schema_probe_failed:{_safe_error_kind(exc)}",
            )

        schema_head = str(schema_rows[0]) if len(schema_rows) == 1 else None
        if schema_head != EXPECTED_ALEMBIC_HEAD:
            return DatabaseHealth(
                ready=False,
                database_available=True,
                schema_head=schema_head,
                expected_schema_head=EXPECTED_ALEMBIC_HEAD,
                detail="schema_head_mismatch",
            )
        if role is None:
            return DatabaseHealth(
                ready=False,
                database_available=True,
                schema_head=schema_head,
                expected_schema_head=EXPECTED_ALEMBIC_HEAD,
                detail="runtime_role_missing",
            )
        role_name = str(role["rolname"])
        if (
            role_name != self.config.expected_role
            or role_name != RUNTIME_ROLE
            or bool(role["rolsuper"])
            or bool(role["rolbypassrls"])
            or bool(role["rolcreatedb"])
            or bool(role["rolcreaterole"])
            or bool(role["rolreplication"])
            or bool(role["rolinherit"])
            or not bool(role["rolcanlogin"])
            or bool(role["has_role_membership"])
        ):
            return DatabaseHealth(
                ready=False,
                database_available=True,
                schema_head=schema_head,
                expected_schema_head=EXPECTED_ALEMBIC_HEAD,
                detail="unsafe_runtime_role",
            )
        return DatabaseHealth(
            ready=True,
            database_available=True,
            schema_head=schema_head,
            expected_schema_head=EXPECTED_ALEMBIC_HEAD,
            detail=None,
        )

    def require_ready(self) -> None:
        health = self.health()
        if health.ready:
            return
        if not health.database_available:
            raise TeamDatabaseUnavailable("team PostgreSQL is unavailable")
        raise TeamSchemaMismatch(f"team PostgreSQL failed readiness: {health.detail or 'unknown'}")

    @contextmanager
    def transaction(self, scope: OwnershipScope) -> Iterator[Connection]:
        """Open one transaction and set both ownership GUCs with local=true."""

        validated = OwnershipScope.model_validate(scope)
        # Startup readiness is not a lifetime guarantee: a migration or role
        # change can occur while the process is alive.  Recheck immediately
        # before every business transaction so an old-but-query-compatible
        # schema can never be used as the team truth source.
        self.require_ready()
        try:
            with self._engine.begin() as connection:
                self._install_scope(
                    connection,
                    tenant_id=validated.tenant_id,
                    container_id=validated.container_id,
                )
                yield connection
        except (OperationalError, InterfaceError) as exc:
            raise TeamDatabaseUnavailable("team PostgreSQL transaction failed") from exc

    @contextmanager
    def tenant_transaction(self, tenant_id: str) -> Iterator[Connection]:
        """Open a tenant control-plane transaction with no container scope."""

        validated_tenant = _canonical_uuid(tenant_id, label="tenant_id")
        self.require_ready()
        try:
            with self._engine.begin() as connection:
                self._install_scope(
                    connection,
                    tenant_id=validated_tenant,
                    container_id=None,
                )
                yield connection
        except (OperationalError, InterfaceError) as exc:
            raise TeamDatabaseUnavailable("team PostgreSQL transaction failed") from exc

    @staticmethod
    def _install_scope(
        connection: Connection,
        *,
        tenant_id: str,
        container_id: str | None,
    ) -> None:
        expected_container = container_id or ""
        # N1.1 timestamps are canonical UTC values. PostgreSQL renders timestamptz in the
        # session zone, so a pooled connection inherited from a non-UTC deployment must be
        # normalized inside the same transaction as the RLS context.
        connection.execute(text("SET LOCAL TIME ZONE 'UTC'"), {})
        observed = (
            connection.execute(
                text(
                    "SELECT "
                    "set_config('owcopilot.tenant_id', :tenant_id, true) AS tenant_id, "
                    "set_config('owcopilot.container_id', :container_id, true) AS container_id"
                ),
                {"tenant_id": tenant_id, "container_id": expected_container},
            )
            .mappings()
            .one()
        )
        if (
            cast(str, observed["tenant_id"]) != tenant_id
            or cast(str, observed["container_id"]) != expected_container
        ):
            raise TeamSchemaMismatch("PostgreSQL rejected the transaction-local ownership scope")


__all__ = ["DatabaseHealth", "TeamDatabase"]
