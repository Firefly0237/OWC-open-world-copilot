"""CascadeRouter + cascade generation (P2 T5): cheap by default, escalate only when the
deterministic consistency validators reject the cheap output. All offline/$0.
"""

from owcopilot.consistency.validators import FactionConflictValidator, ReferenceValidator
from owcopilot.generation.quest import CascadingQuestGenerator, GroundedQuestGenerator
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway, StructuredFakeProvider
from owcopilot.llm.router import CascadeRouter
from owcopilot.llm.telemetry import TelemetryCollector
from owcopilot.worldbible.models import Entity, EntityType, Relation, WorldBible


def _wb() -> WorldBible:
    wb = WorldBible()
    wb.add_entity(Entity(id="aldric", name="Aldric", type=EntityType.NPC))
    wb.add_entity(Entity(id="northwatch", name="Northwatch", type=EntityType.LOCATION))
    wb.add_entity(Entity(id="shadowfen", name="Shadowfen", type=EntityType.LOCATION))
    wb.add_entity(Entity(id="watch", name="Ironhold Watch", type=EntityType.FACTION))
    wb.add_entity(Entity(id="reavers", name="Marsh Reavers", type=EntityType.FACTION))
    wb.add_relation(Relation(source="aldric", target="watch", kind="member_of"))
    wb.add_relation(Relation(source="northwatch", target="watch", kind="controlled_by"))
    wb.add_relation(Relation(source="shadowfen", target="reavers", kind="controlled_by"))
    wb.add_relation(Relation(source="reavers", target="watch", kind="enemy_of"))
    return wb


GOOD = {
    "title": "T",
    "giver_npc": "Aldric",
    "location": "Northwatch",
    "objective": "o",
    "reward": "",
    "prerequisites": [],
}
BAD = {
    "title": "T",
    "giver_npc": "Aldric",
    "location": "Shadowfen",  # enemy-held -> conflict
    "objective": "o",
    "reward": "",
    "prerequisites": [],
}


def test_cascade_router_defaults_generate_cheap_but_hint_wins():
    r = CascadeRouter()
    assert r.choose(task="generate") == "cheap"  # start cheap
    assert r.choose(task="generate", hint="frontier") == "frontier"  # escalation hint wins
    assert r.choose(task="plan") == "cheap"  # non-cascade via base
    assert r.choose(task="repair") == "frontier"  # base StaticRouter default


def _cascade(cheap_quest, strong_quest, wb):
    tel = TelemetryCollector()
    gw = LLMGateway(
        providers={
            "cheap": StructuredFakeProvider(quest=cheap_quest),
            "frontier": StructuredFakeProvider(quest=strong_quest),
        },
        router=CascadeRouter(),
        cache=NoOpCache(),
        telemetry=tel,
    )
    gen = CascadingQuestGenerator(
        GroundedQuestGenerator(gw, wb),
        [ReferenceValidator(wb), FactionConflictValidator(wb)],
    )
    return tel, gen


def test_cascade_escalates_when_cheap_breaks_lore():
    wb = _wb()
    tel, gen = _cascade(BAD, GOOD, wb)  # cheap output is faction-inconsistent
    art = gen.generate("a quest for Aldric")

    assert art["location"] == "Northwatch"  # ended on the strong tier's clean output
    assert gen.escalations == 1
    gen_tiers = [r.tier for r in tel.records if r.task == "generate"]
    assert gen_tiers == ["cheap", "frontier"]  # tried cheap, then escalated once


def test_cascade_stays_cheap_when_clean():
    wb = _wb()
    tel, gen = _cascade(GOOD, GOOD, wb)  # cheap output is already consistent
    art = gen.generate("a quest for Aldric")

    assert art["location"] == "Northwatch"
    assert gen.escalations == 0
    gen_tiers = [r.tier for r in tel.records if r.task == "generate"]
    assert gen_tiers == ["cheap"]  # never escalated -> stayed on the cheap tier
