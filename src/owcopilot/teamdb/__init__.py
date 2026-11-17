"""PostgreSQL team truth-source boundary."""

from .config import TeamDatabaseConfig
from .errors import (
    RevisionConflict,
    TeamAuthorizationError,
    TeamCanonicalUnavailableError,
    TeamDatabaseConfigurationError,
    TeamDatabaseError,
    TeamDatabaseUnavailable,
    TeamIntakeConflict,
    TeamIntegrityError,
    TeamNotFoundError,
    TeamSchemaMismatch,
)
from .intake_repository import LineageKind, TeamIntakeRepository
from .repository import (
    CanonicalState,
    MembershipGrant,
    PointerState,
    TeamRepository,
)
from .session import DatabaseHealth, TeamDatabase

__all__ = [
    "CanonicalState",
    "DatabaseHealth",
    "MembershipGrant",
    "LineageKind",
    "PointerState",
    "RevisionConflict",
    "TeamAuthorizationError",
    "TeamCanonicalUnavailableError",
    "TeamDatabase",
    "TeamDatabaseConfig",
    "TeamDatabaseConfigurationError",
    "TeamDatabaseError",
    "TeamDatabaseUnavailable",
    "TeamIntegrityError",
    "TeamIntakeConflict",
    "TeamIntakeRepository",
    "TeamNotFoundError",
    "TeamRepository",
    "TeamSchemaMismatch",
]
