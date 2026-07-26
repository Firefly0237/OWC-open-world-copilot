"""T4-C: True tiktoken tokenizer tests.

Acceptance criteria (from SUPERVISOR_rubric.md P4C-1):
1. count_tokens("你好世界 hello world") returns value in [6, 10] (true BPE)
2. count_tokens("你好世界") != len("你好世界") // 4   (i.e. not char÷4 = 1)
3. count_tokens("你好世界") returns approximately 4 (≠ 1, the char÷4 result)
4. Without tiktoken: count_tokens() falls back to len(text)//4 with a UserWarning
5. count_tokens is used in _trim_transcript (token-based, not char-based)
"""

from __future__ import annotations

import warnings

# ---------------------------------------------------------------------------
# Basic token counting with tiktoken
# ---------------------------------------------------------------------------


def test_count_tokens_cjk_english_mix() -> None:
    """T4-C-1: Mixed CJK+English returns a real BPE count in [6, 10]."""
    from owcopilot.llm.tokenizer import count_tokens

    result = count_tokens("你好世界 hello world")
    assert 6 <= result <= 10, f"Expected [6, 10], got {result}"


def test_count_tokens_cjk_only_not_char_div_4() -> None:
    """T4-C-2: CJK count is NOT the char÷4 result (which would be 1 for 4 chars)."""
    from owcopilot.llm.tokenizer import count_tokens

    text = "你好世界"
    char_div_4 = len(text) // 4  # = 1
    result = count_tokens(text)
    assert result != char_div_4, (
        f"count_tokens({text!r}) = {result}, same as len÷4 = {char_div_4}; "
        "expected a real BPE count (≈ 4)"
    )
    # CJK tokens in cl100k: typically 1 char ≈ 1 token for these characters
    assert result >= 2, f"Expected ≥2 tokens for '你好世界', got {result}"


def test_count_tokens_pure_english() -> None:
    """English text: BPE result should differ meaningfully from char÷4 in edge cases."""
    from owcopilot.llm.tokenizer import count_tokens

    # "hello world" = 2 tokens in cl100k (common words)
    result = count_tokens("hello world")
    assert result >= 1, f"Expected ≥1 token, got {result}"
    assert isinstance(result, int)


def test_count_tokens_empty_string() -> None:
    """Empty string → 0 tokens."""
    from owcopilot.llm.tokenizer import count_tokens

    assert count_tokens("") == 0


def test_count_tokens_json_structure() -> None:
    """JSON-formatted text (typical audit output) is counted, not erroring."""
    from owcopilot.llm.tokenizer import count_tokens

    text = '{"open_errors": 3, "entities": ["npc_aldric", "npc_missing"], "status": "dirty"}'
    result = count_tokens(text)
    assert result >= 5, f"Expected ≥5 tokens for JSON, got {result}"


def test_count_tokens_long_text() -> None:
    """Long text: token count is proportional (sanity check)."""
    from owcopilot.llm.tokenizer import count_tokens

    short = "hello world"
    long_text = short * 100
    result_short = count_tokens(short)
    result_long = count_tokens(long_text)
    # Long text should have substantially more tokens
    assert result_long > result_short * 10, (
        f"Long text ({result_long} tokens) should be >> short ({result_short} tokens)"
    )


# ---------------------------------------------------------------------------
# Fallback behaviour when tiktoken is not available
# ---------------------------------------------------------------------------


def test_count_tokens_fallback_no_tiktoken(monkeypatch) -> None:
    """T4-C-3: Without tiktoken, falls back to len//4 and emits UserWarning (not silent)."""
    import owcopilot.llm.tokenizer as tok_mod

    # Force fallback by replacing _ENCODER with sentinel and removing tiktoken from imports
    original_encoder = tok_mod._ENCODER
    tok_mod._ENCODER = None  # reset to uninitialized

    # Patch _load_encoder to simulate ImportError (monkeypatch auto-restores)
    def _fake_load() -> object:
        # Simulate tiktoken not available by setting sentinel directly
        tok_mod._ENCODER = tok_mod._FALLBACK_SENTINEL
        return tok_mod._FALLBACK_SENTINEL

    monkeypatch.setattr(tok_mod, "_load_encoder", _fake_load)

    try:
        text = "hello world"
        result = tok_mod.count_tokens(text)
        expected_fallback = max(1, len(text) // 4)
        assert result == expected_fallback, (
            f"Fallback should be len(text)//4={expected_fallback}, got {result}"
        )
    finally:
        tok_mod._ENCODER = original_encoder
        monkeypatch.undo()


def test_count_tokens_fallback_emits_warning_on_import_error(monkeypatch) -> None:
    """T4-C-3: The UserWarning is emitted (not silenced) when tiktoken unavailable."""
    import builtins

    import owcopilot.llm.tokenizer as tok_mod

    original_encoder = tok_mod._ENCODER
    tok_mod._ENCODER = None  # reset cache

    original_import = builtins.__import__

    def _mock_import(name, *args, **kwargs):
        if name == "tiktoken":
            raise ImportError("No module named 'tiktoken'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _mock_import)

    try:
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            tok_mod.count_tokens("test text")
            # The UserWarning must have been emitted
            user_warnings = [x for x in w if issubclass(x.category, UserWarning)]
            assert user_warnings, "Expected a UserWarning when tiktoken is unavailable, got none"
            assert any("tiktoken" in str(x.message) for x in user_warnings)
    finally:
        tok_mod._ENCODER = original_encoder
        monkeypatch.undo()


# _trim_transcript (the text path's safety-net hard trim) retired with the text ReAct loop:
# the token budget is now enforced by the read-time compression middleware, whose token-count
# behaviour is pinned in test_agent_middleware.py / test_t4_context_compressor.py.


# ---------------------------------------------------------------------------
# P3a: honest approximation annotation (non-OpenAI models / budget-not-billing)
# ---------------------------------------------------------------------------


def test_tokenizer_documents_approximation_and_non_billing() -> None:
    """The docstrings must honestly flag the count as an APPROXIMATION for non-OpenAI models and
    state it is for context-budget, never billing — the P3a honesty red line."""
    import owcopilot.llm.tokenizer as tok_mod
    from owcopilot.llm.tokenizer import count_tokens

    module_doc = (tok_mod.__doc__ or "").lower()
    fn_doc = (count_tokens.__doc__ or "").lower()
    blob = module_doc + "\n" + fn_doc

    assert "approxim" in blob, "must flag the count as an approximation"
    assert "deepseek" in blob, "must name the non-OpenAI model the count approximates"
    # budget, not billing
    assert "budget" in blob
    assert "billing" in blob or "billed" in blob or "not for billing" in blob
    # error is bounded
    assert "bound" in blob, "must state the approximation error is bounded"


# ---------------------------------------------------------------------------
# T5: token-budget truncation (truncate_to_token_budget)
# ---------------------------------------------------------------------------


def test_truncate_to_token_budget_same_limit_consistent() -> None:
    """T5: the same token cap yields the same kept-token budget regardless of language.

    A CJK-heavy string and an English string that each exceed N tokens both truncate to exactly N
    tokens — the budget is measured in tokens, so it is language-independent (unlike a char cap,
    which would keep ~4x more CJK content for the same char count)."""
    from owcopilot.llm.tokenizer import count_tokens, truncate_to_token_budget

    n = 50
    cjk = "在一个遥远古老的王国里住着一位勇敢的骑士和一条沉睡的巨龙。" * 20
    eng = "The quick brown fox jumps over the lazy dog by the riverbank. " * 20

    # Precondition (via production count_tokens): both genuinely exceed the budget.
    assert count_tokens(cjk) > n
    assert count_tokens(eng) > n

    cjk_head, cjk_kept = truncate_to_token_budget(cjk, n)
    eng_head, eng_kept = truncate_to_token_budget(eng, n)

    assert cjk_kept == n
    assert eng_kept == n
    # The heads are real prefixes cut to the budget, not the whole string.
    assert len(cjk_head) < len(cjk)
    assert len(eng_head) < len(eng)


def test_truncate_to_token_budget_within_budget_returns_text_unchanged() -> None:
    """Text already within the budget is returned unchanged with its true token count."""
    from owcopilot.llm.tokenizer import count_tokens, truncate_to_token_budget

    text = "hello world"
    tokens = count_tokens(text)
    head, kept = truncate_to_token_budget(text, tokens + 100)
    assert head == text
    assert kept == tokens


def test_truncate_to_token_budget_zero_or_empty() -> None:
    """Non-positive budget or empty text → ('', 0), never a crash."""
    from owcopilot.llm.tokenizer import truncate_to_token_budget

    assert truncate_to_token_budget("anything", 0) == ("", 0)
    assert truncate_to_token_budget("anything", -5) == ("", 0)
    assert truncate_to_token_budget("", 100) == ("", 0)


def test_truncate_to_token_budget_fallback_no_tiktoken(monkeypatch) -> None:
    """T5: with tiktoken forced unavailable, truncate falls back to a char-proportional head
    (len ≤ max_tokens*4) consistent with count_tokens' ÷4 fallback, and never crashes."""
    import owcopilot.llm.tokenizer as tok_mod
    from owcopilot.llm.tokenizer import reset_encoder_cache, truncate_to_token_budget

    reset_encoder_cache()
    monkeypatch.setattr(tok_mod, "_ENCODER", tok_mod._FALLBACK_SENTINEL)
    try:
        text = "hello world " * 100
        head, kept = truncate_to_token_budget(text, 10)
        assert len(head) <= 10 * 4
        assert kept == max(1, len(head) // 4)
        assert kept >= 1
    finally:
        reset_encoder_cache()


# ---------------------------------------------------------------------------
# C-7: split_by_token_budget (chunk_text's multi-piece hard-split primitive)
# ---------------------------------------------------------------------------


def test_split_by_token_budget_covers_all_tokens_without_overlap() -> None:
    """Non-overlapping windows: every piece is within budget, none is empty, and the pieces cover
    essentially the whole source with no chunk dropped.

    Byte-level BPE (cl100k_base) can hold a partial multi-byte CJK char in one token, so decoding
    a slice that cuts through such a token yields a single U+FFFD at that edge (documented on
    ``split_by_token_budget`` -- the same accepted tradeoff as ``truncate_to_token_budget``'s tail
    case, just possible at every internal boundary here); that costs at most ~1 char per boundary,
    so the reconstructed length must stay within a small, bounded slack of the original -- not
    byte-for-byte identical, but nowhere close to losing a whole chunk's worth of content."""
    from owcopilot.llm.tokenizer import count_tokens, split_by_token_budget

    text = "在一个遥远古老的王国里住着一位勇敢的骑士和一条沉睡的巨龙。" * 50
    total = count_tokens(text)
    pieces = split_by_token_budget(text, 40)

    assert len(pieces) >= 2
    assert all(count_tokens(p) <= 40 for p in pieces)
    assert all(p for p in pieces)
    assert abs(len("".join(pieces)) - len(text)) <= len(pieces)  # at most ~1 char/boundary
    assert abs(sum(count_tokens(p) for p in pieces) - total) <= len(pieces)


def test_split_by_token_budget_overlap_repeats_trailing_tokens() -> None:
    """overlap_tokens > 0 shrinks the per-step advance (max_tokens - overlap_tokens): covering the
    same source takes strictly more pieces than the non-overlapping split, and the combined
    material is larger (the overlap window is genuinely repeated, not just cut differently) --
    checked by length/count, not exact substring matching, since decoding a slice that happens to
    cut through a byte-level BPE token can shift a character at the seam (see
    split_by_token_budget's docstring)."""
    from owcopilot.llm.tokenizer import count_tokens, split_by_token_budget

    text = "在一个遥远古老的王国里住着一位勇敢的骑士和一条沉睡的巨龙。" * 50
    plain = split_by_token_budget(text, 40)
    overlapped = split_by_token_budget(text, 40, overlap_tokens=10)

    assert len(overlapped) > len(plain)
    assert all(count_tokens(p) <= 40 for p in overlapped)
    assert sum(len(p) for p in overlapped) > sum(len(p) for p in plain)


def test_split_by_token_budget_overlap_clamped_below_max_tokens() -> None:
    """An overlap >= max_tokens would never advance; it is silently clamped so the window always
    makes progress (never an infinite/degenerate step)."""
    from owcopilot.llm.tokenizer import count_tokens, split_by_token_budget

    text = "在一个遥远古老的王国里住着一位勇敢的骑士和一条沉睡的巨龙。" * 50
    pieces = split_by_token_budget(text, 40, overlap_tokens=999)
    assert len(pieces) >= 2
    assert all(count_tokens(p) <= 40 for p in pieces)


def test_split_by_token_budget_zero_or_empty() -> None:
    """Non-positive budget or empty text -> [], never a crash."""
    from owcopilot.llm.tokenizer import split_by_token_budget

    assert split_by_token_budget("anything", 0) == []
    assert split_by_token_budget("anything", -5) == []
    assert split_by_token_budget("", 100) == []


def test_split_by_token_budget_within_budget_single_piece() -> None:
    """Text already within the budget comes back as a single piece."""
    from owcopilot.llm.tokenizer import count_tokens, split_by_token_budget

    text = "hello world"
    pieces = split_by_token_budget(text, count_tokens(text) + 100)
    assert pieces == [text]


def test_split_by_token_budget_fallback_no_tiktoken(monkeypatch) -> None:
    """With tiktoken forced unavailable, falls back to char-proportional windows, never crashes."""
    import owcopilot.llm.tokenizer as tok_mod
    from owcopilot.llm.tokenizer import reset_encoder_cache, split_by_token_budget

    reset_encoder_cache()
    monkeypatch.setattr(tok_mod, "_ENCODER", tok_mod._FALLBACK_SENTINEL)
    try:
        text = "hello world " * 100
        pieces = split_by_token_budget(text, 10)
        assert pieces
        assert all(len(p) <= 10 * 4 for p in pieces)
        assert "".join(pieces).startswith("hello world")
    finally:
        reset_encoder_cache()


def test_split_by_token_budget_large_single_block_is_linear_time() -> None:
    """C-7 perf regression guard: splitting one huge block with no natural break must be a single
    encode + linear slicing, NOT truncate_to_token_budget called in a loop over the shrinking
    remainder (which would be quadratic and, at this size, far too slow for a unit test)."""
    import time

    from owcopilot.llm.tokenizer import split_by_token_budget

    text = "甲" * 2_000_000
    t0 = time.time()
    pieces = split_by_token_budget(text, 5000)
    elapsed = time.time() - t0

    assert len(pieces) >= 2
    assert elapsed < 5.0, f"expected linear-time split, took {elapsed:.2f}s"
