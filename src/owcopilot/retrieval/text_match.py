"""Small lexical helpers for mixed English/CJK lore retrieval.

These power the deterministic ``$0`` offline / BM25-fallback path only. The real semantic path
(bge-m3's own tokenizer) handles the full Unicode range natively, so the coverage notes below
matter only when the optional ``[semantic]`` model is unavailable.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

# CJK segment matcher for the lexical fallback. Blocks covered (codepoint ranges):
#   U+3400..U+4DBF    CJK Extension A
#   U+4E00..U+9FFF    CJK Unified Ideographs (main block; most simplified/traditional usage)
#   U+F900..U+FAD9    CJK Compatibility Ideographs (legacy duplicates, but appear in names)
#   U+20000..U+2FFFF  CJK Extension B and beyond (supplementary plane; rare archaic/name chars)
# Text is NFKC-normalised first (see ``query_terms``), which folds fullwidth digits/letters
# (fullwidth 1/2 -> 12, fullwidth A -> A) into ASCII so ``_WORD_RE`` no longer drops them.
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufad9\U00020000-\U0002ffff]+")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_STOP_TERMS = {
    "a",
    "an",
    "and",
    "are",
    "for",
    "in",
    "is",
    "of",
    "the",
    "to",
    "what",
    "who",
    "哪个",
    "什么",
    "谁",
    "现在",
    "应该",
    "这个",
    "哪些",
}


def query_terms(text: str) -> list[str]:
    """Return stable terms for languages with and without whitespace.

    Input is NFKC-normalised so fullwidth forms (e.g. ``区域１２`` / ``Ａ``) fold to their ASCII
    equivalents and are tokenised instead of silently dropped by the fallback lexer.
    """

    text = unicodedata.normalize("NFKC", text)
    terms: set[str] = set()
    lowered = text.lower()
    for word in _WORD_RE.findall(lowered):
        if len(word) >= 2 and word not in _STOP_TERMS:
            terms.add(word)
    for segment in _CJK_RE.findall(text):
        if len(segment) >= 2 and segment not in _STOP_TERMS:
            terms.add(segment)
        for size in (2, 3, 4):
            if len(segment) < size:
                continue
            for index in range(0, len(segment) - size + 1):
                term = segment[index : index + size]
                if term not in _STOP_TERMS:
                    terms.add(term)
    return sorted(terms, key=lambda term: (-len(term), term))


def lexical_score(query: str, fields: Iterable[str]) -> float:
    haystack = unicodedata.normalize("NFKC", " ".join(fields)).lower()
    score = 0.0
    for term in query_terms(query):
        if term.lower() in haystack:
            score += float(len(term))
    return score


def mentions(query: str, candidates: Iterable[str]) -> bool:
    q = unicodedata.normalize("NFKC", query).lower()
    terms = query_terms(query)
    for candidate in candidates:
        normalized = unicodedata.normalize("NFKC", candidate.strip()).lower()
        if not normalized:
            continue
        if normalized in q:
            return True
        if any(term.lower() in normalized for term in terms):
            return True
    return False
