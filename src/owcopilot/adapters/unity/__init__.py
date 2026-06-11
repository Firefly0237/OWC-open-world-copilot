"""Unity adapter — the 'one core, two engines' proof.

Same shape as the Unreal adapter (translation layer + injectable bridge), but it lands a Quest
as a Unity **ScriptableObject-style JSON asset** instead of a UE DataTable row. Running the same
Quest through both adapters with zero changes to the core/orchestrator/generation/validation is
the hardest evidence that the architecture is genuinely engine-agnostic.

`bridge`/`asset_collection` are optional so existing `UnityAdapter()` call sites keep working.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..base import BaseEngineAdapter
from .bridge import FakeUnityBridge, UnityBridge


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_") or "untitled"


def default_asset_name(artifact: dict[str, Any]) -> str:
    return "Quest_" + _slug(artifact.get("title", ""))


def quest_to_scriptableobject(artifact: dict[str, Any]) -> dict[str, Any]:
    """Quest dict -> a Unity-importable ScriptableObject description (fields a `QuestData`
    ScriptableObject / `JsonUtility` would deserialise)."""
    data: dict[str, Any] = {
        "assetType": "QuestData",
        "name": default_asset_name(artifact),
        "title": str(artifact.get("title", "")),
        "giverNpc": str(artifact.get("giver_npc", "")),
        "location": str(artifact.get("location", "")),
        "objective": str(artifact.get("objective", "")),
        "reward": str(artifact.get("reward", "")),
        "prerequisites": [str(p) for p in (artifact.get("prerequisites") or [])],
    }
    if artifact.get("timeline_order") is not None:
        data["timelineOrder"] = int(artifact["timeline_order"])
    return data


def scriptableobject_to_quest(data: dict[str, Any]) -> dict[str, Any]:
    """Inverse mapping so a landed Unity asset can be re-validated against the World Bible."""
    data = data or {}
    return {
        "title": data.get("title", ""),
        "giver_npc": data.get("giverNpc", ""),
        "location": data.get("location", ""),
        "objective": data.get("objective", ""),
        "reward": data.get("reward", ""),
        "prerequisites": list(data.get("prerequisites") or []),
        **(
            {"timeline_order": int(data["timelineOrder"])}
            if data.get("timelineOrder") not in (None, "")
            else {}
        ),
    }


class UnityAdapter(BaseEngineAdapter):
    name = "unity"

    def __init__(
        self,
        bridge: UnityBridge | None = None,
        *,
        asset_collection: str = "Quests",
        asset_name_fn: Callable[[dict[str, Any]], str] | None = None,
        commit: bool = False,
        allowed_collections: set[str] | None = None,
    ):
        allowed = allowed_collections if allowed_collections is not None else {"Quests"}
        if asset_collection not in allowed:
            raise ValueError(f"asset_collection {asset_collection!r} is not in the Unity allowlist")
        self.bridge: UnityBridge = bridge if bridge is not None else FakeUnityBridge()
        self.asset_collection = asset_collection
        self.asset_name_fn = asset_name_fn or default_asset_name
        self.commit = commit
        self._last_asset: str | None = None
        self._last_command: dict[str, Any] | None = None

    def apply(self, artifact: dict[str, Any]) -> None:
        name = self.asset_name_fn(artifact)
        data = quest_to_scriptableobject(artifact)
        self._last_command = {
            "collection": self.asset_collection,
            "asset": name,
            "data": data,
        }
        if self.commit:
            self.bridge.write_asset(name, data)
        self._last_asset = name

    def snapshot(self) -> dict[str, Any]:
        data = self.bridge.read_asset(self._last_asset) if self._last_asset is not None else None
        return {
            "engine": self.name,
            "collection": self.asset_collection,
            "asset": self._last_asset,
            "data": data,
            "committed": self.commit,
            "command": self._last_command,
        }
