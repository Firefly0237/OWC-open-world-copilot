"""Column mapping for table-driven imports."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .importers.base import RawObject


class FieldMapping(BaseModel):
    """Map source column names into canonical content fields."""

    columns: dict[str, str] = Field(default_factory=dict)
    default_kind: str | None = None

    def apply(self, raw: RawObject) -> RawObject:
        data = {self.columns.get(key, key): value for key, value in raw.data.items()}
        kind = str(self.default_kind or data.get("object_type") or data.get("kind") or raw.kind)
        data.setdefault("kind", kind)
        return RawObject(
            kind=kind.lower(),
            data=data,
            source_path=raw.source_path,
            line=raw.line,
            sheet=raw.sheet,
            row=raw.row,
        )


def apply_field_mapping(
    raw_objects: list[RawObject], mapping: FieldMapping | None
) -> list[RawObject]:
    if mapping is None:
        return raw_objects
    return [mapping.apply(raw) for raw in raw_objects]
