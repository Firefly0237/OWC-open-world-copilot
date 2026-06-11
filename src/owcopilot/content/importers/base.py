"""Importer protocol.

Importers parse source files into raw typed objects. Normalization is intentionally separate so
project-specific column mappings and ID policies live in one place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel


class RawObject(BaseModel):
    kind: str
    data: dict[str, Any]
    source_path: str
    line: int | None = None
    sheet: str | None = None
    row: int | None = None


class Importer(Protocol):
    def parse(self, path: str | Path) -> list[RawObject]: ...
