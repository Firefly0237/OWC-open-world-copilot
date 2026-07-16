"""Tests for the opt-in, layered, default-off faithfulness (entailment) gate on the QA product path.

These pin the整改 target: ``verify_qa_answer`` is a *citation-existence* check that lets a
real-but-unsupporting citation through, and the faithfulness gate — when opted in — catches exactly
that on the SAME product answer path (``LoreQAService.ask``), annotating the answer without ever
blocking or rewriting it. No real model is ever called: the offline judge is deterministic and the
llm-unavailable path is exercised with an empty-provider gateway.
"""

from __future__ import annotations

import json

import pytest

from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.llm.telemetry import TelemetryCollector
from owcopilot.qa.models import Citation, QAAnswer
from owcopilot.qa.service import LoreQAService, _resolve_faithfulness_mode
from owcopilot.retrieval.bm25 import BM25Retriever
from owcopilot.retrieval.context_pack import ContextPackBuilder
from owcopilot.retrieval.models import ContextPack, RetrievalHit
from owcopilot.storage import SQLiteStore

# Aldric's real, retrieved description says nothing about a favourite food — so a "favourite food"
# answer cites a real ref (existence check passes) while asserting a fact the evidence never states.
_ALDRIC_DESC = "Aldric is the caravan master of the iron road who leads escort missions."


class _QAProvider:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        return json.dumps(self.payload), 10, 5


def _service(
    payload: dict, bundle: ContentBundle, store: SQLiteStore, **kwargs: object
) -> LoreQAService:
    gateway = LLMGateway(
        providers={"cheap": _QAProvider(payload)},
        router=StaticRouter(mapping={"qa_answer": "cheap"}),
        cache=NoOpCache(),
        telemetry=TelemetryCollector(),
    )
    return LoreQAService(
        gateway=gateway,
        context_builder=ContextPackBuilder(bm25=BM25Retriever(store)),
        bundle=bundle,
        **kwargs,  # type: ignore[arg-type]
    )


def _aldric_store() -> tuple[SQLiteStore, ContentBundle]:
    bundle = ContentBundle(
        entities={
            "npc_aldric": Entity(
                id="npc_aldric",
                name="Aldric",
                type=EntityType.NPC,
                description=_ALDRIC_DESC,
            )
        }
    )
    store = SQLiteStore()
    store.replace_content_index(bundle)
    return store, bundle


_FAVOURITE_FOOD_PAYLOAD = {
    "answer": "Aldric's favourite food is roasted lamb.",
    "citations": [{"ref": "entity:npc_aldric"}],
    "confidence": 0.75,
    "mentioned_entities": ["Aldric"],
    "unresolved_mentions": [],
}
_EXTRACTIVE_PAYLOAD = {
    "answer": "Aldric is the caravan master of the iron road.",
    "citations": [{"ref": "entity:npc_aldric"}],
    "confidence": 0.9,
    "mentioned_entities": ["Aldric"],
    "unresolved_mentions": [],
}


def test_faithfulness_off_by_default_leaves_answer_unannotated() -> None:
    """Default (no faithfulness arg): the answer is byte-compatible with the pre-gate behaviour —
    grounded, and carrying no faithfulness annotation."""
    store, bundle = _aldric_store()
    try:
        service = _service(_FAVOURITE_FOOD_PAYLOAD, bundle, store)

        answer = service.ask("Aldric")

        assert answer.grounded is True
        assert answer.refused is False
        assert answer.faithfulness is None
    finally:
        store.close()


def test_offline_judge_flags_unsupported_fact_that_existence_check_passes() -> None:
    """The整改 bullseye: citation-existence grounding accepts the real-but-unsupporting citation,
    but the offline faithfulness judge flags the fabricated fact as unsupported — WITHOUT blocking
    the answer (annotate, not block)."""
    store, bundle = _aldric_store()
    try:
        service = _service(_FAVOURITE_FOOD_PAYLOAD, bundle, store, faithfulness="offline")

        answer = service.ask("Aldric")

        # Existence grounding still passed; the answer is NOT suppressed or rewritten.
        assert answer.grounded is True
        assert answer.refused is False
        assert answer.answer == "Aldric's favourite food is roasted lamb."
        # But faithfulness caught the unsupported fact.
        faithfulness = answer.faithfulness
        assert faithfulness is not None
        assert faithfulness.judge == "offline"
        assert faithfulness.ok is False
        assert faithfulness.score is not None and faithfulness.score < 1.0
        assert "Aldric's favourite food is roasted lamb." in faithfulness.unsupported_claims
        assert "核对" in faithfulness.note  # reviewer-facing guidance
    finally:
        store.close()


def test_offline_judge_passes_grounded_extractive_answer() -> None:
    """An answer whose text is drawn from the retrieved evidence clears the gate: faithfulness_ok
    stays True and the offline path stays green ($0, deterministic)."""
    store, bundle = _aldric_store()
    try:
        service = _service(_EXTRACTIVE_PAYLOAD, bundle, store, faithfulness="offline")

        answer = service.ask("Aldric")

        assert answer.grounded is True
        faithfulness = answer.faithfulness
        assert faithfulness is not None
        assert faithfulness.judge == "offline"
        assert faithfulness.ok is True
        assert faithfulness.score == 1.0
        assert faithfulness.unsupported_claims == []
    finally:
        store.close()


def test_llm_mode_without_provider_degrades_to_unavailable() -> None:
    """llm mode + a gateway with no usable provider must NOT fake a pass: it reports
    judge='unavailable' with a null score and reviewer guidance. Exercised on the annotation seam
    directly because an empty-provider gateway cannot answer the question in the first place."""
    gateway = LLMGateway(providers={})  # no provider → judge is unavailable
    service = LoreQAService(
        gateway=gateway,
        context_builder=ContextPackBuilder(bm25=BM25Retriever(SQLiteStore())),
        bundle=ContentBundle(),
        faithfulness="llm",
    )
    pack = ContextPack(
        query="Aldric",
        budget_tokens=100,
        hits=[
            RetrievalHit(
                ref="entity:npc_aldric",
                object_type="entity",
                title="Aldric",
                body=_ALDRIC_DESC,
                score=1.0,
                source="test",
            )
        ],
    )
    answer = QAAnswer(
        answer="Aldric is the caravan master of the iron road.",
        citations=[Citation(ref="entity:npc_aldric")],
        mentioned_entities=["Aldric"],
        grounded=True,
    )

    faithfulness = service._faithfulness_annotation(answer, pack)

    assert faithfulness is not None
    assert faithfulness.judge == "unavailable"
    assert faithfulness.ok is False
    assert faithfulness.score is None
    assert faithfulness.unsupported_claims == []
    assert "未经蕴含校验" in faithfulness.note


def test_env_backstop_enables_gate_when_no_constructor_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWCOPILOT_QA_FAITHFULNESS", "offline")
    store, bundle = _aldric_store()
    try:
        service = _service(_FAVOURITE_FOOD_PAYLOAD, bundle, store)  # no faithfulness arg

        answer = service.ask("Aldric")

        assert answer.faithfulness is not None
        assert answer.faithfulness.judge == "offline"
    finally:
        store.close()


def test_constructor_arg_overrides_env_backstop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWCOPILOT_QA_FAITHFULNESS", "offline")
    store, bundle = _aldric_store()
    try:
        service = _service(_FAVOURITE_FOOD_PAYLOAD, bundle, store, faithfulness="off")

        answer = service.ask("Aldric")

        # Constructor 'off' wins over the env's 'offline'.
        assert answer.faithfulness is None
    finally:
        store.close()


def test_unknown_faithfulness_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # A typo'd constructor value fails loudly rather than silently disabling the gate.
    with pytest.raises(ValueError, match="忠实性校验模式"):
        _resolve_faithfulness_mode("bogus")
    # ...and so does a typo'd env backstop when no constructor arg is given.
    monkeypatch.setenv("OWCOPILOT_QA_FAITHFULNESS", "bogus")
    with pytest.raises(ValueError, match="忠实性校验模式"):
        _resolve_faithfulness_mode(None)


def test_cli_ask_rejects_llm_faithfulness_without_real_mode(tmp_path, capsys) -> None:
    """The CLI boundary refuses faithfulness=llm without --llm-mode real rather than feeding the
    offline QA double to the judge (no silent downgrade)."""
    from owcopilot.cli.main import main
    from owcopilot.content.store import ContentStore

    content_root = tmp_path / "content"
    ContentStore(content_root).save(_aldric_store()[1])

    code = main(
        [
            "ask",
            "--content-root",
            str(content_root),
            "--query",
            "Aldric",
            "--llm-mode",
            "offline",
            "--faithfulness",
            "llm",
        ]
    )

    assert code == 2  # main() maps the guided ValueError to exit 2
    err = json.loads(capsys.readouterr().err)
    assert "llm-mode real" in err["error"]


def test_api_ask_rejects_llm_faithfulness_without_real_mode(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastapi", reason="install with: pip install -e '.[serve]'")
    from fastapi.testclient import TestClient

    from owcopilot.service.api import create_app

    # Isolate the managed-worlds home so a stray 'demo' materialization can't touch the real home.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OWCOPILOT_PROJECTS_JSON", raising=False)
    client = TestClient(create_app())

    response = client.post(
        "/projects/demo/ask",
        json={"content": {}, "query": "Aldric", "faithfulness": "llm"},  # llm_mode defaults offline
    )

    assert response.status_code == 400
    assert "llm_mode='real'" in response.json()["detail"]
