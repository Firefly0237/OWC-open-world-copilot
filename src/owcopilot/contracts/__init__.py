"""Implemented core domain contracts.

Only symbols accepted in N1.1 are exported here.  Downstream contracts remain
declarations in the registry until their first real consumer is implemented.
"""

from .identity import ArtifactIdentity, OwnershipScope
from .revisions import ContentObjectVersion, ContentRevision, ReleasePointer, RevisionMembership

__all__ = [
    "ArtifactIdentity",
    "ContentObjectVersion",
    "ContentRevision",
    "OwnershipScope",
    "ReleasePointer",
    "RevisionMembership",
]
