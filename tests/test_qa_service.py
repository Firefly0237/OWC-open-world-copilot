from __future__ import annotations

import json

from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.llm.telemetry import TelemetryCollector
from owcopilot.qa.service import LoreQAService
from owcopilot.retrieval.bm25 import BM25Retriever
from owcopilot.retrieval.context_pack import ContextPackBuilder
from owcopilot.storage import SQLiteStore


class QAProvider:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        return json.dumps(self.payload), 10, 5


def _service(
    payload: dict, bundle: ContentBundle, store: SQLiteStore
) -> tuple[LoreQAService, TelemetryCollector]:
    telemetry = TelemetryCollector()
    gateway = LLMGateway(
        providers={"cheap": QAProvider(payload)},
        router=StaticRouter(mapping={"qa_answer": "cheap"}),
        cache=NoOpCache(),
        telemetry=telemetry,
    )
    service = LoreQAService(
        gateway=gateway,
        context_builder=ContextPackBuilder(bm25=BM25Retriever(store)),
        bundle=bundle,
    )
    return service, telemetry


def test_lore_qa_service_returns_verified_answer_through_gateway() -> None:
    store = SQLiteStore()
    try:
        bundle = ContentBundle(
            entities={
                "npc_aldric": Entity(
                    id="npc_aldric",
                    name="Aldric",
                    type=EntityType.NPC,
                    description="Caravan master",
                )
            }
        )
        store.replace_content_index(bundle)
        service, telemetry = _service(
            {
                "answer": "Aldric is a caravan master.",
                "citations": [{"ref": "entity:npc_aldric"}],
                "confidence": 0.9,
                "mentioned_entities": ["Aldric"],
                "unresolved_mentions": [],
            },
            bundle,
            store,
        )

        answer = service.ask("Aldric")

        assert answer.refused is False
        assert answer.citations[0].ref == "entity:npc_aldric"
        assert telemetry.records[0].task == "qa_answer"
    finally:
        store.close()


def test_lore_qa_service_refuses_when_context_pack_is_empty() -> None:
    store = SQLiteStore()
    try:
        service, telemetry = _service({"answer": "x"}, ContentBundle(), store)

        answer = service.ask("unknown")

        assert answer.refused is True
        assert telemetry.records == []
    finally:
        store.close()


def test_lore_qa_service_refuses_invalid_model_citation() -> None:
    store = SQLiteStore()
    try:
        bundle = ContentBundle(
            entities={"npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC)}
        )
        store.replace_content_index(bundle)
        service, _telemetry = _service(
            {
                "answer": "Aldric is a caravan master.",
                "citations": [{"ref": "entity:missing"}],
                "confidence": 0.9,
                "mentioned_entities": ["Aldric"],
                "unresolved_mentions": [],
            },
            bundle,
            store,
        )

        answer = service.ask("Aldric")

        assert answer.refused is True
    finally:
        store.close()
