"""CSV importer for table-driven planning data."""

from __future__ import annotations

import csv
import io
from pathlib import Path

from ..encoding import decode_bytes
from .base import RawObject

# A long lore description in one cell can exceed the stdlib default (128 KB); raise the ceiling so
# legitimate prose imports, but keep it bounded so a runaway cell becomes a clean error, not OOM.
_MAX_CSV_FIELD = 8 * 1024 * 1024
try:
    csv.field_size_limit(_MAX_CSV_FIELD)
except (OverflowError, ValueError):  # pragma: no cover - platform-dependent ceiling
    pass


class CSVImporter:
    def __init__(self, *, default_kind: str = "entity") -> None:
        self.default_kind = default_kind

    def parse(self, path: str | Path) -> list[RawObject]:
        source = Path(path)
        # Decode tolerantly: a CSV exported by Excel on a Chinese Windows machine is GB18030, not
        # UTF-8, and forcing utf-8 would crash on exactly the planner's own files.
        text = decode_bytes(source.read_bytes())
        objects: list[RawObject] = []
        reader = csv.DictReader(io.StringIO(text))
        try:
            for row_number, row in enumerate(reader, start=2):
                data = {str(k): (v if v is not None else "") for k, v in row.items() if k}
                kind = str(data.get("kind") or data.get("object_type") or self.default_kind).lower()
                objects.append(
                    RawObject(
                        kind=kind,
                        data=data,
                        source_path=str(source),
                        line=row_number,
                        row=row_number,
                    )
                )
        except csv.Error as e:  # malformed CSV (a cell beyond the ceiling, NUL bytes, etc.)
            raise ValueError(f"CSV 解析失败，文件可能损坏或字段过大：{e}") from e
        return objects
