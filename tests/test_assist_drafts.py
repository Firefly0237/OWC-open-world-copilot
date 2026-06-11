from __future__ import annotations

import json

from owcopilot.assist.drafts import QuestDraftService, parse_quest_draft
from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.models import Severity
from owcopilot.audit.runner import AuditRunner
from owcopilot.content.models import ContentBundle, Entity, EntityType, Origin, ReviewStatus
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.llm.telemetry import TelemetryCollector
from owcopilot.retrieval.bm25 import BM25Retriever
from owcopilot.retrieval.context_pack import ContextPackBuilder
from owcopilot.storage import SQLiteStore


class DraftProvider:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        return json.dumps(self.payload), 20, 10


def test_parse_quest_draft_fills_missing_id() -> None:
    quest = parse_quest_draft(
        """```json
{"title": "Missing Caravan", "objective": "Find the caravan"}
```"""
    )

    assert quest.id == "quest_missing_caravan"
    assert quest.title == "Missing Caravan"


def test_parse_quest_draft_tolerates_real_model_shape_drift() -> None:
    """Round-2 real-LLM run: deepseek emitted `"rewards": {}` and scalar strings where the
    schema wants lists. Shape drift is normalized; semantics still go through the audit."""
    quest = parse_quest_draft(
        json.dumps(
            {
                "title": "护送盐车",
                "objective": "把盐车送到烽燧。",
                "rewards": {},
                "prerequisites": "quest_intro",
                "localization_keys": "quest.x.objective",
                "tags": "side",
                "stages": {},
                "metadata": "not-a-dict",
            }
        )
    )
    assert quest.rewards == []
    assert quest.prerequisites == ["quest_intro"]
    assert quest.localization_keys == ["quest.x.objective"]
    assert quest.tags == ["side"]
    assert quest.stages == []
    assert quest.metadata == {}


def test_parse_quest_draft_converts_reward_mapping() -> None:
    quest = parse_quest_draft(
        json.dumps({"title": "T", "objective": "O", "rewards": {"gold": 75}})
    )
    assert quest.rewards[0].kind == "gold"
    assert quest.rewards[0].value == "75"


def test_parse_quest_draft_tolerates_null_lists_and_stage_aliases() -> None:
    """Second real run drift: prerequisites=null and stages using `description` for `summary`."""
    quest = parse_quest_draft(
        json.dumps(
            {
                "title": "护送盐车",
                "objective": "把盐车送到烽燧。",
                "prerequisites": None,
                "rewards": None,
                "stages": [
                    {"id": "stage_1", "description": "在渡口集合", "target": "loc_r1_a"},
                    {"text": "抵达烽燧"},
                ],
            }
        )
    )
    assert quest.prerequisites == []
    assert quest.rewards == []
    assert quest.stages[0].summary == "在渡口集合"
    assert quest.stages[1].id == "stage_2"
    assert quest.stages[1].summary == "抵达烽燧"


def test_quest_draft_service_marks_ai_draft_and_runs_audit() -> None:
    store = SQLiteStore()
    try:
        bundle = ContentBundle(
            entities={
                "npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC),
                "location_northwatch": Entity(
                    id="location_northwatch",
                    name="Northwatch",
                    type=EntityType.LOCATION,
                ),
            }
        )
        store.replace_content_index(bundle)
        telemetry = TelemetryCollector()
        gateway = LLMGateway(
            providers={
                "cheap": DraftProvider(
                    {
                        "title": "Missing Caravan",
                        "giver_npc": "npc_aldric",
                        "location": "location_northwatch",
                        "objective": "Find the caravan",
                        "localization_keys": ["quest.missing_caravan.objective"],
                    }
                )
            },
            router=StaticRouter(mapping={"quest_draft": "cheap"}),
            cache=NoOpCache(),
            telemetry=telemetry,
        )
        service = QuestDraftService(
            gateway=gateway,
            context_builder=ContextPackBuilder(bm25=BM25Retriever(store)),
            audit_runner=AuditRunner(build_default_rule_registry()),
            bundle=bundle,
        )

        result = service.draft_quest("Aldric")

        assert result.quest.origin is Origin.AI_DRAFT
        assert result.quest.review_status is ReviewStatus.PENDING_REVIEW
        assert result.quest.metadata["context_refs"]
        assert {issue.rule_code for issue in result.issues} == {"UNREVIEWED_AI_CONTENT"}
        assert all(issue.severity is not Severity.ERROR for issue in result.issues)
        assert telemetry.records[0].task == "quest_draft"
    finally:
        store.close()


def test_quest_draft_service_returns_audit_issues_with_draft() -> None:
    store = SQLiteStore()
    try:
        bundle = ContentBundle(
            entities={"npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC)}
        )
        store.replace_content_index(bundle)
        gateway = LLMGateway(
            providers={
                "cheap": DraftProvider(
                    {
                        "id": "quest_bad",
                        "title": "Bad",
                        "giver_npc": "npc_missing",
                    }
                )
            },
            router=StaticRouter(mapping={"quest_draft": "cheap"}),
            cache=NoOpCache(),
        )
        service = QuestDraftService(
            gateway=gateway,
            context_builder=ContextPackBuilder(bm25=BM25Retriever(store)),
            audit_runner=AuditRunner(build_default_rule_registry()),
            bundle=bundle,
        )

        result = service.draft_quest("bad Aldric quest")

        assert {issue.rule_code for issue in result.issues} >= {
            "UNKNOWN_ENTITY_REF",
            "MISSING_LOCALIZATION_KEY",
            "QUEST_MISSING_OBJECTIVE",
        }
    finally:
        store.close()
