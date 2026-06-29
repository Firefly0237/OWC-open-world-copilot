"""Machine-readable degrade marker on the auto-mode semantic embedder.

The auto path falls back to the hashing stub when the semantic model can't load, and logs a warning.
The warning alone is invisible to callers, so the embedder also exposes a machine-readable
``degraded`` flag (and switches ``model_id`` to the fallback's id). These tests pin that contract
WITHOUT needing the real ~2GB model — they force the fallback by pointing at a non-existent model.
"""

from __future__ import annotations

from owcopilot.llm.cache import HashingEmbedder
from owcopilot.retrieval.embedding import SemanticEmbedder


def test_semantic_embedder_starts_undegraded() -> None:
    embedder = SemanticEmbedder("BAAI/bge-m3")
    assert embedder.degraded is False
    assert embedder.model_id == "st:BAAI/bge-m3"


def test_fallback_sets_machine_readable_degraded_flag_and_switches_model_id() -> None:
    # A model name that cannot resolve forces the lazy load to fail → degrade to hashing.
    embedder = SemanticEmbedder("nonexistent/model-for-degrade-test-xyz")
    assert embedder.degraded is False  # not yet — load is lazy, hasn't been attempted

    vectors = embedder.embed_many(["护送商队穿过雾脊山道", "escort the caravan"])

    # retrieval keeps working (BM25-grade vectors), but the degrade is now observable to callers
    assert len(vectors) == 2
    assert embedder.degraded is True
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
