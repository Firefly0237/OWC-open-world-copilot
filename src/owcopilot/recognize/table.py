"""Recognize entities + relations from a spreadsheet whose columns we don't know in advance.

Two deterministic jobs, both fully inspectable and overridable by a human:

1. **Column mapping** — guess which column is the id / name / type / description, and which columns
   are *foreign keys* (their cell values name other entities). The guess is returned in the plan's
   ``column_mapping`` so a person can correct it and re-run; nothing here is a black box.
2. **Relation inference** — a column is treated as a foreign key when most of its non-empty values
   match an entity id (other rows here, or an id already in canon). This value-domain test is the
   classic way tabular tools lift a flat table into a graph; cf. Addepto/graph_builder and zjunlp/
   DeepKE, which we studied but did not depend on — they target generic KGs / NLP pipelines, while
   this stays inside OWCopilot's content model and keeps every proposal human-reviewable.

No LLM here. Relation *kinds* default to the column's own name (a knob the human edits); we never
invent entities — a foreign-key cell only becomes a relation, and unknown targets are flagged, not
silently dropped, so reference-integrity audit can catch them after apply.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .models import ColumnMapping, ImportPlan, ProposedEntity, ProposedRelation, SourceRef

_ID_NAMES = {"id", "编号", "key", "slug", "标识", "代号"}
_NAME_NAMES = {"name", "名称", "名字", "title", "标题", "姓名"}
_TYPE_NAMES = {"type", "类型", "kind", "category", "类别", "种类"}
_DESC_NAMES = {"description", "desc", "描述", "简介", "summary", "备注", "note", "说明"}
_MULTIVALUE = re.compile(r"[;,，、/|]+")
_FK_THRESHOLD = 0.6  # a column is a foreign key when ≥60% of its non-empty values resolve to an id


def _norm(col: str) -> str:
    return str(col).strip().lower()


def _columns(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Ordered union of every row's keys (rows may be ragged)."""
    seen: list[str] = []
    for row in rows:
        for key in row:
            if key not in seen:
                seen.append(key)
    return seen


def _cell(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _split_values(raw: str) -> list[str]:
    return [part.strip() for part in _MULTIVALUE.split(raw) if part.strip()]


def _pick(columns: Iterable[str], names: set[str]) -> str | None:
    for col in columns:
        if _norm(col) in names:
            return col
    return None


def infer_table_mapping(
    rows: Sequence[Mapping[str, Any]],
    *,
    canon_ids: Iterable[str] = (),
) -> ColumnMapping:
    """Best-effort guess at how columns map. Always returned so the human can correct it."""
    columns = _columns(rows)
    first = columns[0] if columns else None
    id_column = _pick(columns, _ID_NAMES) or _pick(columns, _NAME_NAMES) or first
    name_column = _pick(columns, _NAME_NAMES) or id_column
    type_column = _pick(columns, _TYPE_NAMES)
    description_column = _pick(columns, _DESC_NAMES)

    # Foreign-key inference: a column whose values mostly resolve to a known id becomes a relation.
    id_universe: set[str] = {str(c) for c in canon_ids}
    if id_column is not None:
        id_universe |= {_cell(row.get(id_column)) for row in rows if _cell(row.get(id_column))}
    structural = {id_column, name_column, type_column, description_column}
    relation_columns: dict[str, str] = {}
    for col in columns:
        if col in structural:
            continue
        values = [v for row in rows for v in _split_values(_cell(row.get(col)))]
        if not values:
            continue
        hits = sum(1 for v in values if v in id_universe)
        if hits and hits / len(values) >= _FK_THRESHOLD:
            relation_columns[col] = _norm(col).replace(" ", "_")

    return ColumnMapping(
        id_column=id_column,
        name_column=name_column,
        type_column=type_column,
        description_column=description_column,
        relation_columns=relation_columns,
    )


def recognize_table(
    rows: Sequence[Mapping[str, Any]],
    *,
    mapping: ColumnMapping | None = None,
    source_file: str = "",
    canon_ids: Iterable[str] = (),
) -> ImportPlan:
    """Turn table rows into a reviewable ImportPlan. Pass ``mapping`` to override the guess."""
    canon = {str(c) for c in canon_ids}
    mapping = mapping or infer_table_mapping(rows, canon_ids=canon)
    columns = _columns(rows)
    structural = {
        mapping.id_column,
        mapping.name_column,
        mapping.type_column,
        mapping.description_column,
    }
    field_columns = [
        c
        for c in columns
        if c not in structural
        and c not in mapping.relation_columns
        and c not in mapping.ignore_columns
    ]

    entities: list[ProposedEntity] = []
    relations: list[ProposedRelation] = []
    warnings: list[str] = []
    row_ids: set[str] = set()

    for index, row in enumerate(rows):
        ent_id = _cell(row.get(mapping.id_column)) if mapping.id_column else ""
        if not ent_id:
            warnings.append(f"第 {index} 行缺少 id（列「{mapping.id_column}」为空），已跳过")
            continue
        if ent_id in row_ids:
            warnings.append(f"第 {index} 行 id「{ent_id}」重复，已跳过")
            continue
        row_ids.add(ent_id)
        name = _cell(row.get(mapping.name_column)) if mapping.name_column else ""
        ent_type = mapping.type_constant or (
            _cell(row.get(mapping.type_column)) if mapping.type_column else ""
        )
        desc = _cell(row.get(mapping.description_column)) if mapping.description_column else ""
        entities.append(
            ProposedEntity(
                id=ent_id,
                name=name or ent_id,
                type=ent_type or "concept",
                description=desc,
                fields={c: row[c] for c in field_columns if c in row and _cell(row.get(c))},
                source_ref=SourceRef(file=source_file, locator=f"row:{index}"),
            )
        )

    known = row_ids | canon
    for index, row in enumerate(rows):
        ent_id = _cell(row.get(mapping.id_column)) if mapping.id_column else ""
        if not ent_id or ent_id not in row_ids:
            continue
        for col, kind in mapping.relation_columns.items():
            for target in _split_values(_cell(row.get(col))):
                if target not in known:
                    warnings.append(
                        f"第 {index} 行列「{col}」指向未知对象「{target}」——留待人审复核"
                    )
                relations.append(
                    ProposedRelation(
                        source=ent_id,
                        target=target,
                        kind=kind,
                        evidence=f"{col} = {target}",
                        source_ref=SourceRef(file=source_file, locator=f"row:{index}|col:{col}"),
                    )
                )

    unmapped = [c for c in field_columns if c]  # fields kept as metadata, surfaced honestly
    return ImportPlan(
        source_format="table",
        entities=entities,
        relations=relations,
        column_mapping=mapping,
        columns=columns,
        unmapped=unmapped,
        warnings=warnings,
    )
