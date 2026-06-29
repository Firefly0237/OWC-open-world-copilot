"""Field-weighted lexical re-scoring -- the precision half of two-stage retrieval.

This module implements **LexicalReScorer**: a deterministic, field-weighted lexical
re-scoring stage placed after multi-retriever recall (BM25 + vector + graph expansion,
fused by reciprocal-rank fusion).

**What this is**: a lexical re-scorer that combines explicit signals -- term coverage,
field-weighted density, exact-phrase bonus, recall prior, and (when available) cosine
similarity from a real embedding model. There is no learned weight and no neural
cross-attention. The ordering is golden-testable and matches the project's
deterministic-and-auditable north star.

**What this is NOT**: this is NOT a neural cross-encoder. A true cross-encoder (e.g.
bge-reranker-v2-m3, sentence-transformers CrossEncoder) encodes query and document
*jointly* via cross-attention, computing interaction scores that capture semantic
entailment and paraphrase relationships invisible to any lexical scorer. For the neural
cross-encoder path see ``retrieval/neural_rerank.py`` (opt-in via
``OWCOPILOT_RERANKER=auto`` when model is cached locally).

**Why lexical re-scoring still matters**: RRF collapses every candidate down to its
*rank* and discards match strength. Graph expansion floods the candidate set with 1-2 hop
neighbours that share no query terms. The lexical re-scorer restores field-signal
(title hit >> body hit) and exact-phrase bonuses, so the most on-topic document rises
before the token budget is spent. The semantic leg (cosine from bge-m3) is added when
real embeddings are available, making the scorer hybrid rather than purely lexical.

The signals combined:

* breadth  -- the fraction of distinct query terms the document covers,
* depth    -- a field-weighted, specificity-weighted (longer term = more informative)
              density of those matches, with a title hit worth more than a body hit,
* exactness-- a bonus when the whole query appears verbatim in the title or body,
* consensus-- a small prior from the fused recall score, so multi-retriever agreement
              breaks ties without ever overriding a clearly more on-topic document,
* semantic -- (only when real embeddings are available) the cosine similarity of the
              query to the document, so a paraphrase or synonym that shares *no* words with
              the canon still ranks. Without this signal a purely lexical re-scorer would
              demote exactly the semantic hits the dense retriever worked to surface --
              so the re-scorer is hybrid whenever the vector leg is semantic, and degrades
              to lexical-only (deterministic) when it is the hashing stub.

Re-scoring never invents, drops, or rewrites candidates: it only reorders the fused list.
A query with no usable terms (punctuation/emoji only) leaves the recall order untouched.
"""

from __future__ import annotations

import re

from .models import RetrievalHit
from .text_match import query_terms

_WS_RE = re.compile(r"\s+")

# ---------------------------------------------------------------------------
# Public alias: LexicalReScorer
# The callable below (rerank_hits) is the implementation.  The class name
# exists so that call-sites and documentation can use the honest name without
# importing a different symbol.
# ---------------------------------------------------------------------------


class LexicalReScorer:
    """Field-weighted lexical re-scorer for fused retrieval candidates.

    This is the deterministic, lexical re-scoring stage of the two-stage
    retrieval pipeline.  It is **not** a neural cross-encoder: it does not
    perform joint query–document attention.  For the neural path see
    ``retrieval/neural_rerank.py``.

    Usage::

        scorer = LexicalReScorer()
        ranked = scorer.rerank(query, hits, semantic_scores=scores)
    """

    def rerank(
        self,
        query: str,
        hits: list[RetrievalHit],
        *,
        semantic_scores: dict[str, float] | None = None,
    ) -> list[RetrievalHit]:
        """Re-score and reorder ``hits`` by field-weighted lexical relevance."""
        return rerank_hits(query, hits, semantic_scores=semantic_scores)

# Field weights: a query term landing in the title is a far stronger relevance signal than
# the same term buried in the body; the ref (which embeds the object id) is the weakest.
_W_TITLE = 3.0
_W_BODY = 1.0
_W_REF = 0.5

# Signal weights for the final score. Breadth and depth are the load-bearing signals;
# exactness and consensus only refine the ordering.
_COVERAGE_WEIGHT = 1.0
_DENSITY_WEIGHT = 1.0
_PHRASE_TITLE_BONUS = 0.5
_PHRASE_BODY_BONUS = 0.25
_PRIOR_WEIGHT = 0.25
_SEMANTIC_WEIGHT = 1.0


def rerank_hits(
    query: str,
    hits: list[RetrievalHit],
    *,
    semantic_scores: dict[str, float] | None = None,
) -> list[RetrievalHit]:
    """Re-score fused ``hits`` by field-weighted lexical relevance (highest first).

    This is a **lexical re-scoring** function, not a neural cross-encoder.  It uses
    term coverage, field-weighted density, exact-phrase bonuses, and (optionally) cosine
    similarity from a real embedding model.  For neural cross-encoder re-ranking see
    ``retrieval/neural_rerank.py`` (``NeuralReranker``).

    ``semantic_scores`` maps ref -> cosine(query, doc) from a real embedding model; when
    given, semantic similarity becomes a first-class signal so paraphrase hits are not
    demoted. When ``None`` (the hashing stub / tests) re-scoring is purely lexical and
    deterministic. Returns a new list; the input is not mutated. Each returned hit carries
    its relevance in ``score`` and ``source="reranked"``. A query with no usable terms and
    no semantic scores leaves the recall order untouched."""

    terms = [term.lower() for term in query_terms(query)]
    if (not terms and not semantic_scores) or len(hits) < 2:
        return hits

    max_raw = sum(_W_TITLE * len(term) for term in terms)
    priors = _normalised_priors(hits)
    phrase = _WS_RE.sub(" ", query.strip()).lower()

    scored: list[tuple[float, float, str, RetrievalHit]] = []
    for hit in hits:
        title = hit.title.lower()
        body = hit.body.lower()
        ref = hit.ref.lower()

        matched = 0
        raw = 0.0
        for term in terms:
            if term in title:
                weight = _W_TITLE
            elif term in body:
                weight = _W_BODY
            elif term in ref:
                weight = _W_REF
            else:
                continue
            matched += 1
            raw += weight * len(term)

        coverage = matched / len(terms) if terms else 0.0
        density = raw / max_raw if max_raw else 0.0
        exactness = _phrase_bonus(phrase, title, body)
        prior = priors[hit.ref]
        semantic = max(0.0, semantic_scores.get(hit.ref, 0.0)) if semantic_scores else 0.0

        relevance = (
            _COVERAGE_WEIGHT * coverage
            + _DENSITY_WEIGHT * density
            + exactness
            + _PRIOR_WEIGHT * prior
            + _SEMANTIC_WEIGHT * semantic
        )
        scored.append((relevance, hit.score, hit.ref, hit))

    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [
        hit.model_copy(update={"score": relevance, "source": "reranked"})
        for relevance, _, _, hit in scored
    ]


def _phrase_bonus(phrase: str, title: str, body: str) -> float:
    if not phrase:
        return 0.0
    if phrase in title:
        return _PHRASE_TITLE_BONUS
    if phrase in body:
        return _PHRASE_BODY_BONUS
    return 0.0


def _normalised_priors(hits: list[RetrievalHit]) -> dict[str, float]:
    """Min-max the fused recall scores into [0, 1]; all-equal scores carry no signal."""
    scores = [hit.score for hit in hits]
    low, high = min(scores), max(scores)
    span = high - low
    if span <= 0:
        return {hit.ref: 0.0 for hit in hits}
    return {hit.ref: (hit.score - low) / span for hit in hits}
