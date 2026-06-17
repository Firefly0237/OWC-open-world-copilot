"""Recognize entities + relations from engine data: UE DataTable / Unity ScriptableObject JSON.

Both are "tables of structs/assets", so the core is the same value-domain foreign-key idea as the
spreadsheet adapter — but with engine-native reference shapes made explicit:

* **UE** ``DataTableRowHandle`` serializes as ``{"RowName": "X", ...}`` → a relation to row ``X``
  (the field name is the kind). Bare-string fields whose value matches another row id are foreign
  keys too.
* **Unity** object references serialize as ``{"m_FileID": .., "m_PathID": ..}`` or a GUID string —
  not resolvable without the asset manifest, so we keep them as fields and flag them honestly
  rather than invent a target.

Per-project struct/asset schemas differ, so unmapped fields are preserved in ``metadata`` + listed.
No LLM here. This extends the spirit of the existing ``engine_sync`` import to generic engine rows.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .models import ImportPlan, ProposedEntity, ProposedRelation, SourceRef

_ID_KEYS = {"ue": ("Name", "RowName"), "unity": ("m_Name", "name", "Name")}
_TYPE_KEYS = ("Type", "Category", "type", "m_Type")
_UNITY_REF_KEYS = {"m_PathID", "m_FileID", "fileID", "guid", "m_GUID"}


def _rows(data: Any, dialect: str) -> list[tuple[str, dict[str, Any]]]:
    """Normalize an engine export into (row_id, fields) pairs. Accepts a list of objects, a dict
    keyed by row name, or a Unity wrapper object with a single array field."""
    if isinstance(data, dict):
        # Unity often wraps the real array in one field (e.g. {"rows": [...]}); unwrap it.
        array_fields = [v for v in data.values() if isinstance(v, list)]
        if len(array_fields) == 1 and all(isinstance(x, dict) for x in array_fields[0]):
            data = array_fields[0]
        else:  # dict keyed by row name -> rows
            out: list[tuple[str, dict[str, Any]]] = []
            for key, value in data.items():
                if isinstance(value, dict):
                    out.append((str(key), value))
            if out:
                return out
            data = [data]  # a single ScriptableObject asset
    rows: list[tuple[str, dict[str, Any]]] = []
    for index, item in enumerate(data if isinstance(data, list) else []):
        if not isinstance(item, dict):
            continue
        row_id = ""
        for key in _ID_KEYS.get(dialect, _ID_KEYS["ue"]):
            if item.get(key):
                row_id = str(item[key]).strip()
                break
        rows.append((row_id or f"row_{index}", item))
    return rows


def _row_type(fields: dict[str, Any]) -> str:
    for key in _TYPE_KEYS:
        val = fields.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lower()
    return "concept"


def _handle_target(value: Any) -> str | None:
    """A UE RowHandle dict -> its RowName; otherwise None."""
    if isinstance(value, dict) and value.get("RowName"):
        return str(value["RowName"]).strip()
    return None


def _is_unity_ref(value: Any) -> bool:
    return isinstance(value, dict) and bool(_UNITY_REF_KEYS & set(value))


def _resolves(value: Any, universe: set[str]) -> str | None:
    """A bare scalar whose string value is a known id (a foreign key), else None."""
    if isinstance(value, str | int):
        text = str(value).strip()
        if text in universe:
            return text
    return None


def _field_value(value: Any) -> Any:
    """Keep scalars as-is; serialize complex structs to JSON so metadata stays flat + readable."""
    if isinstance(value, str | int | float | bool):
        return value
    return json.dumps(value, ensure_ascii=False)


def recognize_engine_data(
    data: Any,
    *,
    dialect: str = "ue",
    source_file: str = "",
    canon_ids: Iterable[str] = (),
) -> ImportPlan:
    """Lift an engine data export into a reviewable ImportPlan."""
    rows = _rows(data, dialect)
    row_ids = {rid for rid, _ in rows}
    universe = row_ids | {str(c) for c in canon_ids}
    id_keys = set(_ID_KEYS.get(dialect, ()))

    entities: list[ProposedEntity] = []
    relations: list[ProposedRelation] = []
    warnings: list[str] = []
    unmapped: set[str] = set()

    for row_id, fields in rows:
        meta: dict[str, Any] = {}
        for key, value in fields.items():
            if key in id_keys or key in _TYPE_KEYS:
                continue
            targets: list[str] = []
            # explicit engine reference handle(s)
            handle = _handle_target(value)
            if handle:
                targets.append(handle)
            elif isinstance(value, list):
                targets.extend(t for t in (_handle_target(v) for v in value) if t)
                targets.extend(t for t in (_resolves(v, universe) for v in value) if t)
            elif _is_unity_ref(value):
                meta[key] = value
                unmapped.add(key)
                warnings.append(
                    f"行 {row_id} 字段 {key}：Unity 资产引用(fileID/GUID)，无清单难解引用"
                )
                continue
            elif _resolves(value, universe):
                targets.append(str(value).strip())

            if targets:
                loc = f"{dialect}:{row_id}.{key}"
                for target in targets:
                    relations.append(
                        ProposedRelation(
                            source=row_id, target=target, kind=key,
                            evidence=f"{key} -> {target}",
                            source_ref=SourceRef(file=source_file, locator=loc),
                        )
                    )
            elif isinstance(value, dict | list):
                meta[key] = value  # complex struct we don't model — preserve verbatim
                unmapped.add(key)
            else:
                meta[key] = value

        entities.append(
            ProposedEntity(
                id=row_id,
                name=str(fields.get("DisplayName") or fields.get("m_Name") or row_id),
                type=_row_type(fields),
                fields={k: _field_value(v) for k, v in meta.items()},
                source_ref=SourceRef(file=source_file, locator=f"{dialect}:{row_id}"),
            )
        )

    # foreign-key targets that point outside the imported set (and not in canon) — flag honestly
    for rel in relations:
        if rel.target not in universe:
            warnings.append(f"行「{rel.source}」引用未知对象「{rel.target}」，保留待人审复核")

    return ImportPlan(
        source_format=dialect,
        entities=entities,
        relations=relations,
        unmapped=sorted(unmapped),
        warnings=warnings,
    )
