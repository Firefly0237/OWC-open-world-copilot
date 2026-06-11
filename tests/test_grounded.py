from owcopilot.demo import build_grounded_app, demo_worldbible


def test_grounded_generation_is_consistent():
    wb = demo_worldbible()
    app, tel = build_grounded_app(wb)
    final = app.invoke(
        {
            "intent": "A caravan quest for Aldric headed to Northwatch.",
            "max_repair_attempts": 2,
            "log": [],
        }
    )
    art = final["artifact"]
    # grounded output references only real entities
    assert art["giver_npc"] in wb.names()
    assert art["location"] in wb.names()
    # consistent on first pass -> no error issues, no repair needed
    assert [i for i in final.get("issues", []) if i.severity == "error"] == []
    assert final.get("repair_attempts", 0) == 0
    # plan + generate happened (no repair call)
    assert len(tel.records) >= 2


def test_grounded_generation_retries_on_unparseable_response():
    # Real models occasionally return empty/non-JSON; the generator retries once then succeeds.
    import json as _json

    from owcopilot.generation.quest import GroundedQuestGenerator
    from owcopilot.llm.gateway import LLMGateway

    wb = demo_worldbible()
    good = {
        "title": "T",
        "giver_npc": "Aldric",
        "location": "Northwatch",
        "objective": "o",
        "reward": "",
        "prerequisites": [],
    }

    class _BadThenGood:
        def __init__(self):
            self.calls = 0

        def complete(self, *, system, user, model):
            self.calls += 1
            return ("not json at all" if self.calls == 1 else _json.dumps(good)), 5, 5

    prov = _BadThenGood()
    gen = GroundedQuestGenerator(
        LLMGateway(providers={"frontier": prov}), wb
    )  # generate -> frontier
    art = gen.generate("a quest for Aldric")
    assert art["location"] == "Northwatch"
    assert prov.calls == 2  # retried exactly once after the bad response


def test_retrieval_is_intent_scoped():
    # mentioning Aldric should pull Aldric (and his 1-hop neighbours) into the context
    wb = demo_worldbible()
    from owcopilot.generation.quest import GroundedQuestGenerator
    from owcopilot.llm.gateway import LLMGateway, StructuredFakeProvider

    gen = GroundedQuestGenerator(LLMGateway(providers={"frontier": StructuredFakeProvider()}), wb)
    ctx = gen._retrieve("a quest for Aldric")
    assert "Aldric" in ctx
    assert "Northwatch" in ctx  # 1-hop neighbour via 'located_in'


def test_retrieval_uses_lexical_entity_scores_without_exact_name():
    wb = demo_worldbible()
    from owcopilot.generation.quest import GroundedQuestGenerator
    from owcopilot.llm.gateway import LLMGateway, StructuredFakeProvider

    gen = GroundedQuestGenerator(LLMGateway(providers={"frontier": StructuredFakeProvider()}), wb)
    ctx = gen._retrieve("a quest where a healer tends wounded villagers")
    assert "Mira" in ctx
    assert "Riverbend" in ctx  # Mira's 1-hop location should come along with the lexical hit
