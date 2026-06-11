"""P3 Unity adapter (offline): the 'one core, two engines' proof.

Same translation-layer + injectable-bridge shape as Unreal, landing a Quest as a Unity
ScriptableObject-style JSON asset.
"""

import pytest

from owcopilot.adapters.unity import (
    UnityAdapter,
    default_asset_name,
    quest_to_scriptableobject,
    scriptableobject_to_quest,
)
from owcopilot.adapters.unity.bridge import FakeUnityBridge, UnityFileBridge

QUEST = {
    "title": "Smoke Over the Marsh",
    "giver_npc": "Aldric",
    "location": "Northwatch",
    "objective": "Hold the depot",
    "reward": "150 gold",
    "prerequisites": ["The Caravan Ambush"],
}


def test_quest_to_scriptableobject_and_inverse():
    so = quest_to_scriptableobject(QUEST)
    assert so["assetType"] == "QuestData"
    assert so["title"] == "Smoke Over the Marsh" and so["giverNpc"] == "Aldric"
    assert so["prerequisites"] == ["The Caravan Ambush"]
    assert scriptableobject_to_quest(so) == QUEST
    assert default_asset_name(QUEST) == "Quest_smoke_over_the_marsh"


def test_unity_apply_and_snapshot():
    bridge = FakeUnityBridge()
    adapter = UnityAdapter(bridge, commit=True)
    adapter.apply(QUEST)

    assert len(bridge.writes) == 1
    snap = adapter.snapshot()
    assert snap["engine"] == "unity"
    assert snap["asset"] == "Quest_smoke_over_the_marsh"
    assert snap["data"]["location"] == "Northwatch"


def test_unity_adapter_defaults_to_dry_run_command_only():
    bridge = FakeUnityBridge()
    adapter = UnityAdapter(bridge)
    adapter.apply(QUEST)

    assert bridge.writes == []
    snap = adapter.snapshot()
    assert snap["data"] is None
    assert snap["committed"] is False
    assert snap["command"]["asset"] == "Quest_smoke_over_the_marsh"


def test_unity_collection_allowlist_blocks_unapproved_targets():
    with pytest.raises(ValueError, match="allowlist"):
        UnityAdapter(asset_collection="Arbitrary")


def test_unity_file_bridge_writes_real_json_asset(tmp_path):
    bridge = UnityFileBridge(tmp_path / "Assets" / "Quests")
    adapter = UnityAdapter(bridge, commit=True)
    adapter.apply(QUEST)

    asset = tmp_path / "Assets" / "Quests" / "Quest_smoke_over_the_marsh.json"
    assert asset.exists()  # a real file Unity can import
    assert adapter.snapshot()["data"]["title"] == "Smoke Over the Marsh"


def test_two_engine_demo_lands_same_quest_to_both_engines():
    from owcopilot.demo import run_two_engine_demo

    r = run_two_engine_demo()
    # the one consistent quest reaches both engines, field-aligned
    assert r["unreal"]["row"]["Location"] == r["quest"]["location"]
    assert r["unity"]["data"]["location"] == r["quest"]["location"]
    assert r["unreal"]["row"]["Title"] == r["unity"]["data"]["title"]
