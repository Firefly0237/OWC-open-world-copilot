"""Tests for NeuralReranker (retrieval/neural_rerank.py).

These tests use mocks for the cross-encoder model so they run $0 and deterministically
in CI without loading any ML model.  The neural path with a real model is exercised
locally / in portfolio review.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from owcopilot.retrieval.models import RetrievalHit
from owcopilot.retrieval.neural_rerank import (
    NeuralReranker,
    _STCrossEncoderWrapper,
    resolve_reranker,
)


def _hit(ref: str, title: str, body: str = "", *, score: float = 0.5) -> RetrievalHit:
    return RetrievalHit(
        ref=ref,
        object_type=ref.split(":", 1)[0],
        title=title,
        body=body,
        score=score,
        source="rrf",
    )


# ---------------------------------------------------------------------------
# _STCrossEncoderWrapper
# ---------------------------------------------------------------------------


def test_st_wrapper_predict_returns_float_list() -> None:
    """Wrapper converts CrossEncoder output to list[float]."""
    mock_ce = MagicMock()
    mock_ce.predict.return_value = [0.9, 0.3, 0.7]
    wrapper = _STCrossEncoderWrapper(mock_ce)
    scores = wrapper.predict([("query", "doc1"), ("query", "doc2"), ("query", "doc3")])
    assert scores == pytest.approx([0.9, 0.3, 0.7])
    # Verify predict was called with pairs and no progress bar
    mock_ce.predict.assert_called_once_with(
        [("query", "doc1"), ("query", "doc2"), ("query", "doc3")],
        show_progress_bar=False,
    )


def test_st_wrapper_predict_handles_scalar() -> None:
    """Wrapper handles single-pair scalar return from CrossEncoder."""
    mock_ce = MagicMock()
    mock_ce.predict.return_value = 0.85
    wrapper = _STCrossEncoderWrapper(mock_ce)
    scores = wrapper.predict([("q", "d")])
    assert scores == pytest.approx([0.85])


# ---------------------------------------------------------------------------
# NeuralReranker.rerank -- with mock model
# ---------------------------------------------------------------------------


def _make_reranker_with_mock(scores: list[float]) -> NeuralReranker:
    """Return a NeuralReranker whose cross-encoder is mocked to return ``scores``."""
    reranker = NeuralReranker("mock-model")
    mock_model = MagicMock()
    mock_model.predict.return_value = scores
    reranker._model = mock_model
    return reranker


def test_neural_reranker_reorders_by_score() -> None:
    """Higher cross-encoder score → earlier position in output."""
    hits = [
        _hit("entity:a", "Alpha doc"),
        _hit("entity:b", "Better doc"),
        _hit("entity:c", "Gamma doc"),
    ]
    # b scores highest, then c, then a
    reranker = _make_reranker_with_mock([0.1, 0.9, 0.5])
    ranked = reranker.rerank("query", hits)
    assert [h.ref for h in ranked] == ["entity:b", "entity:c", "entity:a"]


def test_neural_reranker_sets_source_to_reranked_neural() -> None:
    hits = [_hit("entity:a", "Alpha"), _hit("entity:b", "Beta")]
    reranker = _make_reranker_with_mock([0.8, 0.2])
    ranked = reranker.rerank("query", hits)
    assert all(h.source == "reranked_neural" for h in ranked)


def test_neural_reranker_sets_score_from_cross_encoder() -> None:
    hits = [_hit("entity:a", "Alpha"), _hit("entity:b", "Beta")]
    reranker = _make_reranker_with_mock([0.7, 0.3])
    ranked = reranker.rerank("query", hits)
    # First hit (entity:a) had score 0.7 → should be first
    assert ranked[0].ref == "entity:a"
    assert ranked[0].score == pytest.approx(0.7)


def test_neural_reranker_top_n_limit() -> None:
    hits = [_hit(f"entity:{c}", c) for c in "abcde"]
    reranker = _make_reranker_with_mock([0.5, 0.9, 0.1, 0.8, 0.3])
    ranked = reranker.rerank("query", hits, top_n=2)
    assert len(ranked) == 2
    assert ranked[0].ref == "entity:b"  # score 0.9
    assert ranked[1].ref == "entity:d"  # score 0.8


def test_neural_reranker_empty_input_returns_empty() -> None:
    reranker = NeuralReranker("mock")
    result = reranker.rerank("query", [])
    assert result == []


def test_neural_reranker_falls_back_to_lexical_on_model_load_failure() -> None:
    """When model cannot load, fallback is lexical (source='reranked_lexical_fallback')."""
    reranker = NeuralReranker("nonexistent-model-xyz")
    hits = [
        _hit("entity:a", "Alpha patrol beacon"),
        _hit("entity:b", "Unrelated document"),
    ]
    # No mock model; _ensure_model will try to import and fail → fallback
    with patch(
        "owcopilot.retrieval.neural_rerank._load_cross_encoder",
        side_effect=ImportError("no model"),
    ):
        ranked = reranker.rerank("patrol beacon query", hits)
    # Falls back; source is marked to distinguish from neural
    assert all(h.source == "reranked_lexical_fallback" for h in ranked)


def test_neural_reranker_predict_failure_falls_back_to_lexical() -> None:
    """When predict() raises, fallback is lexical re-scoring."""
    hits = [_hit("entity:a", "Alpha"), _hit("entity:b", "Beta")]
    reranker = NeuralReranker("mock")
    mock_model = MagicMock()
    mock_model.predict.side_effect = RuntimeError("CUDA OOM")
    reranker._model = mock_model
    ranked = reranker.rerank("query", hits)
    assert all(h.source == "reranked_lexical_fallback" for h in ranked)


# ---------------------------------------------------------------------------
# NeuralReranker is a TRUE cross-encoder (joint encoding, not bi-encoder)
# ---------------------------------------------------------------------------


def test_neural_reranker_calls_predict_with_pairs_not_separate_encode() -> None:
    """CRITICAL: must call predict([(query, doc)]) -- not encode(query) + encode(doc).

    This test is the key guard against the "bi-encoder masquerading as cross-encoder" anti-pattern.
    A cross-encoder takes pairs; a bi-encoder takes separate inputs.
    """
    hits = [_hit("entity:a", "A title", "A body")]
    reranker = _make_reranker_with_mock([0.8])
    reranker.rerank("test query", hits)

    # Verify predict was called with (query, passage) pairs -- not separate encode calls
    mock_model = reranker._model
    mock_model.predict.assert_called_once()
    call_args = mock_model.predict.call_args[0][0]  # first positional arg
    assert isinstance(call_args, list)
    assert len(call_args) == 1
    query_part, doc_part = call_args[0]
    assert query_part == "test query"
    assert "A title" in doc_part  # passage = title + body


# ---------------------------------------------------------------------------
# resolve_reranker
# ---------------------------------------------------------------------------


def test_resolve_reranker_lexical_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_RERANKER", "lexical")
    assert resolve_reranker() is None


def test_resolve_reranker_none_mode_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_RERANKER", "none")
    assert resolve_reranker() is None


def test_resolve_reranker_unset_defaults_to_lexical(monkeypatch) -> None:
    monkeypatch.delenv("OWCOPILOT_RERANKER", raising=False)
    assert resolve_reranker() is None


def test_resolve_reranker_neural_returns_instance(monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_RERANKER", "neural")
    reranker = resolve_reranker()
    assert isinstance(reranker, NeuralReranker)


def test_resolve_reranker_auto_returns_neural_when_st_available(monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_RERANKER", "auto")
    import importlib.util

    if importlib.util.find_spec("sentence_transformers") is None:
        pytest.skip("sentence_transformers not installed")
    reranker = resolve_reranker()
    assert isinstance(reranker, NeuralReranker)


def test_resolve_reranker_auto_returns_none_when_st_missing(monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_RERANKER", "auto")
    with patch("importlib.util.find_spec", return_value=None):
        reranker = resolve_reranker("auto")
    assert reranker is None


def test_resolve_reranker_uses_custom_model(monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_RERANKER", "neural")
    monkeypatch.setenv("OWCOPILOT_RERANKER_MODEL", "my/custom-reranker")
    reranker = resolve_reranker()
    assert isinstance(reranker, NeuralReranker)
    assert reranker.model_name == "my/custom-reranker"


# ---------------------------------------------------------------------------
# Docstring / architecture description regression tests (RT5)
# ---------------------------------------------------------------------------


def test_neural_rerank_docstring_says_classification_head_not_lora() -> None:
    """RT5: the architecture description must say 'classification-head fine-tuned',
    not 'LoRA fine-tuned'.  bge-reranker-v2-m3 uses a classification head on top of
    XLM-RoBERTa, not a LoRA adapter.
    """
    import owcopilot.retrieval.neural_rerank as neural_mod

    doc = neural_mod.__doc__ or ""
    assert "LoRA fine-tuned" not in doc, (
        "Module docstring still says 'LoRA fine-tuned'. "
        "bge-reranker-v2-m3 uses classification-head fine-tuning, not LoRA. "
        "Change to 'classification-head fine-tuned cross-encoder'."
    )
    assert "classification-head" in doc, (
        "Module docstring should describe the model as 'classification-head fine-tuned'."
    )


def test_flag_reranker_wrapper_docstring_notes_normalize_value_range() -> None:
    """RT5: _FlagRerankerWrapper.predict must document that normalize=True maps to [0, 1]."""
    import inspect

    from owcopilot.retrieval.neural_rerank import _FlagRerankerWrapper

    source = inspect.getsource(_FlagRerankerWrapper.predict)
    # Check that the normalize value range is documented
    assert "[0, 1]" in source or "0, 1" in source, (
        "_FlagRerankerWrapper.predict should document that normalize=True maps scores to [0, 1]. "
        "Add a comment explaining the value range."
    )
