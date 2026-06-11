"""Recent-workspace bookkeeping for the Workbench (local JSON in the user's home)."""

from __future__ import annotations

import json
from pathlib import Path


def _default_path() -> Path:
    return Path.home() / ".owcopilot" / "recent_workspaces.json"


def load_recent_workspaces(path: str | Path | None = None) -> list[str]:
    target = Path(path) if path is not None else _default_path()
    if not target.exists():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if str(item).strip()]


def remember_workspace(
    root: str | Path,
    *,
    path: str | Path | None = None,
    limit: int = 8,
) -> list[str]:
    """Push `root` to the front of the recent list (deduped, capped) and persist it."""
    target = Path(path) if path is not None else _default_path()
    entry = str(Path(root).resolve())
    recent = [item for item in load_recent_workspaces(target) if item != entry]
    recent.insert(0, entry)
    recent = recent[:limit]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(recent, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return recent
