"""Fail-closed error taxonomy for the PostgreSQL team data plane."""

from __future__ import annotations

from ..errors import OWCopilotError


class TeamDatabaseError(OWCopilotError, RuntimeError):
    """Base class for errors crossing the team database boundary."""


class TeamDatabaseConfigurationError(TeamDatabaseError, ValueError):
    """The team database configuration is absent or unsafe."""


class TeamDatabaseUnavailable(TeamDatabaseError):
    """PostgreSQL cannot be reached or a connection became unusable."""


class TeamSchemaMismatch(TeamDatabaseError):
    """The database is not at the application schema head or uses an unsafe role."""


class TeamAuthorizationError(TeamDatabaseError, PermissionError):
    """The authenticated subject has no active membership with a sufficient role."""


class TeamNotFoundError(TeamDatabaseError, LookupError):
    """A tenant-scoped database resource does not exist."""


class TeamCanonicalUnavailableError(TeamNotFoundError):
    """The requested authoring or serving head has not been created."""


class TeamIntegrityError(TeamDatabaseError):
    """Persisted canonical data or a proposed revision violates its contract."""


class RevisionConflict(TeamDatabaseError):
    """The expected release-pointer head or lock generation is stale."""


class TeamIntakeConflict(TeamDatabaseError):
    """An intake run state, immutable identity, or human-governance write conflicts."""


__all__ = [
    "RevisionConflict",
    "TeamAuthorizationError",
    "TeamCanonicalUnavailableError",
    "TeamDatabaseConfigurationError",
    "TeamDatabaseError",
    "TeamDatabaseUnavailable",
    "TeamIntegrityError",
    "TeamIntakeConflict",
    "TeamNotFoundError",
    "TeamSchemaMismatch",
]
