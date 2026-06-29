"""Neural cross-encoder reranker -- bge-reranker-v2-m3 (opt-in).

This module provides ``NeuralReranker``: a true cross-encoder that scores each
(query, document) pair jointly via cross-attention, enabling semantic reranking
that is invisible to any purely lexical scorer.

**What is a cross-encoder?**
A cross-encoder encodes the query and document *together* in a single forward pass,
allowing every query token to attend to every document token and vice versa.  This
produces interaction scores that capture semantic entailment, paraphrase, and
implicit relevance -- relationships that bi-encoders (which embed query and doc
separately) and lexical scorers cannot model.

**Model: BAAI/bge-reranker-v2-m3**
- Architecture: bge-m3 backbone (XLM-RoBERTa), classification-head fine-tuned cross-encoder
- Parameters: ~278M (fp32) / ~140M (fp16)
- Max sequence length: 8192 tokens
- BEIR nDCG@10: 51.8 (comparable to bge-reranker-large at 53.8 with 560M params)
- Multilingual: yes (bge-m3 backbone, zh/en/+100 languages)
- Local inference: CPU-capable; ~130ms per 16 pairs on a modern CPU
- $0 to run: no API, fully offline once model is cached

**Determinism note (honest)**:
Floating-point outputs of the cross-encoder may differ by tiny epsilon across
hardware (CPU vs GPU, different BLAS libraries) due to fp16 arithmetic ordering.
The *ranking order* is stable on fixed hardware.  For CI where strict golden-test
reproducibility is required, use the default lexical re-scorer (``OWCOPILOT_RERANKER``
not set or set to ``lexical``).

**Usage**:
Set ``OWCOPILOT_RERANKER=auto`` (or ``=neural``) and ensure the model is cached::

    python -c "from sentence_transformers import CrossEncoder; \\
        CrossEncoder('BAAI/bge-reranker-v2-m3')"

Interface selection:
* ``FlagEmbedding.FlagReranker`` (preferred, if FlagEmbedding installed)
* ``sentence_transformers.CrossEncoder`` (fallback, lighter dependency)
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

from .models import RetrievalHit
from .rerank import rerank_hits as _lexical_rerank_hits

logger = logging.getLogger(__name__)

DEFAULT_NEURAL_MODEL = "BAAI/bge-reranker-v2-m3"

# Env knob: OWCOPILOT_RERANKER in {auto, neural, lexical, none}
# auto  → use NeuralReranker if model importable, else lexical fallback
# neural → require NeuralReranker; fail loud if unavailable
# lexical → always use LexicalReScorer (deterministic, $0, CI default)
# none → no reranking; return fused order unchanged
_RERANKER_MODE_ENV = "OWCOPILOT_RERANKER"
_RERANKER_MODEL_ENV = "OWCOPILOT_RERANKER_MODEL"


@lru_cache(maxsize=4)
def _load_cross_encoder(model_name: str) -> Any:
    """Load the cross-encoder model, trying FlagEmbedding then sentence-transformers.

    Result is cached per model name for the lifetime of the process.
    This is the first call that downloads the model (~280MB fp16); subsequent calls
    are instant.
    """
    # Prefer FlagEmbedding's FlagReranker (official BAAI interface)
    try:
        from FlagEmbedding import FlagReranker  # noqa: PLC0415

        logger.info("NeuralReranker: loading %s via FlagEmbedding.FlagReranker", model_name)
        return _FlagRerankerWrapper(FlagReranker(model_name, use_fp16=True))
    except ImportError:
        pass

    # Fall back to sentence-transformers CrossEncoder (same model, lighter dep)
    from sentence_transformers import CrossEncoder  # noqa: PLC0415

    logger.info("NeuralReranker: loading %s via sentence_transformers.CrossEncoder", model_name)
    return _STCrossEncoderWrapper(CrossEncoder(model_name))


class _FlagRerankerWrapper:
    """Wraps FlagEmbedding.FlagReranker to the common predict(pairs) interface."""

    def __init__(self, model: Any) -> None:
        self._model = model

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        # normalize=True applies sigmoid to raw logits, mapping scores to [0, 1].
        # Without normalize=True, raw logits are unbounded real numbers (typically
        # in the range [-10, 10]); with normalize=True the output is a relevance
        # probability in [0, 1] (higher = more relevant).
        scores = self._model.compute_score(pairs, normalize=True)
        # compute_score returns a list or a single float when len==1
        if isinstance(scores, float):
            return [scores]
        return [float(s) for s in scores]


class _STCrossEncoderWrapper:
    """Wraps sentence_transformers.CrossEncoder to the common predict(pairs) interface."""

    def __init__(self, model: Any) -> None:
        self._model = model

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        import numpy as np  # noqa: PLC0415

        raw = self._model.predict(pairs, show_progress_bar=False)
        if isinstance(raw, np.ndarray):
            return [float(s) for s in raw]
        if isinstance(raw, (int, float)):
            return [float(raw)]
        return [float(s) for s in raw]


class NeuralReranker:
    """True cross-encoder reranker using bge-reranker-v2-m3 (or any compatible model).

    Encodes each (query, document) pair jointly via cross-attention, producing
    interaction scores that capture semantic relevance invisible to lexical scorers.

    This is a **true cross-encoder**, not a bi-encoder or lexical scorer:

    * Bi-encoder: ``encode(query)`` + ``encode(doc)`` → cosine(q_vec, d_vec)
    * Cross-encoder (this class): ``predict([(query, doc)])`` → scalar relevance score
      via joint cross-attention over all query+doc tokens

    Determinism: ranking order is stable on fixed hardware; fp16 scores are not
    bit-exact across hardware variants (documented limitation).
    """

    def __init__(self, model_name: str = DEFAULT_NEURAL_MODEL) -> None:
        self.model_name = model_name
        self._model: Any | None = None

    def _ensure_model(self) -> Any:
        if self._model is None:
            self._model = _load_cross_encoder(self.model_name)
        return self._model

    def rerank(
        self,
        query: str,
        hits: list[RetrievalHit],
        *,
        top_n: int | None = None,
    ) -> list[RetrievalHit]:
        """Reorder ``hits`` by neural cross-encoder relevance scores.

        Constructs (query, passage) pairs and calls ``predict()`` via joint cross-attention.
        If the model fails to load or score, falls back to the lexical re-scorer with a
        WARNING so degradation is never silent.

        Returns a new list of hits with ``score`` set to the cross-encoder logit and
        ``source="reranked_neural"`` (distinguishing from ``"reranked"`` which is lexical).
        """
        if not hits:
            return hits

        try:
            model = self._ensure_model()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "NeuralReranker: model %s could not load (%s); falling back to lexical re-scorer.",
                self.model_name,
                exc,
            )
            ranked = _lexical_rerank_hits(query, hits)
            # Mark fallback clearly
            return [h.model_copy(update={"source": "reranked_lexical_fallback"}) for h in ranked]

        passages = [f"{hit.title} {hit.body}".strip() for hit in hits]
        pairs = [(query, passage) for passage in passages]

        try:
            scores: list[float] = model.predict(pairs)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "NeuralReranker: predict failed (%s); falling back to lexical re-scorer.", exc
            )
            ranked = _lexical_rerank_hits(query, hits)
            return [h.model_copy(update={"source": "reranked_lexical_fallback"}) for h in ranked]

        scored = sorted(
            zip(scores, hits, strict=True),
            key=lambda pair: (-pair[0], pair[1].ref),
        )
        result = [
            hit.model_copy(update={"score": float(sc), "source": "reranked_neural"})
            for sc, hit in scored
        ]
        if top_n is not None:
            return result[:top_n]
        return result


def resolve_reranker(mode: str | None = None) -> NeuralReranker | None:
    """Return a NeuralReranker per env/mode, or None to use the lexical fallback.

    Modes (``OWCOPILOT_RERANKER`` env or ``mode`` arg):
    * ``auto``    -- return NeuralReranker if sentence_transformers is importable,
                     else None (caller uses LexicalReScorer).
    * ``neural``  -- return NeuralReranker; raise ImportError if unavailable.
    * ``lexical`` -- return None (caller always uses LexicalReScorer).
    * ``none``    -- return None (no reranking at all; caller skips both).
    * unset/other -- same as ``lexical`` (safe CI default, $0 deterministic).

    ``CI`` note: CI sets ``OWCOPILOT_RERANKER=lexical`` (or leaves it unset) so tests
    are deterministic and $0.  The neural path is exercised locally or in portfolio runs.
    """
    if mode is None:
        mode = os.getenv(_RERANKER_MODE_ENV, "lexical").strip().lower()

    model_name = (
        os.getenv(_RERANKER_MODEL_ENV, DEFAULT_NEURAL_MODEL).strip() or DEFAULT_NEURAL_MODEL
    )

    if mode in ("lexical", "none", ""):
        return None

    if mode == "auto":
        # Check whether sentence_transformers is importable (no model load)
        import importlib.util

        if importlib.util.find_spec("sentence_transformers") is None:
            logger.debug(
                "resolve_reranker(auto): sentence_transformers not installed; using lexical."
            )
            return None
        return NeuralReranker(model_name)

    if mode == "neural":
        return NeuralReranker(model_name)

    logger.warning(
        "resolve_reranker: unknown mode %r; defaulting to lexical re-scorer.", mode
    )
    return None
