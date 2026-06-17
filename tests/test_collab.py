"""WS-B · collaboration: optimistic concurrency (etag), assignments, comments, edit locks."""

from __future__ import annotations

import pytest

from owcopilot.app.actions import (
    assign_action,
    collab_state_action,
    comment_action,
    lock_action,
    update_quest_action,
)
from owcopilot.collab import (
    CollabState,
    ConflictError,
    acquire_lock,
    add_comment,
    assign,
    check_etag,
    etag_for,
)
from owcopilot.content.models import ContentBundle, Quest
from owcopilot.content.store import ContentStore


# --------------------------------------------------------------- pure service
def test_etag_changes_with_content_and_guards_writes() -> None:
    q1 = Quest(id="q", title="A")
    q2 = Quest(id="q", title="B")
    assert etag_for(q1) != etag_for(q2)
    check_etag(current=etag_for(q1), if_match=None)  # no etag sent -> always ok
    check_etag(current=etag_for(q1), if_match=etag_for(q1))  # match -> ok
    with pytest.raises(ConflictError, match="已被他人修改"):
        check_etag(current=etag_for(q2), if_match=etag_for(q1))  # stale -> conflict


def test_lock_blocks_other_holder_but_not_self() -> None:
    state = CollabState()
    acquire_lock(state, object_ref="quest:q", holder="alice")
    acquire_lock(state, object_ref="quest:q", holder="alice")  # re-acquire own is fine
    with pytest.raises(ConflictError, match="锁定"):
        acquire_lock(state, object_ref="quest:q", holder="bob")


def test_comment_and_assign_validation() -> None:
    state = CollabState()
    add_comment(state, object_ref="quest:q", author="alice", body="看一下结局分支")
    assert len(state.comments["quest:q"]) == 1
    with pytest.raises(ValueError, match="评论内容"):
        add_comment(state, object_ref="quest:q", author="alice", body="   ")
    assign(state, object_ref="quest:q", assignee="bob", by="alice", note="你来改")
    assert state.assignments["quest:q"].assignee == "bob"


# --------------------------------------------------------------- action level
def _world(root) -> None:
    ContentStore(root).save(ContentBundle(quests={"q": Quest(id="q", title="盐风驰援")}))


def test_optimistic_concurrency_rejects_stale_edit(tmp_path) -> None:
    root = tmp_path / "content"
    _world(root)
    fresh = etag_for(ContentStore(root).load().quests["q"])

    # editor A edits with the fresh etag -> ok, gets a NEW etag
    a = update_quest_action(root, quest_id="q", title="盐风驰援 · A", if_match=fresh)
    new_etag = a["etag"]
    assert new_etag != fresh

    # editor B still holds the OLD etag -> their write is refused (lost-update prevented)
    with pytest.raises(ValueError, match="已被他人修改"):
        update_quest_action(root, quest_id="q", title="盐风驰援 · B", if_match=fresh)

    # B refreshes (uses the new etag) -> ok
    update_quest_action(root, quest_id="q", title="盐风驰援 · B", if_match=new_etag)


def test_collab_actions_persist_and_read_back(tmp_path) -> None:
    root = tmp_path / "content"
    _world(root)
    assign_action(root, object_ref="quest:q", assignee="bob", by="alice", note="你来改")
    comment_action(root, object_ref="quest:q", author="alice", body="注意结局分支")
    locked = lock_action(root, object_ref="quest:q", holder="alice")
    assert locked["locked"] is True

    state = collab_state_action(root, object_ref="quest:q")
    assert state["assignment"]["assignee"] == "bob"
    assert len(state["comments"]) == 1 and state["lock"]["holder"] == "alice"

    # another holder is blocked; the holder can release
    with pytest.raises(ValueError, match="锁定"):
        lock_action(root, object_ref="quest:q", holder="bob")
    released = lock_action(root, object_ref="quest:q", holder="alice", release=True)
    assert released["locked"] is False
