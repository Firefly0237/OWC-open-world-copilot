# 协作状态的按世界 JSON 账本（指派/评论/锁）。
"""Per-world JSON ledger for collaboration state (assignments / comments / locks).

Sits under ``<root>/.collab/collab.json`` beside the world — zero canon pollution, exportable
content untouched. (When the platform DB matures it can move there, like the compliance trail.)
"""

from __future__ import annotations

import json
from pathlib import Path

from ..storage.atomic_file import atomic_write_text, read_json_file
from .models import CollabState

_DIR = ".collab"
_FILE = "collab.json"


class CollabStore:
    def __init__(self, world_root: str | Path) -> None:
        self.path = Path(world_root) / _DIR / _FILE

    def load(self) -> CollabState:
        if not self.path.exists():
            return CollabState()
        return CollabState.model_validate(read_json_file(self.path))

    def save(self, state: CollabState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.path,
            json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True),
        )
