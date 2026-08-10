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
            f"estimate_tokens({text!r}) = {actual}, but AutoTokenizer gives {expected}"
        )


def test_load_tokenizer_uses_only_the_provisioned_local_cache(monkeypatch) -> None:
    """Runtime token accounting must never fetch a model artifact implicitly."""
    import huggingface_hub
    import transformers

    import owcopilot.retrieval.budget as budget_mod

    sentinel = object()
    calls: dict[str, object] = {}
    snapshot_path = "X:/huggingface/bge-m3/snapshots/local"

    def fake_snapshot_download(**kwargs: object) -> str:
        calls["snapshot"] = kwargs
        return snapshot_path

    def fake_from_pretrained(model_name: str, **kwargs: object) -> object:
        calls["tokenizer"] = {"model_name": model_name, "kwargs": kwargs}
        return sentinel

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)
    monkeypatch.setattr(transformers.AutoTokenizer, "from_pretrained", fake_from_pretrained)
    budget_mod._load_tokenizer.cache_clear()
    try:
        assert budget_mod._load_tokenizer() is sentinel
        assert calls == {
            "snapshot": {
                "repo_id": "BAAI/bge-m3",
                "revision": "5617a9f61b028005a4858fdac845db406aefb181",
                "local_files_only": True,
                "max_workers": 1,
            },
            "tokenizer": {
                "model_name": snapshot_path,
                "kwargs": {"local_files_only": True},
            },
        }
    finally:
        budget_mod._load_tokenizer.cache_clear()


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
