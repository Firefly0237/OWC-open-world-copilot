"""Build a WorldBible from a writer-friendly markdown doc.

Supported markdown format:

    ## NPCs
    - Aldric — Caravan master who runs supply routes [merchant, quest_giver]
    ## Locations
    - Northwatch — Fortified town guarding the northern pass
    ## Relations
    - Aldric -> Northwatch : located_in

P1+: also support CSV exports and LLM-based extraction from free prose.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import Entity, EntityType, Relation, WorldBible

_TYPE_BY_SECTION = {
    "npcs": EntityType.NPC,
    "locations": EntityType.LOCATION,
    "factions": EntityType.FACTION,
    "items": EntityType.ITEM,
    "events": EntityType.EVENT,
}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def parse_worldbible_md(text: str) -> WorldBible:
    wb = WorldBible()
    name_to_id: dict[str, str] = {}
    section: str | None = None

    for raw in text.splitlines():
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
            tags: list[str] = []
            mtag = re.search(r"\[(.*?)\]\s*$", body)
            if mtag:
                tags = [t.strip() for t in mtag.group(1).split(",") if t.strip()]
                body = body[: mtag.start()].strip()
            parts = re.split(r"\s+(?:—|-)\s+", body, maxsplit=1)
            name = parts[0].strip()
            desc = parts[1].strip() if len(parts) > 1 else ""
            eid = _slug(name)
            wb.add_entity(
                Entity(
                    id=eid, name=name, type=_TYPE_BY_SECTION[section], description=desc, tags=tags
                )
            )
            name_to_id[name] = eid
        elif section == "relations":
            m = re.match(r"^(.*?)\s*->\s*(.*?)\s*:\s*(.*)$", body)
            if m:
                wb.add_relation(
                    Relation(
                        source=m.group(1).strip(),
                        target=m.group(2).strip(),
                        kind=m.group(3).strip(),
                    )
                )

    # resolve relation endpoints (names -> ids)
    for r in wb.relations:
        r.source = name_to_id.get(r.source, _slug(r.source))
        r.target = name_to_id.get(r.target, _slug(r.target))
    return wb


def ingest_from_file(path: str | Path) -> WorldBible:
    return parse_worldbible_md(Path(path).read_text(encoding="utf-8"))
