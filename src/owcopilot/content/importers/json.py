"""JSON importer for native content files and simple exported arrays."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import RawObject


class JSONImporter:
    def parse(self, path: str | Path) -> list[RawObject]:
        source = Path(path)
        if source.suffix.lower() == ".jsonl":
            objects: list[RawObject] = []
            for line_number, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
                if not raw.strip():
                    continue
                data = json.loads(raw)
                if isinstance(data, dict):
                    objects.append(
                        RawObject(
                            kind=_kind_for(data),
                            data=data,
                            source_path=str(source),
                            line=line_number,
                        )
                    )
            return objects
        data = json.loads(source.read_text(encoding="utf-8"))
        return _objects_from_json(data, source_path=str(source))


def _objects_from_json(data: Any, *, source_path: str) -> list[RawObject]:
    if isinstance(data, list):
        return [
            RawObject(kind=_kind_for(item), data=item, source_path=source_path, line=None)
            for item in data
            if isinstance(item, dict)
        ]
    if isinstance(data, dict):
        if "kind" in data or "type" in data or "id" in data:
            return [RawObject(kind=_kind_for(data), data=data, source_path=source_path)]
        objects: list[RawObject] = []
        for key, value in data.items():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        payload = dict(item)
                        payload.setdefault("kind", key.rstrip("s"))
                        objects.append(_raw_object(payload, source_path=source_path))
            elif isinstance(value, dict):
                payload = dict(value)
                payload.setdefault("kind", key.rstrip("s"))
                objects.append(_raw_object(payload, source_path=source_path))
        return objects
    return []


def _kind_for(data: dict[str, Any]) -> str:
    if {"source", "target", "kind"}.issubset(data):
        return "relation"
    if {"quest_id", "event_id"}.issubset(data) or {"quest_id", "event"}.issubset(data):
        return "quest_event_ref"
    if "text_key" in data and ("locale" in data or "text" in data):
        return "localized_text"
    raw = data.get("kind") or data.get("object_type") or data.get("type") or "entity"
    return str(raw).lower()


def _raw_object(data: dict[str, Any], *, source_path: str) -> RawObject:
    return RawObject(kind=_kind_for(data), data=data, source_path=source_path)
