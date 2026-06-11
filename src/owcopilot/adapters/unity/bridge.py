"""The Unity *bridge* — mirror of the Unreal bridge, proving the same split generalises.

* `FakeUnityBridge` — in-memory; offline tests + the two-engine demo.
* `UnityFileBridge` — writes a Unity-importable JSON description per quest into a project's
                      Assets folder (a `JsonUtility`/custom-importer-friendly form). Real, and
                      works without the Unity editor running — Unity picks the file up on focus.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

from ...fakes import FakeUnityBridge as FakeUnityBridge  # re-export (offline test double)


class UnityBridge(Protocol):
    def write_asset(self, name: str, data: dict[str, Any]) -> None: ...
    def read_asset(self, name: str) -> dict[str, Any] | None: ...
    def health(self) -> bool: ...


class UnityFileBridge:
    """Write each quest as `<assets_dir>/<name>.json` (a ScriptableObject description Unity can
    import). No running editor required to write; a tiny C#/`JsonUtility` importer reads it."""

    def __init__(self, assets_dir: str | os.PathLike[str]):
        self.assets_dir = Path(assets_dir)

    def write_asset(self, name: str, data: dict[str, Any]) -> None:
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self._path(name).write_text(json.dumps(data, indent=2), encoding="utf-8")

    def read_asset(self, name: str) -> dict[str, Any] | None:
        path = self._path(name)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def health(self) -> bool:
        return self.assets_dir.parent.exists()

    def _path(self, name: str) -> Path:
        return self.assets_dir / f"{name}.json"
