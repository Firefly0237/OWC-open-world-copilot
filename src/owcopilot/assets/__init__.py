"""WS-I · asset linking: attach EXISTING media (concept art / audio / maps / links) to objects.

No generation, no upload pipeline — just references (a URL or a path the studio already manages),
so a quest/entity/region can carry its art and audio. State lives in a per-world JSON ledger beside
the world (zero canon pollution); the reference itself points at media the team stores elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

_MAX_URI = 2048
_MAX_TITLE = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")  # a uri/title is single-line: strip ALL control chars


class AssetKind(str, Enum):
    IMAGE = "image"
    AUDIO = "audio"
    MAP = "map"
    LINK = "link"


class Asset(BaseModel):
    id: str
    object_ref: str
    kind: AssetKind
    uri: str
    title: str = ""
    at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class AssetState(BaseModel):
    assets: dict[str, list[Asset]] = Field(default_factory=dict)  # object_ref -> [assets]


def attach(
    state: AssetState, *, object_ref: str, kind: AssetKind, uri: str, title: str = ""
) -> Asset:
    # Normalize first, then key the dedup hash on the NORMALIZED uri — otherwise "x.png" and
    # "x.png " (a stray space) hash differently but store the same uri, leaking a duplicate.
    clean_uri = _CONTROL.sub("", uri).strip()
    if not clean_uri:
        raise ValueError("资产链接/路径不能为空")
    if len(clean_uri) > _MAX_URI:
        raise ValueError("资产链接/路径过长（请控制在 2048 字符内）")
    clean_title = _CONTROL.sub("", title).strip()[:_MAX_TITLE]
    aid = (
        "asset_"
        + hashlib.sha256(f"{object_ref}|{kind.value}|{clean_uri}".encode()).hexdigest()[:10]
    )
    asset = Asset(id=aid, object_ref=object_ref, kind=kind, uri=clean_uri, title=clean_title)
    thread = state.assets.setdefault(object_ref, [])
    if not any(a.id == aid for a in thread):  # idempotent on the same (ref, kind, uri)
        thread.append(asset)
    return asset


def detach(state: AssetState, *, asset_id: str) -> bool:
    for ref, thread in list(state.assets.items()):
        kept = [a for a in thread if a.id != asset_id]
        if len(kept) != len(thread):
            if kept:
                state.assets[ref] = kept
            else:
                del state.assets[ref]
            return True
    return False


class AssetStore:
    def __init__(self, world_root: str | Path) -> None:
        self.path = Path(world_root) / ".assets" / "assets.json"

    def load(self) -> AssetState:
        if not self.path.exists():
            return AssetState()
        return AssetState.model_validate_json(self.path.read_text(encoding="utf-8"))

    def save(self, state: AssetState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )


__all__ = ["Asset", "AssetKind", "AssetState", "AssetStore", "attach", "detach"]
