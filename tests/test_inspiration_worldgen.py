from __future__ import annotations

from owcopilot.app.actions import (
    add_reference_action,
    decide_review_action,
    list_references_action,
    list_review_items_action,
    run_ask_action,
    run_world_seed_action,
    search_references_action,
)
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.store import ContentStore


def test_reference_library_is_separate_from_project_lore_rag(tmp_path) -> None:
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            entities={
                "npc_mara": Entity(
                    id="npc_mara",
                    name="Mara",
                    type=EntityType.NPC,
                    description="Border scout loyal to the guard.",
                )
            }
        )
    )

    added = add_reference_action(
        root,
        title="Mara variant note",
        text="Mara is a rival queen in the inspiration sample, not project lore.",
        allowed_uses=["inspiration", "structure"],
    )
    assert added["indexed_count"] >= 1
    assert list_references_action(root)["count"] == 1

    reference_hits = search_references_action(root, query="rival queen Mara")
    assert reference_hits["hits"]
    assert all(hit["ref"].startswith("reference_chunk:") for hit in reference_hits["hits"])

    answer = run_ask_action(root, query="Who is Mara?")
    assert not answer["answer"]["refused"]
    assert answer["answer"]["citations"]
    assert all(
        not citation["ref"].startswith("reference_chunk:")
        for citation in answer["answer"]["citations"]
    )
    assert "rival queen" not in answer["answer"]["answer"]


def test_world_seed_enters_review_queue_then_accept_writes_bundle(tmp_path) -> None:
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    add_reference_action(
        root,
        title="Three-way frontier conflict",
        text=(
            "A useful structure: three factions compete for a failing resource. "
            "One controls infrastructure, one protects old rites, one sells information."
        ),
        allowed_uses=["inspiration", "structure"],
    )

    result = run_world_seed_action(
        root,
        brief={
            "idea": "A steam-powered border world where forests remember old wars.",
            "world_styles": ["蒸汽朋克", "魔幻"],
            "reference_query": "three factions failing resource",
            "reference_mode": "参考剧情结构",
            "faction_count": 3,
            "region_count": 2,
            "npc_count": 4,
            "quest_count": 3,
            "term_count": 3,
        },
    )

    assert result["counts"]["quests"] == 3
    assert result["counts"]["regions"] == 2
    assert result["inspiration_context_refs"]
    assert result["reference_report"]
    assert result["bundle"]["quests"]
    assert all(
        quest["review_status"] == "pending_review" for quest in result["bundle"]["quests"].values()
    )
    queue = list_review_items_action(root)
    assert queue["count"] == 1
    assert queue["items"][0]["item_type"] == "world_seed"

    accepted = decide_review_action(
        root,
        item_id=result["review_item_id"],
        decision="accepted",
        operator="lead",
    )
    assert accepted["written_ref"].startswith("world_seed:")
    saved = ContentStore(root).load()
    assert len(saved.quests) == 3
    assert len(saved.regions) == 2
    assert all(quest.review_status == "approved" for quest in saved.quests.values())
    assert all(entity.origin == "ai_draft" for entity in saved.entities.values())


def test_use_references_flag_toggles_inspiration_grounding(tmp_path) -> None:
    """The creator controls whether genesis draws on the inspiration library, but is never forced
    to: it is on by default, and turning it off keeps a clean-room world with no reference refs."""
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    add_reference_action(
        root,
        title="Frontier structure",
        text="Three factions fight over a failing resource on a remembered frontier.",
        allowed_uses=["inspiration"],
    )
    brief = {
        "idea": "a frontier where forests remember old wars",
        "reference_query": "three factions failing resource frontier",
        "faction_count": 2,
        "quest_count": 2,
    }
    grounded = run_world_seed_action(root, brief=brief)
    assert grounded["inspiration_context_refs"]

    clean_room = run_world_seed_action(root, brief={**brief, "use_references": False})
    assert clean_room["inspiration_context_refs"] == []


def test_use_project_facts_grounds_new_world_in_imported_canon(tmp_path) -> None:
    """After a manuscript is extracted and approved into the world, genesis can ground a new world
    in that canon — the seam that makes 内容带入 feed straight into 创世."""
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            entities={
                "fac_tianji": Entity(
                    id="fac_tianji",
                    name="天机阁",
                    type=EntityType.FACTION,
                    description="掌握雾隐城情报网络的隐秘势力。",
                )
            }
        )
    )
    result = run_world_seed_action(
        root,
        brief={
            "idea": "围绕天机阁情报网展开的雾港世界",
            "faction_count": 2,
            "quest_count": 2,
            "use_project_facts": True,
        },
    )
    assert result["project_context_refs"]


def test_staged_chain_emits_real_stages_and_grounds_downstream_on_upstream(tmp_path) -> None:
    """The world is built as a grounded chain (premise→factions→regions→cast→quests), not one big
    call: progress events are the real stages, and the capstone quests reference the cast and
    places earlier stages produced (cross-stage ids wired up by the deterministic normaliser)."""
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    seen: list[str] = []
    result = run_world_seed_action(
        root,
        brief={
            "idea": "一个靠蒸汽巨树供能的边境群岛",
            "faction_count": 3,
            "region_count": 2,
            "npc_count": 4,
            "quest_count": 3,
            "term_count": 3,
        },
        refine_rounds=0,  # this test exercises the plain staged chain, no refine loop
        progress=lambda _type, data: seen.append(str(data.get("name"))),
    )

    # Decorative "generating/parsing" is gone — every real stage announces itself.
    for stage in ("retrieving", "premise", "factions", "regions", "cast", "quests", "assembling"):
        assert stage in seen, f"missing real stage event: {stage}"

    bundle = result["bundle"]
    npc_ids = {eid for eid, e in bundle["entities"].items() if e["type"] == "npc"}
    loc_ids = {eid for eid, e in bundle["entities"].items() if e["type"] == "location"}
    faction_ids = {eid for eid, e in bundle["entities"].items() if e["type"] == "faction"}
    assert npc_ids and loc_ids and faction_ids

    # Quests are grounded: every giver/location resolves to a cast member / place that exists.
    for quest in bundle["quests"].values():
        assert quest["giver_npc"] in npc_ids
        assert quest["location"] in loc_ids
    # Cross-stage relations the chain wires up deterministically.
    kinds = {rel["kind"] for rel in bundle["relations"]}
    assert {"member_of", "located_in", "controlled_by"} <= kinds
    # No refine loop unless asked.
    assert result["refine_trail"] == []


def test_premise_spine_persists_in_style_guide(tmp_path) -> None:
    """The premise stage now produces a dramatic spine (central conflict / themes / question /
    stakes), not just a summary. It is folded into the persisted style guide so the planner sees
    the conflict the whole world was built to serve, and fed as grounding to every later stage."""
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    result = run_world_seed_action(
        root,
        brief={
            "idea": "一个靠蒸汽巨树供能的边境群岛",
            "faction_count": 3,
            "region_count": 1,
            "npc_count": 3,
            "quest_count": 2,
            "term_count": 0,
        },
    )
    bodies = " ".join(sg["body"] for sg in result["bundle"]["style_guides"].values())
    assert "戏剧主轴" in bodies and "核心冲突" in bodies  # spine persisted, visible to the planner


def test_world_quest_refine_loop_converges_and_deepens(tmp_path) -> None:
    """Opt-in quests-stage critique→refine loop: a thin first batch is critiqued (revise), refined,
    and re-checked (pass); the deterministic grounding gaps close and every quest gains real
    stages. Mirrors the round-22 single-quest loop, $0 offline."""
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    result = run_world_seed_action(
        root,
        brief={
            "idea": "一个靠蒸汽巨树供能的边境群岛",
            "faction_count": 2,
            "region_count": 1,
            "npc_count": 4,
            "quest_count": 3,
            "term_count": 0,
        },
        refine_rounds=2,
    )
    trail = result["refine_trail"]
    assert [r["verdict"] for r in trail] == ["revise", "pass"]
    # objective gate closed: grounding gaps present on the thin batch, gone after refinement
    assert trail[0]["gap_count"] > 0
    assert trail[1]["gap_count"] == 0
    # refinement deepened the capstone: each quest now carries a real multi-stage arc
    for quest in result["bundle"]["quests"].values():
        assert len(quest["stages"]) >= 2
