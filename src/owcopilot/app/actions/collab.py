# 协作账本：状态/分派/评论/加解锁。
# Collaboration ledger: state, assign, comment, lock.

"""collab actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .._common import open_project as _project


# >>> WS-B collaboration (assignments / comments / locks / concurrency) >>>
def collab_state_action(
    content_root: str | Path,
    *,
    object_ref: str | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """The collaboration ledger (assignments/comments/locks), optionally scoped to one object."""
    from ...collab import CollabStore

    with _project(content_root, sqlite_path) as project:
        state = CollabStore(project.content_store.root).load()
        if object_ref is not None:
            return {
                "object_ref": object_ref,
                "assignment": (
                    state.assignments[object_ref].model_dump(mode="json")
                    if object_ref in state.assignments
                    else None
                ),
                "comments": [c.model_dump(mode="json") for c in state.comments.get(object_ref, [])],
                "lock": (
                    state.locks[object_ref].model_dump(mode="json")
                    if object_ref in state.locks
                    else None
                ),
            }
        return state.model_dump(mode="json")


def assign_action(
    content_root: str | Path,
    *,
    object_ref: str,
    assignee: str,
    by: str,
    note: str = "",
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ...collab import CollabStore, assign, unassign

    with _project(content_root, sqlite_path) as project:
        store = CollabStore(project.content_store.root)
        state = store.load()
        if assignee.strip():
            assign(state, object_ref=object_ref, assignee=assignee, by=by, note=note)
        else:
            unassign(state, object_ref=object_ref)
        store.save(state)
        return {"object_ref": object_ref, "assignee": assignee.strip()}


def comment_action(
    content_root: str | Path,
    *,
    object_ref: str,
    author: str,
    body: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ...collab import CollabStore, add_comment

    with _project(content_root, sqlite_path) as project:
        store = CollabStore(project.content_store.root)
        state = store.load()
        comment = add_comment(state, object_ref=object_ref, author=author, body=body)
        store.save(state)
        return {"comment": comment.model_dump(mode="json")}


def lock_action(
    content_root: str | Path,
    *,
    object_ref: str,
    holder: str,
    release: bool = False,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ...collab import CollabStore, ConflictError, acquire_lock, release_lock

    with _project(content_root, sqlite_path) as project:
        store = CollabStore(project.content_store.root)
        state = store.load()
        if release:
            release_lock(state, object_ref=object_ref, holder=holder)
            store.save(state)
            return {"object_ref": object_ref, "locked": False}
        try:
            lock = acquire_lock(state, object_ref=object_ref, holder=holder)
        except ConflictError as exc:
            raise ValueError(str(exc)) from exc
        store.save(state)
        return {"object_ref": object_ref, "locked": True, "lock": lock.model_dump(mode="json")}
