from __future__ import annotations

import warnings

import pytest

from owcopilot.retrieval.budget import _load_tokenizer, estimate_tokens, trim_hits_to_budget
from owcopilot.retrieval.models import RetrievalHit


def _hit(ref: str, body: str) -> RetrievalHit:
    return RetrievalHit(ref=ref, object_type="entity", title=ref, body=body, score=1.0, source="x")


def test_estimate_tokens_is_positive() -> None:
    """estimate_tokens must return a positive integer for any non-empty text."""
    assert estimate_tokens("hello world") >= 1
    assert estimate_tokens("你好") >= 1
    assert estimate_tokens("") == 1  # max(1, ...) floor


def test_estimate_tokens_bge_m3_accurate_when_available() -> None:
    """When the bge-m3 tokenizer is available, results must match AutoTokenizer directly.

    This test is skipped when transformers / bge-m3 is not cached (CI $0 path).
    诚实说明: bge-m3 tokenizes 'hello world' as 3 BPE tokens (not 2 words), which is the
    correct BPE count.  The old regex counted words (2), not tokens.
    """
    tok = _load_tokenizer()
    if tok is None:
        pytest.skip("bge-m3 tokenizer not available; skipping accurate-count test")

    text_en = "The Iron Legion controls the northern pass."
    text_cjk = "铁卫军团控制着北方山道。"
    text_mixed = "铁卫军团 Iron Ward Legion"

    for text in (text_en, text_cjk, text_mixed):
        expected = max(1, len(tok.encode(text, add_special_tokens=False)))
        actual = estimate_tokens(text)
        assert actual == expected, (
            f"estimate_tokens({text!r}) = {actual}, "
            f"but AutoTokenizer gives {expected}"
        )


def test_estimate_tokens_fallback_emits_warning_when_no_tokenizer(monkeypatch) -> None:
    """When the tokenizer is unavailable, a WARNING is emitted (not silent degradation)."""
    import owcopilot.retrieval.budget as budget_mod

    monkeypatch.setattr(budget_mod, "_FALLBACK_WARNED", False)
    monkeypatch.setattr(budget_mod._load_tokenizer, "cache_clear", lambda: None, raising=False)
    # Patch _load_tokenizer to return None to simulate missing transformers
    def patched_load():
        return None

    # bypass lru_cache
    monkeypatch.setattr(budget_mod, "_load_tokenizer", patched_load)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = budget_mod.estimate_tokens("hello world")

    assert result >= 1
    warning_msgs = [str(w.message) for w in caught]
    assert any("fallback" in m.lower() or "heuristic" in m.lower() for m in warning_msgs), (
        f"Expected a fallback warning but got: {warning_msgs}"
    )


def test_trim_hits_to_budget_keeps_first_hit_even_if_large() -> None:
    hits = [_hit("entity:a", "one two three four"), _hit("entity:b", "five")]

    trimmed = trim_hits_to_budget(hits, budget_tokens=2)

    assert [hit.ref for hit in trimmed] == ["entity:a"]
