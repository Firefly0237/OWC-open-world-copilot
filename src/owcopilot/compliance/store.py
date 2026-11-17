# 整改工单的按世界持久化（<root>/.compliance/ 下的 JSON 账本）。
"""Per-world persistence for remediation cases — a JSON ledger under ``<root>/.compliance/``.

This ledger is intentionally separate from the WS-P platform audit log (SQLite): remediation cases
live beside the world they concern. The on-disk JSON keeps the trail with zero canon pollution
(it sits beside the world, not inside the content bundle).
"""

from __future__ import annotations

import json
from pathlib import Path

from ..storage.atomic_file import atomic_write_text, read_json_file
from .models import RemediationCase

_DIR = ".compliance"
_FILE = "cases.json"


class CaseStore:
    def __init__(self, world_root: str | Path) -> None:
        self.path = Path(world_root) / _DIR / _FILE

    def load(self) -> dict[str, RemediationCase]:
        if not self.path.exists():
            return {}
        raw = read_json_file(self.path)
        return {cid: RemediationCase.model_validate(data) for cid, data in raw.items()}

    def save(self, cases: dict[str, RemediationCase]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {cid: case.model_dump(mode="json") for cid, case in cases.items()}
        atomic_write_text(
            self.path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        )
