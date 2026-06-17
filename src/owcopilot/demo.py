"""Demos — both run fully offline (no API keys, $0).

run_demo()          : P0 — hardcoded mock generator that emits an inconsistency, showing
                      the verify -> REPAIR -> verify loop fix it.
run_grounded_demo() : P1 — in-code demo World Bible + retrieval-grounded,
                      structured generation. Grounding makes the output consistent on the
                      first pass; the validator stays as the safety net (see tests).
"""

from __future__ import annotations

from .assembly import PrefixMode, RouterMode, build_grounded_pipeline, build_validator_suite
from .llm.cache import CacheBackend, NoOpCache
from .llm.gateway import (
    LLMProvider,
    MockProvider,
    OpenAICompatProvider,
    ScriptedFakeProvider,
    StructuredFakeProvider,
)
from .llm.telemetry import TelemetryCollector
from .util import load_dotenv, use_utf8_stdout
from .worldbible.models import Entity, EntityType, Relation, WorldBible


# --------------------------------------------------------------------------- validators
def all_validators(wb: WorldBible):
    """Backward-compatible wrapper for the shared validator suite."""
    return build_validator_suite(wb)


# --------------------------------------------------------------------------- shared
def _print_run(title: str, final: dict, telemetry: TelemetryCollector) -> None:
    bar = "=" * 64
    print(bar)
    print(title)
    print(bar)
    for line in final.get("log", []):
        print("  •", line)
    print("\nFinal artifact:")
    for k, v in (final.get("artifact") or {}).items():
        print(f"  {k:<14}: {v}")
    remaining = final.get("issues", [])
    print(
        f"\nRemaining issues: {len(remaining)}  "
        f"-> {'CONSISTENT' if not remaining else 'STILL INCONSISTENT'}"
    )
    print("\n" + bar)
    print("Cost telemetry (illustrative prices)")
    print(bar)
    print(telemetry.render_table())


# --------------------------------------------------------------------------- P0 (mock)
def seed_worldbible() -> WorldBible:
    wb = WorldBible()
    for e in [
        Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC, description="Caravan master"),
        Entity(id="npc_mira", name="Mira", type=EntityType.NPC, description="Village healer"),
        Entity(id="loc_northwatch", name="Northwatch", type=EntityType.LOCATION),
        Entity(id="loc_riverbend", name="Riverbend", type=EntityType.LOCATION),
    ]:
        wb.add_entity(e)
    return wb


def demo_worldbible() -> WorldBible:
    """Small in-code fixture for offline demos/tests.

    Production/API usage should pass a project World Bible explicitly; this fixture exists only so
    local demos and CI can run without keeping a markdown sample file in the package.
    """
    wb = WorldBible()
    for e in [
        Entity(
            id="aldric",
            name="Aldric",
            type=EntityType.NPC,
            description="Caravan master who runs supply routes through the northern pass",
            tags=["merchant", "quest_giver"],
        ),
        Entity(
            id="mira",
            name="Mira",
            type=EntityType.NPC,
            description="Village healer who tends the wounded in Riverbend",
            tags=["healer"],
        ),
        Entity(
            id="garruk",
            name="Garruk",
            type=EntityType.NPC,
            description="Bandit chieftain hiding in the Shadowfen marshes",
            tags=["antagonist"],
        ),
        Entity(
            id="northwatch",
            name="Northwatch",
            type=EntityType.LOCATION,
            description="Fortified town guarding the northern mountain pass",
        ),
        Entity(
            id="riverbend",
            name="Riverbend",
            type=EntityType.LOCATION,
            description="Riverside village a half-day south of Northwatch",
        ),
        Entity(
            id="shadowfen",
            name="Shadowfen",
            type=EntityType.LOCATION,
            description="Treacherous marshland east of the river, rarely travelled",
        ),
        Entity(
            id="ironhold_watch",
            name="Ironhold Watch",
            type=EntityType.FACTION,
            description="The disciplined town guard of Northwatch",
            tags=["lawful"],
        ),
        Entity(
            id="marsh_reavers",
            name="Marsh Reavers",
            type=EntityType.FACTION,
            description="Bandits operating out of Shadowfen",
            tags=["hostile"],
        ),
        Entity(
            id="the_caravan_ambush",
            name="The Caravan Ambush",
            type=EntityType.EVENT,
            description="Marsh Reavers raid Aldric's supply line on the north road",
            tags=["order=1"],
        ),
        Entity(
            id="the_healers_plea",
            name="The Healer's Plea",
            type=EntityType.EVENT,
            description="Mira calls for aid as the wounded reach Riverbend",
            tags=["order=2"],
        ),
        Entity(
            id="the_siege_of_northwatch",
            name="The Siege of Northwatch",
            type=EntityType.EVENT,
            description="Ironhold Watch makes its stand against the Reavers",
            tags=["order=3"],
        ),
    ]:
        wb.add_entity(e)

    for r in [
        Relation(source="aldric", target="northwatch", kind="located_in"),
        Relation(source="mira", target="riverbend", kind="located_in"),
        Relation(source="garruk", target="shadowfen", kind="located_in"),
        Relation(source="garruk", target="marsh_reavers", kind="leads"),
        Relation(source="aldric", target="ironhold_watch", kind="member_of"),
        Relation(source="mira", target="ironhold_watch", kind="member_of"),
        Relation(source="garruk", target="marsh_reavers", kind="member_of"),
        Relation(source="northwatch", target="ironhold_watch", kind="controlled_by"),
        Relation(source="riverbend", target="ironhold_watch", kind="controlled_by"),
        Relation(source="shadowfen", target="marsh_reavers", kind="controlled_by"),
        Relation(source="marsh_reavers", target="ironhold_watch", kind="enemy_of"),
        Relation(source="northwatch", target="riverbend", kind="road_to"),
    ]:
        wb.add_relation(r)
    return wb


def build_grounded_app(
    wb: WorldBible,
    *,
    frontier: LLMProvider | None = None,
    use_llm_repair: bool = False,
    cheap: LLMProvider | None = None,
    router_mode: RouterMode = "static",
    cache: CacheBackend | None = None,
    prefix_mode: PrefixMode = "retrieval",
):
    """Assemble the P1 grounded pipeline. Defaults reproduce the offline demo exactly; the
    optional knobs let the API layer (`service/api.py`) reuse this same kernel without copying
    pipeline logic:

      - `cheap` / `frontier`: tier providers. Defaults reproduce the offline demo; pass
                              `OpenAICompatProvider(...)` to go live.
      - `use_llm_repair` : `True` swaps the deterministic repair for the LLM-backed one
                            (with the deterministic fallback) — what a real deployment wants.
      - `router_mode`    : `"static"` or `"cascade"` for cheap-first generation.
      - `cache`          : cache backend to hang off the gateway (`NoOpCache` by default).
      - `prefix_mode`    : prompt structure for generation (`"retrieval"` or `"stable"`).
    """
    cheap_provider = cheap if cheap is not None else MockProvider()
    frontier_provider = frontier if frontier is not None else StructuredFakeProvider()
    app, telemetry, _generator = build_grounded_pipeline(
        wb,
        cheap_provider=cheap_provider,
        frontier_provider=frontier_provider,
        use_llm_repair=use_llm_repair,
        router_mode=router_mode,
        cache=cache or NoOpCache(),
        prefix_mode=prefix_mode,
    )
    return app, telemetry


def run_grounded_demo() -> dict:
    use_utf8_stdout()
    wb = demo_worldbible()
    app, telemetry = build_grounded_app(wb)
    print(f"Demo World Bible: {len(wb.entities)} entities, {len(wb.relations)} relations\n")
    final = app.invoke(
        {
            "intent": (
                "Create a quest where Aldric needs help protecting a caravan headed to Northwatch."
            ),
            "max_repair_attempts": 2,
            "log": [],
        }
    )
    _print_run("P1  Retrieval-grounded structured generation", final, telemetry)
    print(
        "\nNote: grounding + structured output -> consistent on first pass (0 repairs). "
        "The verify->repair net still runs; see tests/test_orchestrator.py for the repair path."
    )
    return {"final": final, "telemetry": telemetry.summary()}


# --------------------------------------------------------------------------- P1 milestone
# A deliberately hard intent: it asks to send Aldric (a member of the Ironhold Watch) into
# Shadowfen, which the enemy Marsh Reavers control. Grounded generation follows the intent
# and produces that pairing; FactionConflictValidator catches it; LLM repair relocates the
# quest to friendly Northwatch; re-verify is clean.
MILESTONE_INTENT = (
    "Create a quest that takes place inside Shadowfen, the Marsh Reavers' stronghold. "
    "Aldric must venture deep into Shadowfen alone and hold a hidden winter supply depot "
    "there until the first snow. The quest's location is Shadowfen itself."
)
_MILESTONE_BAD_QUEST = {
    "title": "Smoke Over the Marsh",
    "giver_npc": "Aldric",
    "location": "Shadowfen",
    "objective": "Haul the winter supplies on foot into the heart of Shadowfen",
    "reward": "150 gold",
    "prerequisites": [],
}
_MILESTONE_FIXED_QUEST = {
    "title": "Smoke Over the Marsh",
    "giver_npc": "Aldric",
    "location": "Northwatch",
    "objective": "Stockpile the winter supplies safely behind Northwatch's walls",
    "reward": "150 gold",
    "prerequisites": [],
}


def build_milestone_app(wb: WorldBible, *, use_real_model: bool = False):
    """All four validators + LLM-backed repair (deterministic fallback).

    Offline: a ScriptedFakeProvider returns the inconsistent quest for generation and the
    corrected one for repair, so the full loop runs at $0. Live: pass use_real_model=True to
    route the frontier tier to DeepSeek (needs OPENAI_BASE_URL / OPENAI_API_KEY + `openai`).
    """
    if use_real_model:
        load_dotenv()
        frontier: LLMProvider = OpenAICompatProvider(model="deepseek-v4-pro")
    else:
        frontier = ScriptedFakeProvider(
            generate=_MILESTONE_BAD_QUEST, repair=_MILESTONE_FIXED_QUEST
        )
    app, telemetry, _generator = build_grounded_pipeline(
        wb,
        cheap_provider=MockProvider(),
        frontier_provider=frontier,
        use_llm_repair=True,
        router_mode="static",
        prefix_mode="retrieval",
    )
    return app, telemetry


def run_milestone_demo(*, use_real_model: bool = False) -> dict:
    use_utf8_stdout()
    wb = demo_worldbible()
    app, telemetry = build_milestone_app(wb, use_real_model=use_real_model)
    mode = "DeepSeek (real model)" if use_real_model else "offline fake ($0)"
    n_ent, n_rel = len(wb.entities), len(wb.relations)
    print(f"World Bible: {n_ent} entities, {n_rel} relations  |  provider: {mode}")
    print(f"Intent: {MILESTONE_INTENT}\n")
    final = app.invoke({"intent": MILESTONE_INTENT, "max_repair_attempts": 2, "log": []})
    _print_run(
        "P1 milestone  intent -> grounded gen -> caught -> LLM repair -> clean", final, telemetry
    )
    n_repairs = final.get("repair_attempts", 0)
    print("\nValidators run each VERIFY: reference + prereq-cycle + faction-conflict + timeline.")
    detail = (
        "LLM-backed localised fix, deterministic fallback"
        if n_repairs
        else "first pass already clean"
    )
    print(f"Repairs applied: {n_repairs} ({detail}).")
    return {"final": final, "telemetry": telemetry.summary()}



if __name__ == "__main__":
    run_grounded_demo()
