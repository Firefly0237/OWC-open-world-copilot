"""Deterministic QA provider for offline tools and tests."""

from __future__ import annotations

import json
import re

from ..retrieval.text_match import lexical_score, query_terms


class OfflineQAProvider:
    """Return a simple extractive JSON answer from the provided context.

    This provider is intentionally deterministic and offline. It is not a language model, but it
    must still answer with evidence text instead of a placeholder sentence so CLI evals can test
    retrieval, citation verification and refusal behaviour meaningfully at $0.
    """

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        context = _context_rows(system)
        selected = _select_rows(user, context)
        refused = _should_refuse(user, selected)
        payload = {
            "answer": _answer_text([] if refused else selected),
            "citations": [] if refused else [{"ref": ref} for ref, _title, _body in selected],
            "confidence": 0.0 if refused else 0.75,
            "mentioned_entities": [],
            "unresolved_mentions": [user] if refused else [],
            "refused": refused,
        }
        text = json.dumps(payload)
        return text, max(1, (len(system) + len(user)) // 4), max(1, len(text) // 4)


def _context_rows(system: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for ref, title, body in re.findall(r"^- \[([a-z_]+:[^\]]+)\]\s*([^:]*):\s*(.*)$", system, re.M):
        rows.append((ref, title.strip(), body.strip()))
    return rows


def _select_rows(query: str, rows: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    if not rows:
        return []
    scored: list[tuple[int, tuple[str, str, str]]] = []
    for row in rows:
        score = lexical_score(query, row)
        scored.append((int(score), row))
    scored.sort(key=lambda item: (-item[0], item[1][0]))
    best = [row for score, row in scored if score > 0][:6]
    return _expand_referenced_rows(best, rows)[:10]


def _answer_text(rows: list[tuple[str, str, str]]) -> str:
    if not rows:
        return "No grounded lore answer is available for this question."
    facts = []
    for ref, title, body in rows:
        snippet = " ".join(part for part in [title, body] if part).strip()
        facts.append(f"{ref}: {snippet}")
    return "；".join(facts)


def _expand_referenced_rows(
    selected: list[tuple[str, str, str]], rows: list[tuple[str, str, str]]
) -> list[tuple[str, str, str]]:
    if not selected:
        return []
    by_ref = {row[0]: row for row in rows}
    expanded = list(selected)
    seen = {row[0] for row in expanded}
    while len(expanded) < 12:
        before = len(expanded)
        ids = set()
        for ref, _title, body in expanded:
            ids.add(ref.split(":", 1)[-1])
            ids.update(re.findall(r"\b[a-z][a-z0-9_]*_[a-z0-9_]+\b", body))
            ids.update(
                re.findall(r"\b(?:entity|poi|quest|region|term|dialogue):([A-Za-z0-9_]+)\b", body)
            )
        for object_id in sorted(ids):
            for prefix in (
                "entity",
                "poi",
                "quest",
                "region",
                "term",
                "dialogue",
                "localized_text",
            ):
                ref = f"{prefix}:{object_id}"
                if ref in by_ref and ref not in seen:
                    expanded.append(by_ref[ref])
                    seen.add(ref)
                    if len(expanded) >= 12:
                        break
            if len(expanded) >= 12:
                break
        if len(expanded) == before:
            break
    return expanded


def _should_refuse(query: str, rows: list[tuple[str, str, str]]) -> bool:
    if not rows:
        return True
    property_terms = _property_terms(query)
    if not property_terms:
        return False
    context_text = " ".join(" ".join(row) for row in rows)
    return not any(term in context_text for term in property_terms)


def _property_terms(query: str) -> list[str]:
    if "关系" in query:
        return []
    match = re.search(r"的(.+?)(?:是谁|是什么|是怎样|怎样|吗|\?|？)", query)
    if not match:
        return []
    raw = match.group(1)
    if any(term in raw for term in ("势力", "区域", "地点", "任务")):
        return []
    return [term for term in query_terms(raw) if len(term) >= 2]
