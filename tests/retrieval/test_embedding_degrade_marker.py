"""Machine-readable degrade marker on the auto-mode semantic embedder.

The auto path falls back to the hashing stub when the semantic model can't load, and logs a warning.
The warning alone is invisible to callers, so the embedder also exposes a machine-readable
``degraded`` flag (and switches ``model_id`` to the fallback's id). These tests pin that contract
WITHOUT needing the real ~2GB model — they force the fallback by pointing at a non-existent model.
"""

from __future__ import annotations

import sys
from types import ModuleType

from owcopilot.llm.cache import HashingEmbedder
from owcopilot.retrieval import embedding
from owcopilot.retrieval.embedding import SemanticEmbedder


def test_semantic_embedder_starts_undegraded() -> None:
    embedder = SemanticEmbedder("BAAI/bge-m3")
    assert embedder.degraded is False
    assert embedder.model_id == (
        "st:BAAI/bge-m3@5617a9f61b028005a4858fdac845db406aefb181"
    )
    assert embedder.model_revision == "5617a9f61b028005a4858fdac845db406aefb181"


def test_semantic_loader_resolves_exact_revision_before_model_load(monkeypatch) -> None:
    calls: dict[str, object] = {}
    snapshot_path = "X:/huggingface/bge-m3/snapshots/exact"
    sentinel = object()

    hub_module = ModuleType("huggingface_hub")

    def fake_snapshot_download(**kwargs: object) -> str:
        calls["snapshot"] = kwargs
        return snapshot_path

    hub_module.snapshot_download = fake_snapshot_download  # type: ignore[attr-defined]
    sentence_transformers_module = ModuleType("sentence_transformers")

    def fake_sentence_transformer(model_source: str) -> object:
        calls["model_source"] = model_source
        return sentinel

    sentence_transformers_module.SentenceTransformer = fake_sentence_transformer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub_module)
    monkeypatch.setitem(sys.modules, "sentence_transformers", sentence_transformers_module)
    embedding._load_model.cache_clear()
    try:
        result = embedding._load_model(
            "BAAI/bge-m3",
            "5617a9f61b028005a4858fdac845db406aefb181",
        )
    finally:
        embedding._load_model.cache_clear()

    assert result is sentinel
    assert calls == {
        "snapshot": {
                "repo_id": "BAAI/bge-m3",
                "revision": "5617a9f61b028005a4858fdac845db406aefb181",
                "max_workers": 1,
        },
        "model_source": snapshot_path,
    }


def test_fallback_sets_machine_readable_degraded_flag_and_switches_model_id() -> None:
    # A model name that cannot resolve forces the lazy load to fail → degrade to hashing.
    embedder = SemanticEmbedder("nonexistent/model-for-degrade-test-xyz")
    assert embedder.degraded is False  # not yet — load is lazy, hasn't been attempted

    vectors = embedder.embed_many(["护送商队穿过雾脊山道", "escort the caravan"])

    # retrieval keeps working (BM25-grade vectors), but the degrade is now observable to callers
    assert len(vectors) == 2
    assert embedder.degraded is True
    assert embedder.degradation_reason is not None
    assert embedder.degradation_reason.startswith("semantic_model_load_failed:")
    # model_id flips to the fallback's id so persisted vectors are keyed correctly, not as "st:*"
    assert embedder.model_id == HashingEmbedder().model_id
    assert not embedder.model_id.startswith("st:")


def test_degrade_flag_is_stable_across_subsequent_calls() -> None:
    embedder = SemanticEmbedder("nonexistent/model-for-degrade-test-xyz")
    embedder.embed_many(["alpha"])
    assert embedder.degraded is True
    # a second batch goes straight through the cached fallback and stays degraded
    embedder.embed_many(["beta"])
    assert embedder.degraded is True


def test_auto_missing_optional_runtime_exposes_reason(monkeypatch) -> None:
    monkeypatch.delenv("OWCOPILOT_EMBEDDER", raising=False)
    monkeypatch.setattr(embedding, "semantic_installed", lambda: False)

    embedder = embedding.resolve_embedder()

    assert isinstance(embedder, HashingEmbedder)
    assert embedder.degraded is True  # type: ignore[attr-defined]
    assert embedder.degradation_reason == "sentence_transformers_not_installed"  # type: ignore[attr-defined]
