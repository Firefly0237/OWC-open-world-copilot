"""Markdown importer for the conventional lore shape (NPCs / Locations / Factions / Relations)."""

from __future__ import annotations

import re
from pathlib import Path

from ..encoding import decode_bytes
from .base import RawObject

_TYPE_BY_SECTION = {
    "npcs": "npc",
    "locations": "location",
    "factions": "faction",
    "items": "item",
    "events": "event",
    "regions": "region",
    "organizations": "organization",
    "concepts": "concept",
    "terms": "term",
}


class MarkdownImporter:
    def parse(self, path: str | Path) -> list[RawObject]:
        source = Path(path)
        # Tolerant decode (GB18030/UTF-16 markdown from a Chinese editor), matching the CSV/JSON
        # importers rather than crashing on a non-UTF-8 file.
        return parse_markdown(decode_bytes(source.read_bytes()), source_path=str(source))


def parse_markdown(text: str, *, source_path: str = "<memory>") -> list[RawObject]:
    objects: list[RawObject] = [
        RawObject(
            kind="style_guide",
            data={"id": "style_guide", "body": text},
            source_path=source_path,
            line=1,
        )
    ]
    section: str | None = None
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        header = re.match(r"^##\s+(.*)$", line)
        if header:
            section = header.group(1).strip().lower()
            continue
        bullet = re.match(r"^\s*-\s+(.*)$", line)
        if not bullet or section is None:
            continue
        body = bullet.group(1).strip()
        if section in _TYPE_BY_SECTION:
            objects.append(
                RawObject(
                    kind="entity",
                    data=_parse_entity_body(body, entity_type=_TYPE_BY_SECTION[section]),
                    source_path=source_path,
                    line=line_number,
                )
            )
        elif section == "relations":
            relation = _parse_relation_body(body)
            if relation is not None:
                objects.append(
                    RawObject(
                        kind="relation",
                        data=relation,
                        source_path=source_path,
                        line=line_number,
                    )
                )
    return objects


def _parse_entity_body(body: str, *, entity_type: str) -> dict[str, object]:
    tags: list[str] = []
    mtag = re.search(r"\[(.*?)\]\s*$", body)
    if mtag:
        tags = [t.strip() for t in mtag.group(1).split(",") if t.strip()]
        body = body[: mtag.start()].strip()
    explicit_id: str | None = None
    colon = re.match(r"^(.*?)\s*:\s*(.*)$", body)
    if colon:
        head = colon.group(1).strip()
        description = colon.group(2).strip()
    else:
        parts = re.split(r"\s+(?:—|-)\s+", body, maxsplit=1)
        head = parts[0].strip()
        description = parts[1].strip() if len(parts) > 1 else ""
    id_match = re.match(r"^(.*?)\s*\(([^()]+)\)\s*$", head)
    if id_match:
        name = id_match.group(1).strip()
        explicit_id = id_match.group(2).strip()
    else:
        name = head
    data: dict[str, object] = {
        "name": name,
        "type": entity_type,
        "description": description,
        "tags": tags,
    }
    if explicit_id:
        data["id"] = explicit_id
    for tag in tags:
        key_value = re.match(r"^([a-zA-Z_][\w-]*)\s*:\s*(.+)$", tag)
        if key_value:
            data[key_value.group(1).strip()] = key_value.group(2).strip()
    return data


def _parse_relation_body(body: str) -> dict[str, str] | None:
    match = re.match(r"^(.*?)\s*->\s*(.*?)\s*:\s*(.*)$", body)
    if match is not None:
        return {
            "source": match.group(1).strip(),
            "target": match.group(2).strip(),
            "kind": match.group(3).strip(),
        }
    parts = body.split()
    if len(parts) >= 3:
        return {"source": parts[0].strip(), "kind": parts[1].strip(), "target": parts[2].strip()}
    return None
