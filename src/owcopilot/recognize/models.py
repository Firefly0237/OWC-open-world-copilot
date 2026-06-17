"""The human-editable recognition result — the *clear format* a person revises before it lands.

A recognition run over a foreign game-project file (a spreadsheet, an articy export, …) produces an
``ImportPlan``: proposed entities + relations, the column mapping that was used (the editable knob),
plus an honest list of fields it could NOT map. Every proposal carries where it came from
(``source_ref``), how it was found (``method``: deterministic vs llm) and a confidence — so a human
can read it, fix the mapping, drop a wrong relation, edit a field, and re-apply. ``apply`` consumes
exactly this object, so the edited plan IS the source of truth: recognition never auto-lands.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Method = Literal["deterministic", "llm"]


class SourceRef(BaseModel):
    """Where a proposed item came from, for traceability in review."""

    file: str = ""
    locator: str = ""  # e.g. "row:12", "col:giver", "articy:0x01000000000004D2", "json:$.npcs[3]"


class ProposedEntity(BaseModel):
    id: str
    name: str
    type: str = "concept"  # an EntityType value; unknown types fall back to "concept" on apply
    description: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)  # extra columns -> entity.metadata
    source_ref: SourceRef | None = None
    confidence: float = 1.0
    method: Method = "deterministic"


class ProposedRelation(BaseModel):
    source: str  # entity id
    target: str  # entity id
    kind: str
    evidence: str = ""  # the source text span (LLM) or structural origin (deterministic)
    source_ref: SourceRef | None = None
    confidence: float = 1.0
    method: Method = "deterministic"


class ColumnMapping(BaseModel):
    """How a table's columns map to the model — the main knob a human edits for spreadsheet imports.

    ``relation_columns`` maps a column name to a relation kind: each non-empty cell becomes a
    relation from the row's entity to the entity named by the cell value (a foreign key)."""

    id_column: str | None = None
    name_column: str | None = None
    type_column: str | None = None
    type_constant: str | None = None  # used when every row is the same entity type
    description_column: str | None = None
    relation_columns: dict[str, str] = Field(default_factory=dict)  # column -> relation kind
    ignore_columns: list[str] = Field(default_factory=list)


class ImportPlan(BaseModel):
    """The editable recognition result. Edit it (mapping / proposals) and re-apply."""

    source_format: str  # "table" | "articy" | …
    entities: list[ProposedEntity] = Field(default_factory=list)
    relations: list[ProposedRelation] = Field(default_factory=list)
    column_mapping: ColumnMapping | None = None  # populated for table sources
    columns: list[str] = Field(default_factory=list)  # table header union, so a UI can edit mapping
    variables: list[dict[str, Any]] = Field(default_factory=list)  # e.g. articy GlobalVariables
    unmapped: list[str] = Field(default_factory=list)  # columns/fields not mapped — listed honestly
    warnings: list[str] = Field(default_factory=list)
    # Filled by the pipeline's diff against canon (so review only stages new/changed):
    new: list[str] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {
            "entities": len(self.entities),
            "relations": len(self.relations),
            "llm_relations": sum(1 for r in self.relations if r.method == "llm"),
            "unmapped": len(self.unmapped),
            "new": len(self.new),
            "changed": len(self.changed),
            "unchanged": len(self.unchanged),
        }
