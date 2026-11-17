"""CJK coverage of the lexical fallback tokenizer (``retrieval/text_match``).

These pin the extended Unicode coverage of the ``$0`` offline / BM25-fallback lexer: CJK Extension B
(supplementary plane), CJK Compatibility Ideographs, and fullwidth digits/letters (folded via NFKC).
The real bge-m3 path handles all of these natively; this only matters when the semantic model is
unavailable. Codepoints are written with ``chr()`` to keep this source file pure-ASCII (the dev box
console is GBK and chokes on literal supplementary-plane glyphs).
"""

from __future__ import annotations

from owcopilot.retrieval.text_match import lexical_score, mentions, query_terms

_EXT_B = chr(0x20000)  # CJK Extension B (supplementary plane) — previously dropped
_COMPAT = chr(0xFA0E)  # CJK Compatibility Ideograph — previously dropped
_FW_1, _FW_2 = chr(0xFF11), chr(0xFF12)  # fullwidth 1, 2
_FW_A, _FW_B = chr(0xFF21), chr(0xFF22)  # fullwidth A, B


def test_extension_b_character_is_tokenized_not_dropped() -> None:
    terms = query_terms(_EXT_B + "任务")
    assert _EXT_B in "".join(terms), "supplementary-plane CJK must survive the lexer"


def test_compatibility_ideograph_is_tokenized_not_dropped() -> None:
    terms = query_terms(_COMPAT + "据点")
    assert _COMPAT in "".join(terms), "CJK compatibility ideograph must survive the lexer"


def test_fullwidth_digits_fold_to_ascii_via_nfkc() -> None:
    terms = query_terms("区域" + _FW_1 + _FW_2)
    assert "12" in terms, "fullwidth digits must NFKC-fold to ASCII and be tokenized"


def test_fullwidth_letters_fold_to_ascii_and_lowercase() -> None:
    terms = query_terms(_FW_A + _FW_B + " zone")
    assert "ab" in terms, "fullwidth letters must NFKC-fold to ASCII then lowercase"


def test_lexical_score_matches_across_fullwidth_query_and_ascii_field() -> None:
    # A query written with fullwidth digits should still score against an ASCII-digit field.
    score = lexical_score("区域" + _FW_1 + _FW_2, ["区域 12 the zone"])
    assert score > 0


def test_mentions_normalizes_fullwidth_forms_on_both_sides() -> None:
    assert mentions("区域" + _FW_1 + _FW_2, ["区域 12"])
