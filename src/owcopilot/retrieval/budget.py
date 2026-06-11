"""Context budget trimming."""

from __future__ import annotations

import re

from .models import RetrievalHit

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def estimate_tokens(text: str) -> int:
    return max(1, len(_TOKEN_RE.findall(text)))


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
