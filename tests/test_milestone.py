"""P1 milestone, offline ($0): intent -> grounded gen -> validator catch -> LLM repair -> clean.

Mirrors `scripts/run_milestone_demo.py` without a real model. The real-model path
(`use_real_model=True`) is the same wiring with OpenAICompatProvider and is covered manually.
"""

from owcopilot.demo import build_milestone_app, demo_worldbible


def test_milestone_catches_faction_conflict_and_repairs():
    wb = demo_worldbible()
    app, tel = build_milestone_app(wb)
    final = app.invoke(
        {
            "intent": "Send Aldric deep into Shadowfen with the winter supplies.",
            "max_repair_attempts": 2,
            "log": [],
        }
    )
    art = final["artifact"]

    # generation pairs Aldric with enemy-held Shadowfen; repair relocates him to Northwatch
    assert art["giver_npc"] == "Aldric"
    assert art["location"] == "Northwatch"
    # ends consistent after exactly one repair
    assert [i for i in final.get("issues", []) if i.severity == "error"] == []
    assert final.get("repair_attempts", 0) == 1
    # the repair really went through the gateway as a frontier (LLM) call
    assert any(r.task == "repair" and r.tier == "frontier" for r in tel.records)
