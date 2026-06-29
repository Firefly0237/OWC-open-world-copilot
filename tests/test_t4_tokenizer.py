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
            assert user_warnings, (
                "Expected a UserWarning when tiktoken is unavailable, got none"
            )
            assert any("tiktoken" in str(x.message) for x in user_warnings)
    finally:
        tok_mod._ENCODER = original_encoder
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# _trim_transcript uses token counts (T4-C integration)
# ---------------------------------------------------------------------------

def test_trim_transcript_uses_token_budget() -> None:
    """_trim_transcript respects token budget, not char budget."""
    from owcopilot.agent.react import _trim_transcript

    # Create turns where char÷4 and tiktoken diverge.
    # "你好世界" is 4 chars → char÷4 = 1 token; real tiktoken ≈ 4 tokens.
    # Use a budget that admits the turn under char÷4 but rejects under tiktoken.
    cjk_turn = "你好世界" * 20  # 80 CJK chars → ~80 tokens via tiktoken, ~20 via char÷4
    english_turn = "hello world"

    transcript = [cjk_turn, english_turn]

    # Budget=25: would admit cjk_turn under char÷4 (20 tokens) but reject under tiktoken (~80).
    trimmed, n_omitted = _trim_transcript(transcript, budget=25)
    # Under real tiktoken the CJK turn should be omitted (too large).
    assert n_omitted >= 1, (
        "With token budget=25 and CJK turn (~80 tokens), at least one turn should be omitted. "
        f"Got n_omitted={n_omitted}, trimmed={trimmed}"
    )
    # The smaller english turn should always fit.
    assert english_turn in trimmed, "English turn should survive trim"


def test_trim_transcript_none_budget_unchanged() -> None:
    """Budget=None → transcript unchanged, 0 omitted."""
    from owcopilot.agent.react import _trim_transcript

    turns = ["turn 1", "turn 2", "turn 3"]
    trimmed, n_omitted = _trim_transcript(turns, budget=None)
    assert trimmed == turns
    assert n_omitted == 0


def test_trim_transcript_always_keeps_one_turn() -> None:
    """Even with budget=1, keeps the most recent turn (never empty)."""
    from owcopilot.agent.react import _trim_transcript

    turns = ["a" * 1000, "b" * 1000, "most recent"]
    trimmed, _ = _trim_transcript(turns, budget=1)
    assert len(trimmed) >= 1
    assert "most recent" in trimmed


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
