"""Distill an unstructured manuscript (novel chapter, script, notes) into a content draft.

The pipeline is: chunk → per-chunk LLM extraction (JSON) → name-keyed merge → id mapping →
candidate ContentBundle + plot beats + gap list. The draft never touches the content store;
it is submitted to the review queue and lands only when a human accepts it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..content.documents import binary_document_text
from ..content.encoding import decode_bytes
from ..content.lang import LanguageProfile, detect_language, language_directive
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
from ..llm.jsonio import extract_json_object
from ..util import unique_id
from .faithfulness import check_deterministic, llm_unsupported
from .models import CoverageReport, ExtractionDraft, ExtractionGap, PlotBeat

# Coverage budget: the WHOLE document is always read, but in at most this many model calls.
# When the natural fine-grained chunking would exceed the budget, chunk size is enlarged (up
# to MAX_CHUNK_CHARS) so coverage stays 100% while cost stays bounded. Only a document larger
# than BUDGET * MAX_CHUNK_CHARS (~768K chars — longer than most single novels) is read
# partially, and the uncovered tail is then reported honestly. These are server-side constants
# on purpose: choosing a coverage/cost trade-off is the system's responsibility, not the user's.
_COVERAGE_BUDGET_CHUNKS = 48
_BASE_CHUNK_CHARS = 3500
_MAX_CHUNK_CHARS = 16000
_CHUNK_OVERLAP_CHARS = 200

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
    "(list of {source, target, kind, description?} using names, where description says HOW or "
    "WHY they are related), beats "
    "(list of {title, summary, location?, participants?} describing plot beats in order). "
    "List EVERY alternate name a character is called in the chunk under aliases, so the same "
    "person is not recorded twice. Only record facts present in the chunk; leave description "
    "empty when the chunk gives none. Keep names exactly as written in the manuscript."
)

_FILL_SYSTEM_PROMPT = (
    "You complete missing fields for game-world content extracted from a manuscript. "
    'Return ONE JSON object: {"suggestion": "..."}. Write 1-2 sentences in the '
    "manuscript's language, consistent with the provided context, no new proper nouns."
)

# Appended on a retry after a chunk's reply could not be parsed as JSON — the single most common
# real-model failure (a prose preamble or trailing sentence around the object).
_STRICT_JSON_RETRY = (
    "\n\nIMPORTANT: return ONLY the JSON object, with no preamble, explanation or markdown fences."
)

# Sentinel marking a gleaning (second) pass over a chunk, so the deterministic offline provider can
# recognise it and return nothing (it found everything on pass one); real models use it to add what
# they missed. This is GraphRAG's "gleaning" idea: one extra pass recovers entities a single pass
# overlooks, at the cost of one more model call per chunk.
EXTRACTION_GLEAN_MARKER = "[[GLEAN_PASS]]"
_GLEAN_INSTRUCTION = (
    "\n\nThis is a SECOND pass over the SAME chunk. The user lists the names already extracted. "
    "Re-read carefully and return ONLY entities/relations/terms/beats that were MISSED — the same "
    "JSON object shape and keys, empty lists when nothing was missed. Do not repeat known names. "
    + EXTRACTION_GLEAN_MARKER
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
        glean_rounds: int = 1,
        verify_faithfulness: bool = False,
        progress: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> ExtractionDraft:
        """Distill a manuscript of any length into a reviewable draft.

        The whole document is read — short notes at fine granularity, a full novel by
        coarsening chunk size within a bounded call budget (see :func:`plan_coverage`). The
        detected language drives a directive so the model answers in the source language and
        keeps proper nouns verbatim, even in a mixed-language manuscript. Nothing about input
        length or language is asked of the creator.

        ``glean_rounds`` adds up to N extra recovery passes per chunk (GraphRAG-style gleaning)
        that pick up entities a single pass overlooks; each pass is one more model call.
        """
        clean = text.strip()
        if not clean:
            raise ValueError("manuscript text is empty")
        chunks, coverage = plan_coverage(clean)
        directive = language_directive(detect_language(clean))
        system = f"{_SYSTEM_PROMPT}\n\n{directive}"
        merged = _MergedFacts()
        failed: list[int] = []
        for index, chunk in enumerate(chunks):
            if progress is not None:
                progress("chunk", {"index": index + 1, "total": len(chunks)})
            payload = self._extract_chunk(system, title, source_kind, chunk, index, len(chunks))
            if payload is None:
                # One unparseable chunk must not lose the other 47. Skip it, record it, and report
                # it honestly below — never crash the whole run, never drop it in silence.
                failed.append(index + 1)
                continue
            merged.add(payload, chunk_order=index)
            self._glean_chunk(
                system, title, source_kind, chunk, index, len(chunks), payload, merged, glean_rounds
            )
        if failed:
            _record_failed_chunks(coverage, failed)
        merged.resolve_aliases()
        draft_id = "extract_" + hashlib.sha256(f"{title}\n{clean}".encode()).hexdigest()[:12]
        draft = _draft_from_merged(
            merged,
            draft_id=draft_id,
            title=title,
            source_kind=source_kind,
            existing=self.bundle,
            source_text=clean,
            coverage=coverage,
        )
        # optional RAGAS-style tier: NLI-judge each structured relation claim against the source,
        # catching invented links the deterministic co-occurrence pass let through. Opt-in (one
        # extra model call); a parse failure never fabricates a flag.
        if verify_faithfulness:
            if progress is not None:
                progress("verify", {"claims": len(draft.bundle.relations)})
            extra = llm_unsupported(
                draft.bundle,
                clean,
                self.gateway,
                already_flagged={item.ref for item in draft.unsupported},
            )
            if extra:
                draft.unsupported.extend(extra)
                draft.stats["unsupported"] = len(draft.unsupported)
        return draft

    def _extract_chunk(
        self, system: str, title: str, source_kind: str, chunk: str, index: int, total: int
    ) -> dict[str, Any] | None:
        """Extract one chunk, retrying once with a stricter JSON directive before giving up.

        Returns ``None`` when the model's reply still has no usable JSON after the retry, so the
        caller can skip-and-report rather than crash the whole manuscript run."""
        user = f"[chunk {index + 1}/{total}] 来源：{title}（{source_kind}）\n\n{chunk}"
        for attempt_system in (system, system + _STRICT_JSON_RETRY):
            raw = self.gateway.complete(task="extract_lore", system=attempt_system, user=user)
            try:
                return parse_extraction_payload(raw)
            except ValueError:
                continue
        return None

    def _glean_chunk(
        self,
        system: str,
        title: str,
        source_kind: str,
        chunk: str,
        index: int,
        total: int,
        first_pass: dict[str, Any],
        merged: _MergedFacts,
        rounds: int,
    ) -> None:
        """Run up to ``rounds`` recovery passes, merging only genuinely new findings.

        Stops early once a pass surfaces no new names (diminishing returns). Best-effort: a glean
        pass that fails to parse is simply skipped — the first pass already landed."""
        glean_system = system + _GLEAN_INSTRUCTION
        seen = _payload_names(first_pass)
        for _ in range(max(0, rounds)):
            known = "、".join(sorted(seen)) or "（无）"
            user = (
                f"[chunk {index + 1}/{total}] 来源：{title}（{source_kind}）\n"
                f"已提取：{known}\n\n{chunk}"
            )
            raw = self.gateway.complete(task="extract_lore", system=glean_system, user=user)
            try:
                extra = parse_extraction_payload(raw)
            except ValueError:
                return
            new_names = _payload_names(extra) - seen
            if not new_names:
                return
            merged.add(extra, chunk_order=index)
            seen |= new_names

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
    """Decode an uploaded manuscript: txt/md/json/csv plus .docx/.pdf/.epub."""
    binary = binary_document_text(data, filename)
    if binary is not None:
        return binary
    text = decode_bytes(data)
    if Path(filename).suffix.lower() == ".json":
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            return text
    return text


def parse_extraction_payload(raw: str) -> dict[str, Any]:
    """Parse one chunk's extraction JSON, tolerating prose and markdown fences.

    Real models prepend "Here is the extraction:" or append a trailing sentence; a strict
    ``json.loads`` crashes on those, and a crash here would abort the whole multi-chunk run.
    ``extract_json_object`` pulls the first balanced object out instead, raising ``ValueError``
    only when there is genuinely no JSON — which the caller handles per chunk."""
    return extract_json_object(raw)


def plan_coverage(
    text: str,
    *,
    budget_chunks: int = _COVERAGE_BUDGET_CHUNKS,
    base_chunk_chars: int = _BASE_CHUNK_CHARS,
    max_chunk_chars: int = _MAX_CHUNK_CHARS,
    overlap_chars: int = _CHUNK_OVERLAP_CHARS,
) -> tuple[list[str], CoverageReport]:
    """Plan how to read ``text`` completely within a bounded number of model calls.

    Strategy, in order:
      1. Chunk at fine granularity (``base_chunk_chars``). If that fits the call budget, done —
         coverage is "full".
      2. Otherwise enlarge chunk size just enough to fit the whole document in ``budget_chunks``
         chunks (capped at ``max_chunk_chars``) — coverage is still 100%, granularity "coarsened".
      3. Only if the document is larger than ``budget_chunks * max_chunk_chars`` do we read the
         head and report the rest as uncovered — granularity "partial", never silent.

    ``chunk_text`` packs every paragraph into some chunk, so a non-truncated plan genuinely
    covers the whole source; the report states which case applies and in what language.
    """
    clean = text.strip()
    total = len(clean)
    profile = detect_language(clean)
    chunks = chunk_text(clean, max_chars=base_chunk_chars, overlap_chars=overlap_chars)
    chunk_chars = base_chunk_chars
    granularity = "full"

    if len(chunks) > budget_chunks:
        # Enlarge chunk size until the whole document fits in `budget_chunks` chunks. Paragraph
        # packing leaves slack (a chunk stops before exceeding its cap), so a single proportional
        # estimate can fall short — grow iteratively until it fits or we hit max_chunk_chars.
        granularity = "coarsened"
        while len(chunks) > budget_chunks and chunk_chars < max_chunk_chars:
            scale = len(chunks) / budget_chunks
            chunk_chars = min(
                max_chunk_chars, max(chunk_chars + 500, math.ceil(chunk_chars * scale))
            )
            chunks = chunk_text(clean, max_chars=chunk_chars, overlap_chars=overlap_chars)

    if len(chunks) > budget_chunks:
        # The document exceeds the whole budget. Read the head and be honest about the tail.
        kept = chunks[:budget_chunks]
        unique = sum(len(c) for c in kept) - overlap_chars * max(0, len(kept) - 1)
        covered = max(0, min(total, unique))
        report = CoverageReport(
            total_chars=total,
            covered_chars=covered,
            chunk_count=len(kept),
            chunk_chars=chunk_chars,
            granularity="partial",
            language=profile.label,
            languages=profile.labels,
            mixed=profile.mixed,
            note=_coverage_note("partial", total, covered, len(kept), profile),
        )
        return kept, report

    report = CoverageReport(
        total_chars=total,
        covered_chars=total,
        chunk_count=len(chunks),
        chunk_chars=chunk_chars,
        granularity=granularity,
        language=profile.label,
        languages=profile.labels,
        mixed=profile.mixed,
        note=_coverage_note(granularity, total, total, len(chunks), profile),
    )
    return chunks, report


def _payload_names(payload: dict[str, Any]) -> set[str]:
    """Distinct entity/term names in one extraction payload (for gleaning dedup)."""
    names: set[str] = set()
    for key in ("characters", "locations", "factions", "items", "terms"):
        for item in _list(payload.get(key)):
            name = str(_dict(item).get("name") or _dict(item).get("canonical") or "").strip()
            if name:
                names.add(name)
    return names


def _record_failed_chunks(coverage: CoverageReport, failed: list[int]) -> None:
    """Note chunks whose reply could not be parsed even after a retry — surfaced, not hidden."""
    coverage.failed_chunks = failed
    coverage.note = (
        f"{coverage.note} 其中 {len(failed)} 个分块的模型返回无法解析、已跳过"
        f"（第 {'、'.join(str(i) for i in failed)} 块），可重试提炼这些段落。"
    )


def _coverage_note(
    granularity: str, total: int, covered: int, chunks: int, profile: LanguageProfile
) -> str:
    lang = (
        f"识别为{profile.label}（多语言混排，专有名词保留原文）"
        if profile.mixed
        else f"识别为{profile.label}"
    )
    if granularity == "partial":
        pct = round(100 * covered / total) if total else 0
        return (
            f"文档较长，本次完整读取了前约 {covered:,} 字（约 {pct}%，共 {chunks} 块），"
            f"其余部分未在本次提炼——可对剩余章节另行提炼。{lang}。"
        )
    if granularity == "coarsened":
        return f"长文已整篇覆盖，自动放大分块粒度以控制成本（{total:,} 字 / {chunks} 块）。{lang}。"
    return f"全文已整篇覆盖（{total:,} 字 / {chunks} 块）。{lang}。"


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
                self.relations.append(
                    {
                        "source": source,
                        "target": target,
                        "kind": rel_kind,
                        "description": str(raw.get("description") or "").strip(),
                    }
                )
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

    def resolve_aliases(self) -> None:
        """Merge entities the model flagged as the same person via aliases (李白 ≡ 李太白).

        Without this, "李白" and "李太白" become two entities and a human has to spot and merge
        them by hand — pushing a check onto the reviewer that the machine can do. We only merge
        when the model itself asserted the alias link (one slot's alias exactly equals another
        slot's name, same kind), so this stays deterministic and conservative, never a fuzzy guess.
        Relation and beat name references are remapped onto the surviving canonical name."""
        name_map: dict[str, str] = {}
        for slots in self.by_kind.values():
            name_map.update(_merge_aliased_slots(slots))
        for relation in self.relations:
            relation["source"] = name_map.get(relation["source"], relation["source"])
            relation["target"] = name_map.get(relation["target"], relation["target"])
        self.relations = [r for r in self.relations if r["source"] != r["target"]]
        for beat in self.beats:
            if beat["location"]:
                beat["location"] = name_map.get(beat["location"], beat["location"])
            beat["participants"] = [name_map.get(p, p) for p in beat["participants"]]


def _draft_from_merged(
    merged: _MergedFacts,
    *,
    draft_id: str,
    title: str,
    source_kind: str,
    existing: ContentBundle,
    source_text: str = "",
    coverage: CoverageReport | None = None,
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
            # Aliases resolve to the same entity, so a relation naming an alias still wires up.
            for alias in slot["aliases"]:
                name_to_id.setdefault(str(alias), entity_id)
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
    relation_by_key: dict[tuple[str, str, str], Relation] = {}
    for relation in merged.relations:
        source_id = name_to_id.get(relation["source"])
        target_id = name_to_id.get(relation["target"])
        if source_id is None or target_id is None:
            unresolved.append(relation)
            continue
        key = (source_id, relation["kind"], target_id)
        description = relation.get("description", "")
        existing_rel = relation_by_key.get(key)
        if existing_rel is not None:
            # Same edge seen again: keep the more informative "how/why" description.
            if len(description) > len(existing_rel.metadata.get("description", "")):
                existing_rel.metadata["description"] = description
            continue
        rel_meta = dict(meta)
        if description:
            rel_meta["description"] = description
        new_rel = Relation(
            source=source_id,
            target=target_id,
            kind=relation["kind"],
            metadata=rel_meta,
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
        )
        relation_by_key[key] = new_rel
        bundle.relations.append(new_rel)

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

    unsupported = check_deterministic(bundle, source_text)

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
        unsupported=unsupported,
        coverage=coverage,
        stats={
            "entities": len(bundle.entities),
            "relations": len(bundle.relations),
            "terms": len(bundle.terms),
            "beats": len(beats),
            "gaps": len(gaps),
            "unsupported": len(unsupported),
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


def _unique_id(prefix: str, raw: str, used: set[str]) -> str:
    return unique_id(prefix, raw, used)


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


def _merge_aliased_slots(slots: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Union slots linked by an asserted alias; merge each group into one canonical slot.

    Returns a map of merged-away name -> surviving canonical name. Mutates ``slots`` in place."""
    names = list(slots)
    name_set = set(names)
    parent = {name: name for name in names}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    for name in names:
        for alias in slots[name]["aliases"]:
            alias_name = str(alias).strip()
            if alias_name in name_set and alias_name != name:
                parent[find(alias_name)] = find(name)

    groups: dict[str, list[str]] = {}
    for name in names:
        groups.setdefault(find(name), []).append(name)

    name_map: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        # Canonical = the most informative slot (longest description), then shortest/earliest name.
        canonical = sorted(members, key=lambda n: (-len(slots[n]["description"]), len(n), n))[0]
        survivor = slots[canonical]
        for member in members:
            if member == canonical:
                continue
            other = slots.pop(member)
            if len(other["description"]) > len(survivor["description"]):
                survivor["description"] = other["description"]
            survivor["aliases"] = _merge_lists(survivor["aliases"], [*other["aliases"], member])
            survivor["traits"] = _merge_lists(survivor["traits"], other["traits"])
            name_map[member] = canonical
        survivor["aliases"] = [a for a in survivor["aliases"] if a != canonical]
    return name_map
