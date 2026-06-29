"""JSON importer for native content files and simple exported arrays."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..encoding import decode_bytes
from .base import RawObject


class JSONImporter:
    def parse(self, path: str | Path) -> list[RawObject]:
        source = Path(path)
        # Decode tolerantly (a JSON exported on a Chinese Windows box may be GB18030/UTF-16, not
        # UTF-8) and turn a malformed file into a clean domain error like the xlsx/docx importers.
        text = decode_bytes(source.read_bytes())
        if source.suffix.lower() == ".jsonl":
            objects: list[RawObject] = []
            for line_number, raw in enumerate(text.splitlines(), 1):
                if not raw.strip():
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as e:
                    raise ValueError(f"第 {line_number} 行不是合法 JSON，请检查该行格式。") from e
                except RecursionError as e:
                    raise ValueError(
                        f"第 {line_number} 行的 JSON 嵌套层数过深，无法解析。"
                        "请减少该行数据的嵌套层级后重试。"
                    ) from e
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
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(
                "文件不是合法 JSON，可能已损坏或格式不对（应是对象或对象数组）。"
            ) from e
        except RecursionError as e:
            # Pathologically deep nesting (~thousands of levels) makes json.loads exceed
            # Python's recursion limit. Convert the raw RecursionError into a guided
            # domain error instead of leaking the interpreter-level message.
            raise ValueError(
                f"JSON 文件嵌套层数过深，无法解析（可能是异常或恶意构造的文件）。"
                f"请减少嵌套层级后重试。（文件：{source}）"
            ) from e
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
    # BUG-6: top-level scalars (number, string, bool, null) are never valid content files.
    # Silently returning [] would mask a corrupt or mis-named file; raise a friendly error instead.
    type_name = type(data).__name__
    raise ValueError(
        f"JSON 文件顶层必须是对象（{{...}}）或对象数组（[...]），"
        f"但实际得到的是 {type_name}（{data!r:.80}）。"
        f"请检查文件格式是否正确。（文件：{source_path}）"
    )


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
