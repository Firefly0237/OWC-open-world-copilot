"""Per-world persistence for remediation cases — a JSON ledger under ``<root>/.compliance/``.

WS-D ships before the multi-tenant platform (WS-P); when WS-P lands, this audit trail migrates to
the platform audit log / Postgres. The on-disk JSON keeps the trail today with zero canon pollution
(it sits beside the world, not inside the content bundle).
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import RemediationCase

_DIR = ".compliance"
_FILE = "cases.json"


class CaseStore:
    def __init__(self, world_root: str | Path) -> None:
        self.path = Path(world_root) / _DIR / _FILE

    def load(self) -> dict[str, RemediationCase]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return {cid: RemediationCase.model_validate(data) for cid, data in raw.items()}

    def save(self, cases: dict[str, RemediationCase]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {cid: case.model_dump(mode="json") for cid, case in cases.items()}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
