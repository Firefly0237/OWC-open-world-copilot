"""Collaboration operations over the per-world ledger (WS-B): assign, comment, lock, concurrency.

Pure functions over a CollabState (the action layer loads/saves). A lock held by another user blocks
acquisition; a stale etag on a guarded write is a conflict — both make concurrent editing safe.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from .models import Assignment, CollabState, Comment, Lock


class ConflictError(ValueError):
    """A concurrent-edit conflict: stale etag, or a lock held by someone else."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def assign(
    state: CollabState, *, object_ref: str, assignee: str, by: str, note: str = ""
) -> Assignment:
    if not by.strip():
        raise ValueError("请先填写署名")
    item = Assignment(object_ref=object_ref, assignee=assignee.strip(), assigned_by=by, note=note)
    state.assignments[object_ref] = item
    return item


def unassign(state: CollabState, *, object_ref: str) -> None:
    state.assignments.pop(object_ref, None)


def add_comment(state: CollabState, *, object_ref: str, author: str, body: str) -> Comment:
    if not author.strip():
        raise ValueError("请先填写署名")
    if not body.strip():
        raise ValueError("评论内容不能为空")
    cid = "c_" + hashlib.sha256(f"{object_ref}|{author}|{_now()}|{body}".encode()).hexdigest()[:10]
    comment = Comment(id=cid, object_ref=object_ref, author=author, body=body.strip())
    state.comments.setdefault(object_ref, []).append(comment)
    return comment


def acquire_lock(state: CollabState, *, object_ref: str, holder: str) -> Lock:
    """Take an edit lock. Re-acquiring your own lock is fine; another holder's lock blocks."""
    if not holder.strip():
        raise ValueError("请先填写署名")
    existing = state.locks.get(object_ref)
    if existing is not None and existing.holder != holder:
        raise ConflictError(f"该对象正被 {existing.holder} 编辑锁定")
    lock = Lock(object_ref=object_ref, holder=holder)
    state.locks[object_ref] = lock
    return lock


def release_lock(state: CollabState, *, object_ref: str, holder: str) -> None:
    existing = state.locks.get(object_ref)
    if existing is not None and existing.holder == holder:
        state.locks.pop(object_ref, None)


def check_etag(*, current: str, if_match: str | None) -> None:
    """Optimistic-concurrency guard: if the caller sent an etag and it no longer matches, the object
    changed underneath them — raise rather than clobber the other edit."""
    if if_match is not None and if_match != current:
        raise ConflictError("内容已被他人修改，请刷新后重试")
