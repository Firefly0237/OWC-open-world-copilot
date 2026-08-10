"""Strict environment configuration for the PostgreSQL team data plane."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.engine import URL, make_url

from .errors import TeamDatabaseConfigurationError

TEAM_DATABASE_URL_ENV = "OWCOPILOT_TEAM_DATABASE_URL"
TEAM_DATABASE_ROLE_ENV = "OWCOPILOT_TEAM_DATABASE_ROLE"


def _positive_int(source: str | None, *, default: int, label: str) -> int:
    if source is None:
        return default
    try:
        value = int(source)
    except ValueError as exc:
        raise TeamDatabaseConfigurationError(f"{label} must be an integer") from exc
    if value <= 0:
        raise TeamDatabaseConfigurationError(f"{label} must be positive")
    return value


def _nonnegative_int(source: str | None, *, default: int, label: str) -> int:
    if source is None:
        return default
    try:
        value = int(source)
    except ValueError as exc:
        raise TeamDatabaseConfigurationError(f"{label} must be an integer") from exc
    if value < 0:
        raise TeamDatabaseConfigurationError(f"{label} must not be negative")
    return value


@dataclass(frozen=True, slots=True)
class TeamDatabaseConfig:
    """Validated app-role connection and bounded pool settings.

    Only the psycopg 3 SQLAlchemy dialect is accepted.  In particular, accepting
    SQLite here would make it too easy for a test double to masquerade as the N1.2
    team truth source.
    """

    database_url: URL
    expected_role: str = "owcopilot_app"
    connect_timeout_seconds: int = 5
    pool_size: int = 5
    max_overflow: int = 5
    pool_timeout_seconds: int = 5

    def __post_init__(self) -> None:
        if self.database_url.drivername != "postgresql+psycopg":
            raise TeamDatabaseConfigurationError(
                "team database URL must use the postgresql+psycopg dialect"
            )
        if not self.database_url.database:
            raise TeamDatabaseConfigurationError("team database URL must name a database")
        if not self.database_url.username:
            raise TeamDatabaseConfigurationError("team database URL must name the app role")
        if not self.expected_role or self.expected_role.strip() != self.expected_role:
            raise TeamDatabaseConfigurationError("expected app role must be a non-empty token")
        if self.database_url.username != self.expected_role:
            raise TeamDatabaseConfigurationError(
                "team database URL username must match OWCOPILOT_TEAM_DATABASE_ROLE"
            )
        if self.connect_timeout_seconds <= 0:
            raise TeamDatabaseConfigurationError("connect timeout must be positive")
        if self.pool_size <= 0:
            raise TeamDatabaseConfigurationError("pool size must be positive")
        if self.max_overflow < 0:
            raise TeamDatabaseConfigurationError("max overflow must not be negative")
        if self.pool_timeout_seconds <= 0:
            raise TeamDatabaseConfigurationError("pool timeout must be positive")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> TeamDatabaseConfig:
        values = os.environ if environ is None else environ
        raw_url = values.get(TEAM_DATABASE_URL_ENV)
        if not raw_url:
            raise TeamDatabaseConfigurationError(
                f"{TEAM_DATABASE_URL_ENV} is required for the team profile"
            )
        try:
            database_url = make_url(raw_url)
        except Exception as exc:
            # Never echo a malformed URL: it can contain an app-role password.
            raise TeamDatabaseConfigurationError("team database URL is malformed") from exc
        return cls(
            database_url=database_url,
            expected_role=values.get(TEAM_DATABASE_ROLE_ENV, "owcopilot_app"),
            connect_timeout_seconds=_positive_int(
                values.get("OWCOPILOT_TEAM_DB_CONNECT_TIMEOUT_SECONDS"),
                default=5,
                label="team database connect timeout",
            ),
            pool_size=_positive_int(
                values.get("OWCOPILOT_TEAM_DB_POOL_SIZE"),
                default=5,
                label="team database pool size",
            ),
            max_overflow=_nonnegative_int(
                values.get("OWCOPILOT_TEAM_DB_MAX_OVERFLOW"),
                default=5,
                label="team database max overflow",
            ),
            pool_timeout_seconds=_positive_int(
                values.get("OWCOPILOT_TEAM_DB_POOL_TIMEOUT_SECONDS"),
                default=5,
                label="team database pool timeout",
            ),
        )

    def safe_database_label(self) -> str:
        """Return a diagnostic label that never includes credentials."""

        host = self.database_url.host or "<local>"
        port = self.database_url.port or 5432
        return f"{host}:{port}/{self.database_url.database}"


__all__ = [
    "TEAM_DATABASE_ROLE_ENV",
    "TEAM_DATABASE_URL_ENV",
    "TeamDatabaseConfig",
]
