from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from owcopilot.service.pagination import PaginationCursorError, paginate  # noqa: E402


def test_cursor_page_reaches_every_item_and_detects_a_stale_snapshot() -> None:
    items = [{"id": f"issue-{index:03d}"} for index in range(205)]

    first = paginate(items, limit=100)
    second = paginate(items, limit=100, cursor=first.next_cursor)
    third = paginate(items, limit=100, cursor=second.next_cursor)

    observed = [*first.items, *second.items, *third.items]
    assert [item["id"] for item in observed] == [item["id"] for item in items]
    assert first.total == 205
    assert third.next_cursor is None
    assert first.snapshot_etag == second.snapshot_etag == third.snapshot_etag

    changed = [*items, {"id": "issue-new"}]
    with pytest.raises(PaginationCursorError, match="发生变化") as caught:
        paginate(changed, limit=100, cursor=first.next_cursor)
    assert caught.value.stale is True
