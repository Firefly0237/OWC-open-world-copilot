"""Distill an unstructured manuscript (novel chapter, script, notes) into a content draft.

The pipeline is: chunk → per-chunk LLM extraction (JSON) → name-keyed merge → id mapping →
candidate ContentBundle + plot beats + gap list. The draft never touches the content store;
it is submitted to the review queue and lands only when a human accepts it.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from ..content.models import (
    ContentBundle,
    Entity,
    EntityType,
    Origin,
    Quest,
    QuestStage,
    Relation,
    ReviewStatus,
    Term,
)
from ..llm.gateway import LLMGateway
from .models import ExtractionDraft, ExtractionGap, PlotBeat

_KIND_PREFIX = {
    EntityType.NPC: "npc",
    EntityType.LOCATION: "loc",
    EntityType.FACTION: "fac",
    EntityType.ITEM: "item",
}

_SYSTEM_PROMPT = (
    "You are a narrative-design archivist for a game studio. Extract structured facts from "
    "ONE chunk of a manuscript (novel, script or design notes). Return ONE JSON object only, "
    "no markdown fences. Keys: characters, locations, factions, items, terms "
    "(each a list of {name, description, aliases?, traits?}), relations "
    "(list of {source, target, kind} using names), beats "
    "(list of {title, summary, location?, participants?} describing plot beats in order). "
    "Only record facts present in the chunk; leave description empty when the chunk gives "
    "none. Keep names exactly as written in the manuscript."
)

_FILL_SYSTEM_PROMPT = (
    "You complete missing fields for game-world content extracted from a manuscript. "
    'Return ONE JSON object: {"suggestion": "..."}. Write 1-2 sentences in the '
    "manuscript's language, consistent with the provided context, no new proper nouns."
)


class ExtractionService:
    def __init__(self, *, gateway: LLMGateway, bundle: ContentBundle) -> None:
        self.gateway = gateway
        self.bundle = bundle

    def extract(
        self,
        *,
        title: str,
        text: str,
        source_kind: str = "文稿",
        max_chunks: int = 12,
        progress: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> ExtractionDraft:
        clean = text.strip()
        if not clean:
            raise ValueError("manuscript text is empty")
        chunks = chunk_text(clean)[:max_chunks]
        merged = _MergedFacts()
        for index, chunk in enumerate(chunks):
            if progress is not None:
                progress("chunk", {"index": index + 1, "total": len(chunks)})
            raw = self.gateway.complete(
                task="extract_lore",
                system=_SYSTEM_PROMPT,
                user=f"[chunk {index + 1}/{len(chunks)}] 来源：{title}（{source_kind}）\n\n{chunk}",
            )
            merged.add(parse_extraction_payload(raw), chunk_order=index)
        draft_id = "extract_" + hashlib.sha256(f"{title}\n{clean}".encode()).hexdigest()[:12]
        return _draft_from_merged(
            merged,
            draft_id=draft_id,
            title=title,
            source_kind=source_kind,
            existing=self.bundle,
        )

    def fill_gaps(
        self,
        draft: ExtractionDraft,
        *,
        gap_refs: list[str] | None = None,
    ) -> ExtractionDraft:
        """Ask the model to suggest values for the selected gaps (all when refs is None).

        Suggestions land in `gap.suggestion`; nothing is applied until the user confirms
        via `apply_gap_answers` — the human stays in charge of every field.
        """
        wanted = set(gap_refs) if gap_refs is not None else None
        for gap in draft.gaps:
            if wanted is not None and gap.ref not in wanted:
                continue
            context = _gap_context(draft, gap)
            raw = self.gateway.complete(
                task="extract_fill",
                system=_FILL_SYSTEM_PROMPT,
                user=f"{context}\n\n问题：{gap.question}",
            )
            gap.suggestion = _parse_suggestion(raw)
        return draft


def apply_gap_answers(draft: ExtractionDraft, answers: dict[str, str]) -> ExtractionDraft:
    """Write confirmed answers into the draft bundle and drop the resolved gaps."""
    remaining: list[ExtractionGap] = []
    for gap in draft.gaps:
        answer = (answers.get(gap.ref) or "").strip()
        if not answer:
            remaining.append(gap)
            continue
        object_kind, object_id = gap.object_ref.split(":", 1)
        if object_kind == "entity" and object_id in draft.bundle.entities:
            entity = draft.bundle.entities[object_id]
            if gap.field == "description":
                draft.bundle.entities[object_id] = entity.model_copy(update={"description": answer})
        elif object_kind == "term" and object_id in draft.bundle.terms:
            term = draft.bundle.terms[object_id]
            draft.bundle.terms[object_id] = term.model_copy(update={"description": answer})
    draft.gaps = remaining
    return draft


def quests_from_beats(draft: ExtractionDraft) -> dict[str, Quest]:
    """Optional: turn plot beats into quest skeletons (the user opts in at submit time)."""
    quests: dict[str, Quest] = {}
    used: set[str] = set(draft.bundle.quests)
    for beat in draft.plot_beats:
        quest_id = _unique_id("quest", beat.title or f"beat_{beat.order}", used)
        quests[quest_id] = Quest(
            id=quest_id,
            title=beat.title or quest_id,
            objective=beat.summary or beat.title,
            location=beat.location,
            timeline_order=beat.order,
            stages=[
                QuestStage(
                    id=f"{quest_id}_stage_1",
                    summary=beat.summary or beat.title,
                    location=beat.location,
                )
            ],
            localization_keys=[f"quest.{quest_id}.objective"],
            metadata={"extraction_id": draft.id, "plot_beat": str(beat.order)},
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
        )
    return quests


def decode_document_bytes(data: bytes, filename: str) -> str:
    """Decode an uploaded manuscript: txt/md/json/csv plus .docx (stdlib-only)."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".docx":
        return _docx_text(data)
    text = _decode_text(data)
    if suffix == ".json":
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            return text
    return text


def parse_extraction_payload(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text[text.find("{") : text.rfind("}") + 1]
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("extraction provider returned non-object JSON")
    return payload


def chunk_text(text: str, *, max_chars: int = 3500, overlap_chars: int = 200) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs or [text.strip()]:
        if len(current) + len(paragraph) + 2 <= max_chars:
            current = f"{current}\n\n{paragraph}".strip()
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > max_chars:
            chunks.append(paragraph[:max_chars].strip())
            paragraph = paragraph[max(0, max_chars - overlap_chars) :].strip()
        current = paragraph
    if current:
        chunks.append(current)
    return chunks


# ------------------------------------------------------------------------- merge internals
class _MergedFacts:
    def __init__(self) -> None:
        self.by_kind: dict[EntityType, dict[str, dict[str, Any]]] = {
            EntityType.NPC: {},
            EntityType.LOCATION: {},
            EntityType.FACTION: {},
            EntityType.ITEM: {},
        }
        self.terms: dict[str, dict[str, Any]] = {}
        self.relations: list[dict[str, str]] = []
        self.beats: list[dict[str, Any]] = []

    def add(self, payload: dict[str, Any], *, chunk_order: int) -> None:
        kind_keys = {
            EntityType.NPC: "characters",
            EntityType.LOCATION: "locations",
            EntityType.FACTION: "factions",
            EntityType.ITEM: "items",
        }
        for kind, key in kind_keys.items():
            for item in _list(payload.get(key)):
                raw = _dict(item)
                name = str(raw.get("name") or "").strip()
                if not name:
                    continue
                slot = self.by_kind[kind].setdefault(
                    name, {"name": name, "description": "", "aliases": [], "traits": []}
                )
                description = str(raw.get("description") or "").strip()
                if len(description) > len(slot["description"]):
                    slot["description"] = description
                slot["aliases"] = _merge_lists(slot["aliases"], _list(raw.get("aliases")))
                slot["traits"] = _merge_lists(slot["traits"], _list(raw.get("traits")))
        for item in _list(payload.get("terms")):
            raw = _dict(item)
            name = str(raw.get("name") or raw.get("canonical") or "").strip()
            if not name:
                continue
            slot = self.terms.setdefault(name, {"name": name, "description": ""})
            description = str(raw.get("description") or "").strip()
            if len(description) > len(slot["description"]):
                slot["description"] = description
        for item in _list(payload.get("relations")):
            raw = _dict(item)
            source = str(raw.get("source") or "").strip()
            target = str(raw.get("target") or "").strip()
            rel_kind = str(raw.get("kind") or "").strip() or "相关"
            if source and target and source != target:
                self.relations.append({"source": source, "target": target, "kind": rel_kind})
        for item in _list(payload.get("beats")):
            raw = _dict(item)
            title = str(raw.get("title") or "").strip()
            if not title:
                continue
            self.beats.append(
                {
                    "order": chunk_order,
                    "title": title,
                    "summary": str(raw.get("summary") or "").strip(),
                    "location": str(raw.get("location") or "").strip() or None,
                    "participants": [str(p).strip() for p in _list(raw.get("participants"))],
                }
            )


def _draft_from_merged(
    merged: _MergedFacts,
    *,
    draft_id: str,
    title: str,
    source_kind: str,
    existing: ContentBundle,
) -> ExtractionDraft:
    bundle = ContentBundle()
    name_to_id: dict[str, str] = {}
    used: set[str] = set(existing.entities)
    meta = {"extraction_id": draft_id, "source_title": title, "source_kind": source_kind}

    for kind, facts in merged.by_kind.items():
        prefix = _KIND_PREFIX[kind]
        for name, slot in facts.items():
            entity_id = _unique_id(prefix, name, used)
            name_to_id[name] = entity_id
            bundle.entities[entity_id] = Entity(
                id=entity_id,
                name=name,
                type=kind,
                description=slot["description"],
                aliases=[str(a) for a in slot["aliases"]],
                tags=[str(t) for t in slot["traits"]],
                metadata=dict(meta),
                origin=Origin.AI_DRAFT,
                review_status=ReviewStatus.PENDING_REVIEW,
            )

    used_terms: set[str] = set(existing.terms)
    for name, slot in merged.terms.items():
        term_id = _unique_id("term", name, used_terms)
        bundle.terms[term_id] = Term(
            id=term_id,
            canonical=name,
            description=slot["description"],
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
        )

    unresolved: list[dict[str, str]] = []
    seen_relations: set[tuple[str, str, str]] = set()
    for relation in merged.relations:
        source_id = name_to_id.get(relation["source"])
        target_id = name_to_id.get(relation["target"])
        if source_id is None or target_id is None:
            unresolved.append(relation)
            continue
        key = (source_id, relation["kind"], target_id)
        if key in seen_relations:
            continue
        seen_relations.add(key)
        bundle.relations.append(
            Relation(
                source=source_id,
                target=target_id,
                kind=relation["kind"],
                metadata=dict(meta),
                origin=Origin.AI_DRAFT,
                review_status=ReviewStatus.PENDING_REVIEW,
            )
        )

    beats = [
        PlotBeat(
            order=index + 1,
            title=str(raw["title"]),
            summary=str(raw["summary"]),
            location=name_to_id.get(str(raw["location"])) if raw["location"] else None,
            participants=[name_to_id[name] for name in raw["participants"] if name in name_to_id],
        )
        for index, raw in enumerate(merged.beats)
    ]

    gaps: list[ExtractionGap] = []
    for entity in bundle.entities.values():
        if len(entity.description.strip()) < 8:
            gaps.append(
                ExtractionGap(
                    ref=f"entity:{entity.id}.description",
                    object_ref=f"entity:{entity.id}",
                    field="description",
                    question=f"用一两句话介绍「{entity.name}」（身份、动机或用途）。",
                )
            )
    for term in bundle.terms.values():
        if len(term.description.strip()) < 4:
            gaps.append(
                ExtractionGap(
                    ref=f"term:{term.id}.description",
                    object_ref=f"term:{term.id}",
                    field="description",
                    question=f"「{term.canonical}」在这个世界里指什么？",
                )
            )

    summary = (
        f"从《{title}》提炼：角色 {len(merged.by_kind[EntityType.NPC])}、"
        f"地点 {len(merged.by_kind[EntityType.LOCATION])}、"
        f"势力 {len(merged.by_kind[EntityType.FACTION])}、"
        f"关系 {len(bundle.relations)}、剧情节拍 {len(beats)}。"
    )
    return ExtractionDraft(
        id=draft_id,
        source_title=title,
        source_kind=source_kind,
        summary=summary,
        bundle=bundle,
        plot_beats=beats,
        gaps=gaps,
        unresolved_relations=unresolved,
        stats={
            "entities": len(bundle.entities),
            "relations": len(bundle.relations),
            "terms": len(bundle.terms),
            "beats": len(beats),
            "gaps": len(gaps),
        },
    )


def _gap_context(draft: ExtractionDraft, gap: ExtractionGap) -> str:
    object_kind, object_id = gap.object_ref.split(":", 1)
    lines = [f"来源：《{draft.source_title}》（{draft.source_kind}）", f"草案概要：{draft.summary}"]
    if object_kind == "entity" and object_id in draft.bundle.entities:
        entity = draft.bundle.entities[object_id]
        lines.append(f"目标对象：{entity.name}（{entity.type.value}）")
        related = [
            f"{relation.source} -{relation.kind}-> {relation.target}"
            for relation in draft.bundle.relations
            if object_id in (relation.source, relation.target)
        ]
        if related:
            lines.append("相关关系：" + "；".join(related[:6]))
    return "\n".join(lines)


def _parse_suggestion(raw: str) -> str:
    text = raw.strip()
    try:
        if text.startswith("```"):
            text = text[text.find("{") : text.rfind("}") + 1]
        payload = json.loads(text)
        if isinstance(payload, dict):
            return str(payload.get("suggestion") or "").strip()
    except json.JSONDecodeError:
        pass
    return text[:200]


def _docx_text(data: bytes) -> str:
    """Extract paragraph text from a .docx (a zip of XML) without extra dependencies."""
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as e:
        raise ValueError("not a valid .docx file") from e
    root = ElementTree.fromstring(xml)
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{ns}p"):
        runs = [node.text or "" for node in paragraph.iter(f"{ns}t")]
        text = "".join(runs).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _unique_id(prefix: str, raw: str, used: set[str]) -> str:
    stem = _slug(raw)
    base = stem if stem.startswith(f"{prefix}_") else f"{prefix}_{stem or 'item'}"
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def _slug(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9㐀-鿿]+", "_", text)
    return text.strip("_")


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _merge_lists(current: list[Any], incoming: list[Any]) -> list[Any]:
    merged = [str(item) for item in current]
    for item in incoming:
        text = str(item).strip()
        if text and text not in merged:
            merged.append(text)
    return merged
