"""Small lexical helpers for mixed English/CJK lore retrieval."""

from __future__ import annotations

import re
from collections.abc import Iterable

_CJK_RE = re.compile(r"[\u3400-\u9fff]+")
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
    """Return stable terms for languages with and without whitespace."""

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
    haystack = " ".join(fields).lower()
    score = 0.0
    for term in query_terms(query):
        if term.lower() in haystack:
            score += float(len(term))
    return score


def mentions(query: str, candidates: Iterable[str]) -> bool:
    q = query.lower()
    terms = query_terms(query)
    for candidate in candidates:
        normalized = candidate.strip().lower()
        if not normalized:
            continue
        if normalized in q:
            return True
        if any(term.lower() in normalized for term in terms):
            return True
    return False
