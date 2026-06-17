"""Per-world JSON ledger for collaboration state (assignments / comments / locks).

Sits under ``<root>/.collab/collab.json`` beside the world — zero canon pollution, exportable
content untouched. (When the platform DB matures it can move there, like the compliance trail.)
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import CollabState

_DIR = ".collab"
_FILE = "collab.json"


class CollabStore:
    def __init__(self, world_root: str | Path) -> None:
        self.path = Path(world_root) / _DIR / _FILE

    def load(self) -> CollabState:
        if not self.path.exists():
            return CollabState()
        return CollabState.model_validate_json(self.path.read_text(encoding="utf-8"))

    def save(self, state: CollabState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
