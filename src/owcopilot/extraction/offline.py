"""Deterministic offline providers for manuscript extraction.

Not language models: cheap pattern extractors that exercise the full pipeline —
chunking, merging, id mapping, gap detection, review queue — at $0. Real runs swap in
`OpenAICompatProvider`; nothing else changes.
"""

from __future__ import annotations

import json
import re

from .service import EXTRACTION_GLEAN_MARKER

_EMPTY_PAYLOAD = json.dumps(
    {
        "characters": [],
        "locations": [],
        "factions": [],
        "items": [],
        "terms": [],
        "relations": [],
        "beats": [],
    },
    ensure_ascii=False,
)

_CHAR_PATTERNS = [
    re.compile(r"^(?:角色|人物)[:：]\s*([^\s，。;；]{1,12})", re.MULTILINE),
    re.compile(r"([一-鿿]{2,4})(?:说道|喊道|低声道|答道|说)"),
]
_LOC_PATTERNS = [
    re.compile(r"^地点[:：]\s*([^\s，。;；]{1,12})", re.MULTILINE),
    re.compile(r"(?:来到|抵达|前往|回到)([一-鿿]{2,6})"),
]
_FACTION_PATTERNS = [
    re.compile(r"^(?:势力|阵营)[:：]\s*([^\s，。;；]{1,12})", re.MULTILINE),
    re.compile(r"([一-鿿]{2,6}(?:会|教团|军团|商会|公会|王庭))"),
]
_TERM_PATTERN = re.compile(r"「([一-鿿]{2,8})」")
_PAIR_PATTERN = re.compile(r"([一-鿿]{2,4})(?:和|与)([一-鿿]{2,4})")


class OfflineExtractionProvider:
    """Extract a minimal structured payload from one manuscript chunk, deterministically."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        if EXTRACTION_GLEAN_MARKER in system:
            # A gleaning second pass: the deterministic extractor already returned everything it
            # can find on the first pass, so it has nothing to add. Real models find missed items.
            return _EMPTY_PAYLOAD, max(1, len(system) // 4), max(1, len(_EMPTY_PAYLOAD) // 4)
        body = re.sub(r"^\[chunk[^\n]*\n+", "", user.strip())
        characters = _collect(_CHAR_PATTERNS, body)
        locations = _collect(_LOC_PATTERNS, body)
        factions = _collect(_FACTION_PATTERNS, body)
        terms = _unique(_TERM_PATTERN.findall(body))
        relations = []
        for source, target in _PAIR_PATTERN.findall(body):
            source = _snap_to_known(source, characters)
            target = _snap_to_known(target, characters)
            if source == target:
                continue
            relations.append({"source": source, "target": target, "kind": "认识"})
            for name in (source, target):  # a pair endpoint is a character sighting too
                if name not in characters:
                    characters.append(name)
        first_sentence = re.split(r"[。！？\n]", body, maxsplit=1)[0]
        payload = {
            "characters": [
                {"name": name, "description": _describe(user, name)} for name in characters
            ],
            "locations": [{"name": name, "description": ""} for name in locations],
            "factions": [{"name": name, "description": ""} for name in factions],
            "items": [],
            "terms": [{"name": name, "description": ""} for name in terms],
            "relations": relations,
            "beats": [
                {
                    "title": first_sentence[:16] or "未命名节拍",
                    "summary": user.strip()[:80],
                    "location": locations[0] if locations else None,
                    "participants": characters[:3],
                }
            ],
        }
        text = json.dumps(payload, ensure_ascii=False)
        return text, max(1, (len(system) + len(user)) // 4), max(1, len(text) // 4)


class OfflineGapFillProvider:
    """Return a deterministic placeholder suggestion so the gap-fill loop is testable."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        question = user.strip().splitlines()[-1] if user.strip() else ""
        payload = {"suggestion": f"（离线补全）{question[:40]}"}
        text = json.dumps(payload, ensure_ascii=False)
        return text, max(1, (len(system) + len(user)) // 4), max(1, len(text) // 4)


def _collect(patterns: list[re.Pattern[str]], text: str) -> list[str]:
    names: list[str] = []
    for pattern in patterns:
        names.extend(pattern.findall(text))
    return _unique(names)


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            ordered.append(cleaned)
    return ordered


def _snap_to_known(name: str, known: list[str]) -> str:
    for candidate in sorted(known, key=len, reverse=True):
        if name.startswith(candidate) or candidate.startswith(name):
            return candidate
    return name


def _describe(text: str, name: str) -> str:
    for sentence in re.split(r"[。！？\n]", text):
        if name in sentence and len(sentence.strip()) > len(name):
            return sentence.strip()[:60]
    return ""
