"""Team-authoring collaboration (WS-B): assignments, comments, edit locks, optimistic concurrency.

Built on the per-world JSON ledger + the WS-P role model. Lets multiple authors work a world without
clobbering each other (etag conflict detection + locks) and coordinate (assign + comment threads).
"""

from __future__ import annotations

from .models import Assignment, CollabState, Comment, Lock, etag_for
from .service import (
    ConflictError,
    acquire_lock,
    add_comment,
    assign,
    check_etag,
    release_lock,
    unassign,
)
from .store import CollabStore

__all__ = [
    "Assignment",
    "CollabState",
    "CollabStore",
    "Comment",
    "ConflictError",
    "Lock",
    "acquire_lock",
    "add_comment",
    "assign",
    "check_etag",
    "etag_for",
    "release_lock",
    "unassign",
]
