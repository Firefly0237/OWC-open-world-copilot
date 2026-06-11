from __future__ import annotations

import json

from owcopilot.assist.barks import BarkBatchService, parse_bark_texts
from owcopilot.assist.review_queue import ReviewQueue
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.llm.telemetry import TelemetryCollector


class BarkProvider:
    def __init__(self, variants: list[str]) -> None:
        self.variants = variants

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        return json.dumps({"variants": self.variants}), 10, 5


def test_parse_bark_texts_accepts_fenced_json() -> None:
    assert parse_bark_texts("""```json\n["Hold!", "Move."]\n```""") == ["Hold!", "Move."]


def test_bark_batch_service_filters_invalid_variants_and_enqueues_valid_ones() -> None:
    bundle = ContentBundle(
        entities={
            "npc_guard": Entity(id="npc_guard", name="Guard", type=EntityType.NPC),
            "npc_mara": Entity(id="npc_mara", name="Mara", type=EntityType.NPC),
        }
    )
    telemetry = TelemetryCollector()
    gateway = LLMGateway(
        providers={"cheap": BarkProvider(["Hold!", "Mara sent you."])},
        router=StaticRouter(mapping={"barks_batch": "cheap"}),
        cache=NoOpCache(),
        telemetry=telemetry,
    )
    queue = ReviewQueue()
    service = BarkBatchService(gateway=gateway, bundle=bundle, review_queue=queue)

    result = service.generate(
        speaker_ids=["npc_guard"],
        topic="intruder",
        variants_per_speaker=2,
        max_chars=20,
    )

    assert [variant.text for variant in result.accepted] == ["Hold!"]
    assert result.rejected[0].issues[0].code == "FORBIDDEN_ENTITY_REF"
    assert len(queue.list_pending()) == 1
    assert queue.list_pending()[0].payload["text"] == "Hold!"
    assert telemetry.records[0].task == "barks_batch"
