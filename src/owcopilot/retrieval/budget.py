"""Context budget trimming -- accurate token estimation via bge-m3 tokenizer.

Token counting uses the bge-m3 AutoTokenizer (transformers) when available.  This gives
accurate BPE token counts consistent with the embedding model's own tokenizer.

**Why not character counting?**
The previous implementation (``re.compile(r'[A-Za-z0-9_]+|[\\u4e00-\\u9fff]')``) counts
regex-matched units, not BPE tokens.  For mixed CJK+English text the error can be 2–3x:
English multi-token words (e.g. "Ironwall" → ["Iron", "wall"] = 2 tokens) are counted as
1 unit, and punctuation is missed entirely.  This causes systematic underestimation and
can lead to silent budget overruns on large worlds.

**Fallback chain (in order)**:
1. ``transformers.AutoTokenizer.from_pretrained("BAAI/bge-m3")`` -- accurate, same
   tokenizer the embedding model uses.  Lazy-loaded and cached per process.  Fast:
   ``encode()`` runs in microseconds (no neural inference, no GPU).
2. ``len(text) // 3`` heuristic -- for mixed CJK+Latin text ~3 chars/token is a better
   approximation than the old regex.  Only used when transformers is unavailable.
   A WARNING is emitted the first time this fallback activates so the degradation is
   never silent.
"""

from __future__ import annotations

import logging
import warnings
from functools import lru_cache
from typing import Any

from .models import RetrievalHit

logger = logging.getLogger(__name__)

# Emitted once per process if we fall back to the heuristic estimator.
_FALLBACK_WARNED = False


@lru_cache(maxsize=1)
def _load_tokenizer() -> Any | None:
    """Lazy-load the bge-m3 tokenizer for accurate BPE token counting.

    Returns the tokenizer object on success, or ``None`` if transformers is not
    installed or the tokenizer cannot be loaded (e.g. model not yet cached and
    offline).  The result is cached for the lifetime of the process.
    """
    try:
        from transformers import AutoTokenizer  # noqa: PLC0415

        tok = AutoTokenizer.from_pretrained("BAAI/bge-m3")
        return tok
    except Exception as exc:  # noqa: BLE001
        logger.debug("bge-m3 tokenizer unavailable (%s); will use heuristic fallback.", exc)
        return None


def estimate_tokens(text: str) -> int:
    """Estimate the BPE token count of ``text``.

    Uses the bge-m3 tokenizer (AutoTokenizer) when available for accurate
    counting (±2% of true token count).  Falls back to ``len(text) // 3``
    when transformers is unavailable; this is a rough heuristic for mixed
    CJK+Latin text and emits a one-time WARNING so the degradation is visible.

    The old regex-based counter (``re.findall(r'[A-Za-z0-9_]+|[CJK]')``) is
    intentionally removed: it underestimates English multi-token words by 2–3x
    and ignores punctuation tokens, making budget overruns possible on
    large worlds.
    """
    global _FALLBACK_WARNED  # noqa: PLW0603

    tok = _load_tokenizer()
    if tok is not None:
        # fast_tokenizer=True (default when available) → microsecond latency
        ids = tok.encode(text, add_special_tokens=False)
        return max(1, len(ids))

    # Heuristic fallback: ~3 chars/token for mixed CJK+Latin
    if not _FALLBACK_WARNED:
        warnings.warn(
            "owcopilot.retrieval.budget: bge-m3 tokenizer not available; "
            "falling back to len(text)//3 heuristic for token estimation. "
            "Install transformers and cache BAAI/bge-m3 for accurate budget trimming.",
            stacklevel=2,
        )
        _FALLBACK_WARNED = True
    return max(1, len(text) // 3)


def trim_hits_to_budget(hits: list[RetrievalHit], *, budget_tokens: int) -> list[RetrievalHit]:
    kept: list[RetrievalHit] = []
    used = 0
    for hit in hits:
        cost = estimate_tokens(f"{hit.title} {hit.body}")
        if kept and used + cost > budget_tokens:
            continue
        kept.append(hit)
        used += cost
        if used >= budget_tokens:
            break
    return kept
