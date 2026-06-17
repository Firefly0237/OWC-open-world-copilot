"""Global literal search across the canon — the fast 'find anything / jump to it' an authoring tool
needs alongside the semantic RAG. Deterministic scoring (exact > prefix > contains > body), so it is
golden-testable and reproducible.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..content.models import ContentBundle


class SearchHit(BaseModel):
    ref: str  # "<kind>:<id>", e.g. "entity:npc_mara"
    kind: str
    title: str
    snippet: str
    score: int


def _snippet(text: str, needle: str, *, width: int = 60) -> str:
    if not text:
        return ""
    lowered = text.lower()
    idx = lowered.find(needle)
    if idx < 0:
        return text[:width]
    start = max(0, idx - width // 3)
    return text[start : start + width]


def _score(name: str, body: str, needle: str) -> int:
    name_l, body_l = name.lower(), body.lower()
    if name_l == needle:
        return 100
    if name_l.startswith(needle):
        return 80
    if needle in name_l:
        return 60
    if needle in body_l:
        return 30
    return 0


def _candidates(bundle: ContentBundle) -> list[tuple[str, str, str, str]]:
    """(kind, id, name/title, body) for every searchable canon object."""
    rows: list[tuple[str, str, str, str]] = []
    for entity in bundle.entities.values():
        rows.append(("entity", entity.id, entity.name, entity.description))
    for quest in bundle.quests.values():
        rows.append(("quest", quest.id, quest.title, quest.objective))
    for region in bundle.regions.values():
        rows.append(("region", region.id, region.name, ""))
    for poi in bundle.pois.values():
        rows.append(("poi", poi.id, poi.name, ""))
    for term in bundle.terms.values():
        rows.append(("term", term.id, getattr(term, "name", ""), term.description))
    for dialogue in bundle.dialogues.values():
        rows.append(("dialogue", dialogue.id, dialogue.id, dialogue.text or ""))
    for tree in bundle.dialogue_trees.values():
        rows.append(("dialogue_tree", tree.id, tree.title or tree.id, ""))
    for text in bundle.localized_texts.values():
        rows.append(("localized_text", text.id, text.text_key, text.text))
    return rows


def search_all(
    bundle: ContentBundle,
    query: str,
    *,
    kinds: set[str] | None = None,
    limit: int = 30,
) -> list[SearchHit]:
    needle = query.strip().lower()
    if not needle:
        return []
    hits: list[SearchHit] = []
    for kind, obj_id, name, body in _candidates(bundle):
        if kinds is not None and kind not in kinds:
            continue
        score = _score(name or obj_id, body, needle)
        if score == 0 and needle in obj_id.lower():
            score = 20  # id-only match still jumps you there
        if score == 0:
            continue
        hits.append(
            SearchHit(
                ref=f"{kind}:{obj_id}",
                kind=kind,
                title=name or obj_id,
                snippet=_snippet(body or name, needle),
                score=score,
            )
        )
    hits.sort(key=lambda h: (-h.score, h.ref))  # deterministic: score desc, then ref asc
    return hits[:limit]
