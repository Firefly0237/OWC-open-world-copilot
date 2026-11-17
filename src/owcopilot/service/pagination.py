"""Small, deterministic cursor pagination helpers for REST collection endpoints.

The cursor is intentionally opaque to clients and is bound to a snapshot hash of the
filtered collection.  If the collection changes between requests, continuing with the
old cursor fails with HTTP 409 instead of silently duplicating or skipping work items.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class PaginationCursorError(ValueError):
    """An invalid or stale opaque cursor."""

    def __init__(self, message: str, *, stale: bool = False) -> None:
        super().__init__(message)
        self.stale = stale


@dataclass(frozen=True)
class CursorPage(Generic[T]):
    items: list[T]
    total: int
    next_cursor: str | None
    snapshot_etag: str
    offset: int
    page_cursor: str | None
    previous_cursor: str | None


def _snapshot_etag(items: list[Any]) -> str:
    payload = json.dumps(
        items,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def _encode_cursor(*, offset: int, snapshot_etag: str) -> str:
    raw = json.dumps(
        {"v": 1, "offset": offset, "snapshot_etag": snapshot_etag},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[int, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise ValueError
        offset = payload.get("offset")
        snapshot_etag = payload.get("snapshot_etag")
        if not isinstance(offset, int) or offset < 0 or not isinstance(snapshot_etag, str):
            raise ValueError
        return offset, snapshot_etag
    except (UnicodeEncodeError, ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
        raise PaginationCursorError("分页游标无效，请从第一页重新加载。") from exc


def paginate(
    items: list[T],
    *,
    limit: int,
    cursor: str | None = None,
    start_offset: int = 0,
    snapshot_items: list[Any] | None = None,
) -> CursorPage[T]:
    """Return one stable page from an already-filtered, deterministically ordered collection.

    ``snapshot_items`` lets a caller bind the opaque cursor to surrounding mutable state without
    putting that metadata into the returned collection.  For example, a page of immutable intake
    segments is still stale after the run's governance decision changes.
    """

    snapshot_etag = _snapshot_etag(items if snapshot_items is None else snapshot_items)
    if start_offset < 0:
        raise PaginationCursorError("分页起点无效，请从第一页重新加载。")
    offset = start_offset
    if cursor:
        offset, cursor_etag = _decode_cursor(cursor)
        if cursor_etag != snapshot_etag:
            raise PaginationCursorError(
                "列表在翻页期间发生变化，请从第一页重新加载。",
                stale=True,
            )
        if offset > len(items):
            raise PaginationCursorError("分页游标超出当前列表范围，请从第一页重新加载。")

    end = min(len(items), offset + limit)
    next_cursor = (
        _encode_cursor(offset=end, snapshot_etag=snapshot_etag) if end < len(items) else None
    )
    return CursorPage(
        items=items[offset:end],
        total=len(items),
        next_cursor=next_cursor,
        snapshot_etag=snapshot_etag,
        offset=offset,
        page_cursor=(
            _encode_cursor(offset=offset, snapshot_etag=snapshot_etag) if offset else None
        ),
        previous_cursor=(
            _encode_cursor(offset=max(0, offset - limit), snapshot_etag=snapshot_etag)
            if offset
            else None
        ),
    )


def paginate_window(
    *,
    total: int,
    snapshot_etag: str,
    limit: int,
    cursor: str | None,
    load: Callable[[int, int], list[T]],
) -> CursorPage[T]:
    """Page a datastore-backed collection without materialising the whole collection.

    The datastore supplies a constant-memory snapshot marker and a bounded ``offset, limit``
    loader.  Cursor validation and encoding stay byte-compatible with :func:`paginate`, including
    stale-snapshot detection.  This is used for collections such as Agent run history that can
    grow for the lifetime of a project.
    """

    if total < 0 or limit <= 0 or not snapshot_etag:
        raise PaginationCursorError("分页参数无效，请从第一页重新加载。")
    offset = 0
    if cursor:
        offset, cursor_etag = _decode_cursor(cursor)
        if cursor_etag != snapshot_etag:
            raise PaginationCursorError(
                "列表在翻页期间发生变化，请从第一页重新加载。",
                stale=True,
            )
        if offset > total:
            raise PaginationCursorError("分页游标超出当前列表范围，请从第一页重新加载。")

    items = load(offset, limit)
    if len(items) > limit:
        raise PaginationCursorError("分页数据源返回了超出上限的结果。")
    end = offset + len(items)
    if end < min(total, offset + limit):
        raise PaginationCursorError("分页数据源返回了不完整的结果。")
    next_cursor = _encode_cursor(offset=end, snapshot_etag=snapshot_etag) if end < total else None
    return CursorPage(
        items=items,
        total=total,
        next_cursor=next_cursor,
        snapshot_etag=snapshot_etag,
        offset=offset,
        page_cursor=(
            _encode_cursor(offset=offset, snapshot_etag=snapshot_etag) if offset else None
        ),
        previous_cursor=(
            _encode_cursor(offset=max(0, offset - limit), snapshot_etag=snapshot_etag)
            if offset
            else None
        ),
    )
