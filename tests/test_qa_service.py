from __future__ import annotations

import json

from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.llm.telemetry import TelemetryCollector
from owcopilot.qa.service import (
    QA_EXPAND_MARKER,
    REFUSAL_NO_CONTEXT,
    REFUSAL_UNGROUNDED,
    LoreQAService,
)
from owcopilot.retrieval.bm25 import BM25Retriever
from owcopilot.retrieval.context_pack import ContextPackBuilder
from owcopilot.storage import SQLiteStore


class QAProvider:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        return json.dumps(self.payload), 10, 5


class ExpandingQAProvider:
    """Returns query variants for an expansion call, and a fixed answer for the answer call."""

    def __init__(self, payload: dict, variants: list[str]) -> None:
        self.payload = payload
        self.variants = variants

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        if QA_EXPAND_MARKER in system:
            return json.dumps(self.variants), 5, 5
        return json.dumps(self.payload), 10, 5


def test_lore_qa_service_uses_query_expansion_when_enabled() -> None:
    store = SQLiteStore()
    try:
        bundle = ContentBundle(
            entities={"npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC)}
        )
        store.replace_content_index(bundle)
        telemetry = TelemetryCollector()
        gateway = LLMGateway(
            providers={
                "cheap": ExpandingQAProvider(
                    {
                        "answer": "Aldric is a caravan master.",
                        "citations": [{"ref": "entity:npc_aldric"}],
                        "confidence": 0.9,
                        "mentioned_entities": [],
                        "unresolved_mentions": [],
                    },
                    variants=["the caravan master", "who leads the caravan"],
                )
            },
            router=StaticRouter(mapping={"qa_answer": "cheap", "qa_expand": "cheap"}),
            cache=NoOpCache(),
            telemetry=telemetry,
        )
        service = LoreQAService(
            gateway=gateway,
            context_builder=ContextPackBuilder(bm25=BM25Retriever(store)),
            bundle=bundle,
            expand=True,
        )

        answer = service.ask("Aldric")

        assert answer.refused is False
        # the expansion call ran (and then the answer call)
        assert {record.task for record in telemetry.records} == {"qa_expand", "qa_answer"}
    finally:
        store.close()


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
        # No relevant lore at all: say so, rather than the same line used when lore exists but the
        # asked-for point isn't recorded.
        assert answer.answer == REFUSAL_NO_CONTEXT
        assert telemetry.records == []
    finally:
        store.close()


def test_refusal_distinguishes_ungrounded_from_no_context() -> None:
    """When relevant lore WAS retrieved but the model can't ground the specific point (e.g. no
    direct relation between two factions that both exist), the refusal must read as 'not recorded',
    not 'nothing found' — the trap where the tool seems not to know its own world."""
    store = SQLiteStore()
    try:
        bundle = ContentBundle(
            entities={"npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC)}
        )
        store.replace_content_index(bundle)
        # Context pack has a hit (Aldric), but the model self-refuses on the specific question.
        service, _telemetry = _service(
            {"answer": "", "citations": [], "confidence": 0, "refused": True},
            bundle,
            store,
        )

        answer = service.ask("Aldric")

        assert answer.refused is True
        assert answer.answer == REFUSAL_UNGROUNDED
    finally:
        store.close()


class _RawQAProvider:
    def __init__(self, raw: str) -> None:
        self.raw = raw

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        return self.raw, 10, 5


def test_lore_qa_service_refuses_on_unparseable_model_output() -> None:
    """A model reply that isn't valid JSON must make the service REFUSE — never crash the request,
    never fabricate an answer. Refusal is the designed behaviour, not a degradation."""
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
        gateway = LLMGateway(
            providers={"cheap": _RawQAProvider("Sorry — I can't help with that. (no JSON)")},
            router=StaticRouter(mapping={"qa_answer": "cheap"}),
            cache=NoOpCache(),
            telemetry=TelemetryCollector(),
        )
        service = LoreQAService(
            gateway=gateway,
            context_builder=ContextPackBuilder(bm25=BM25Retriever(store)),
            bundle=bundle,
        )

        answer = service.ask("Aldric")  # must not raise

        assert answer.refused is True
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
