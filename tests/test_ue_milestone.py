"""P3 milestone, offline ($0): intent -> grounded gen -> catch -> LLM repair -> clean ->
LAND into UE5 (FakeUnrealBridge) -> snapshot read-back -> engine-layer VERIFY.

The real-bridge path (`run_ue_demo(use_real_bridge=True)`) is the same wiring with a
RemoteControlBridge and is a manual machine test (scripts/run_ue_demo.py --ue).
"""

from owcopilot.adapters.unreal import fields_to_quest
from owcopilot.adapters.unreal.bridge import FakeUnrealBridge
from owcopilot.demo import MILESTONE_INTENT, build_ue_app, demo_worldbible, run_ue_demo


def test_ue_milestone_lands_the_repaired_quest_and_reads_it_back():
    result = run_ue_demo()  # offline FakeUnrealBridge
    final, snap = result["final"], result["snapshot"]

    # the loop ended consistent after exactly one repair (Shadowfen -> Northwatch)
    assert [i for i in final.get("issues", []) if i.severity == "error"] == []
    assert final.get("repair_attempts", 0) == 1

    # we landed the REPAIRED quest, and snapshot() round-trips it back from the engine
    assert snap["table"] == "QuestTable"
    assert snap["row_name"] == "Quest_smoke_over_the_marsh"
    assert snap["row"]["Location"] == "Northwatch"  # the repaired location, not Shadowfen
    assert fields_to_quest(snap["row"]) == final["artifact"]

    # engine-layer VERIFY: the landed row re-validates clean against the World Bible
    assert result["landing_issues"] == []


def test_ue_milestone_lands_exactly_once_post_verify():
    wb = demo_worldbible()
    bridge = FakeUnrealBridge()
    app, _tel, adapter = build_ue_app(wb, bridge=bridge)

    final = app.invoke({"intent": MILESTONE_INTENT, "max_repair_attempts": 2, "log": []})
    assert bridge.upserts == []  # nothing landed during the loop (adapter not in execute)

    adapter.apply(final["artifact"])  # land only after VERIFY is clean
    assert len(bridge.upserts) == 1  # exactly one landing, the verified quest
    assert bridge.upserts[0][2]["Location"] == "Northwatch"
