"""CSV importer for table-driven planning data."""

from __future__ import annotations

import csv
from pathlib import Path

from .base import RawObject


class CSVImporter:
    def __init__(self, *, default_kind: str = "entity") -> None:
        self.default_kind = default_kind

    def parse(self, path: str | Path) -> list[RawObject]:
        source = Path(path)
        objects: list[RawObject] = []
        with source.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
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
        return objects
