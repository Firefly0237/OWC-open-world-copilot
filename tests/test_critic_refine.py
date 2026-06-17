"""Generate→critique→refine loop: the critic + bounded refinement that raises autonomous draft
quality before human review. Offline / $0 — the offline provider drives both halves of the loop.
"""

from __future__ import annotations

import json

from owcopilot.assist.critic import QuestCritic, parse_critique
from owcopilot.assist.drafts import QuestDraftService
from owcopilot.assist.offline import OfflineQuestDraftProvider
from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.runner import AuditRunner
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.retrieval.bm25 import BM25Retriever
from owcopilot.retrieval.context_pack import ContextPackBuilder
from owcopilot.storage import SQLiteStore


def _service(bundle: ContentBundle, store: SQLiteStore, *, rounds: int) -> QuestDraftService:
    store.replace_content_index(bundle)
    gateway = LLMGateway(
        providers={"cheap": OfflineQuestDraftProvider()},
        router=StaticRouter(mapping={"quest_draft": "cheap"}),  # quest_critique falls back to cheap
        cache=NoOpCache(),
    )
    return QuestDraftService(
        gateway=gateway,
        context_builder=ContextPackBuilder(bm25=BM25Retriever(store)),
        audit_runner=AuditRunner(build_default_rule_registry()),
        bundle=bundle,
        critic=QuestCritic(gateway=gateway) if rounds else None,
        max_refine_rounds=rounds,
    )


def test_parse_critique_promotes_blocker_to_revise() -> None:
    # A self-contradictory "pass" with a blocker must be trusted as "revise".
    res = parse_critique(
        json.dumps(
            {
                "verdict": "pass",
                "score": 0.5,
                "dimensions": [{"dimension": "completeness", "severity": "blocker", "fix": "x"}],
            }
        )
    )
    assert res.verdict == "revise"
    assert res.actionable_fixes() == ["[completeness] x"]


def test_parse_critique_unparsable_is_revise_not_a_fake_pass() -> None:
    # An unparsable critique means the quality gate FAILED to run — it must never be reported as a
    # pass (that would silently disable the gate). It comes back as revise + parse_ok=False so the
    # caller can flag the draft for human scrutiny.
    res = parse_critique("not json at all")
    assert res.verdict == "revise"
    assert res.parse_ok is False
    assert res.score == 0.0


def test_parse_critique_recovers_json_wrapped_in_prose() -> None:
    # The usual reason a real critique "won't parse" is prose around the object; extract it.
    res = parse_critique(
        'Sure! Here is my review:\n```json\n{"verdict": "pass", "score": 0.8}\n```'
    )
    assert res.parse_ok is True
    assert res.verdict == "pass"


def test_refine_loop_converges_to_complete_grounded_quest() -> None:
    store = SQLiteStore()
    try:
        bundle = ContentBundle(
            entities={
                "npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC),
                "location_northwatch": Entity(
                    id="location_northwatch", name="Northwatch", type=EntityType.LOCATION
                ),
            }
        )
        result = _service(bundle, store, rounds=2).draft_quest("Aldric Northwatch caravan")

        # The minimal first draft is critiqued, refined, and converges: trail records a revise then
        # a pass, and the final quest gained stages + rewards + grounded giver/location.
        assert [r.verdict for r in result.refine_trail] == ["revise", "pass"]
        assert result.refine_trail[0].readiness_score < result.refine_trail[1].readiness_score
        assert result.refine_trail[1].readiness_score == 1.0
        assert len(result.quest.stages) >= 1
        assert len(result.quest.rewards) >= 1
        assert result.quest.giver_npc == "npc_aldric"
        assert result.quest.location == "location_northwatch"
        assert result.quest.metadata["refine_rounds"] == 2
        # objective correctness gate held: refined quest introduced no new audit errors
        assert result.refine_trail[1].new_error_count == 0
    finally:
        store.close()


class _JunkCriticProvider:
    """Returns a valid quest for generation but unparsable text for every critique — simulating a
    critic model that keeps failing, to prove the loop never fakes a pass."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        if "STRICT_QUEST_REVIEWER" in system:
            return "the quest looks fine to me, no JSON here", 10, 10
        return json.dumps({"id": "q_x", "title": "X", "objective": "do the thing properly"}), 10, 10


def test_unparsable_critic_flags_for_human_not_silent_pass() -> None:
    store = SQLiteStore()
    try:
        bundle = ContentBundle(
            entities={"npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC)}
        )
        store.replace_content_index(bundle)
        gateway = LLMGateway(
            providers={"cheap": _JunkCriticProvider()},
            router=StaticRouter(mapping={"quest_draft": "cheap"}),
            cache=NoOpCache(),
        )
        result = QuestDraftService(
            gateway=gateway,
            context_builder=ContextPackBuilder(bm25=BM25Retriever(store)),
            audit_runner=AuditRunner(build_default_rule_registry()),
            bundle=bundle,
            critic=QuestCritic(gateway=gateway),
            max_refine_rounds=2,
        ).draft_quest("Aldric")
        # the gate failed to run, so the draft is flagged for human scrutiny, NOT passed
        assert result.auto_review_incomplete is True
        assert result.quest.metadata.get("auto_review_incomplete") is True
        assert all(r.verdict != "pass" for r in result.refine_trail)
        assert any(r.auto_review_ok is False for r in result.refine_trail)
    finally:
        store.close()


def test_without_critic_is_unchanged_single_shot() -> None:
    store = SQLiteStore()
    try:
        bundle = ContentBundle(
            entities={"npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC)}
        )
        result = _service(bundle, store, rounds=0).draft_quest("Aldric")
        assert result.refine_trail == []
        assert result.quest.stages == []  # the minimal single-shot draft, no refinement
        assert "refine_rounds" not in result.quest.metadata
    finally:
        store.close()


def test_run_draft_action_surfaces_refine_trail(tmp_path) -> None:
    from owcopilot.app.actions import run_draft_action
    from owcopilot.content.store import ContentStore

    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            entities={
                "npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC),
                "location_northwatch": Entity(
                    id="location_northwatch", name="Northwatch", type=EntityType.LOCATION
                ),
            }
        )
    )
    out = run_draft_action(root, brief="Aldric Northwatch caravan", llm_mode="offline")
    assert out["refine_trail"], "action must surface how the draft was refined"
    assert out["quest"]["stages"]
