# canon 全局字面搜索：作者工具的『找任何东西/跳过去』快查。
"""Global literal search across the canon — the fast 'find anything / jump to it' an authoring tool
needs alongside the semantic RAG. Deterministic scoring (exact > prefix > contains > body), so it is
golden-testable and reproducible.
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from .content.models import ContentBundle


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
    for relation in bundle.relations:
        relation_id = f"{relation.source}:{relation.kind}:{relation.target}"
        title = f"{relation.source} —{relation.kind}→ {relation.target}"
        body = json.dumps(relation.metadata, ensure_ascii=False, sort_keys=True)
        rows.append(("relation", relation_id, title, body))
    for event_ref in bundle.quest_event_refs.values():
        body = " ".join(
            [event_ref.quest_id, event_ref.event_id, event_ref.ref_kind.value, event_ref.note]
        )
        rows.append(("quest_event_ref", event_ref.id, event_ref.id, body))
    for guide in bundle.style_guides.values():
        rows.append(("style_guide", guide.id, guide.id, f"{guide.body} {' '.join(guide.rules)}"))
    for rubric in bundle.style_rubrics.values():
        element_text = " ".join(
            " ".join([element.text, *element.terms, *element.patterns, element.note])
            for element in rubric.elements
        )
        body = " ".join(
            [rubric.abstract_tag, rubric.version, rubric.author, element_text]
        )
        rows.append(("style_rubric", rubric.id, rubric.label or rubric.id, body))
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
