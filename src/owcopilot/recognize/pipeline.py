"""Turn a (possibly human-edited) ImportPlan into canon-bound content — without auto-landing.

Three pure steps the action layer composes with a project + review queue:

* ``recognize`` — dispatch to the right adapter (table / articy) and return the editable plan.
* ``diff_against_canon`` — mark each proposed entity new / changed / unchanged against canon,
  by a content fingerprint, so only real deltas get staged for review.
* ``plan_to_bundle`` — materialize proposals as content-model objects. Deterministic proposals come
  in as ``human`` (it's the team's own data, just imported) but **pending_review**; LLM-proposed
  relations come in as ``ai_draft`` + pending_review. Nothing reaches canon until a human approves.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..collab.models import etag_for
from ..content.models import (
    ContentBundle,
    Entity,
    EntityType,
    Origin,
    Relation,
    ReviewStatus,
    SourceRef,
)
from .articy import recognize_articy
from .engine_data import recognize_engine_data
from .ink import recognize_ink
from .llm_relations import RelationProposer, propose_relations_guarded
from .models import ImportPlan, ProposedEntity, ProposedRelation
from .table import recognize_table
from .yarn import recognize_yarn

SUPPORTED_FORMATS = ("table", "articy", "ink", "yarn", "ue", "unity")


def _dispatch(
    source_format: str,
    *,
    rows: Sequence[Mapping[str, Any]] | None,
    articy_data: Any,
    text: str | None,
    engine_data: Any,
    mapping: Any,
    source_file: str,
    canon_ids: Iterable[str],
) -> ImportPlan:
    if source_format == "table":
        if rows is None:
            raise ValueError("表格识别需要 rows")
        return recognize_table(rows, mapping=mapping, source_file=source_file, canon_ids=canon_ids)
    if source_format == "articy":
        if articy_data is None:
            raise ValueError("articy 识别需要解析后的 JSON")
        return recognize_articy(articy_data, source_file=source_file)
    if source_format == "ink":
        if text is None:
            raise ValueError("ink 识别需要脚本文本")
        return recognize_ink(text, source_file=source_file)
    if source_format == "yarn":
        if text is None:
            raise ValueError("Yarn 识别需要脚本文本")
        return recognize_yarn(text, source_file=source_file)
    if source_format in {"ue", "unity"}:
        if engine_data is None:
            raise ValueError("引擎数据识别需要解析后的 JSON")
        return recognize_engine_data(
            engine_data, dialect=source_format, source_file=source_file, canon_ids=canon_ids
        )
    raise ValueError(f"暂不支持的来源格式：{source_format}（支持：{', '.join(SUPPORTED_FORMATS)}）")


def _llm_source_text(plan: ImportPlan, override: str | None) -> str:
    """Text the LLM relates entities over: the raw script if given, else entity descriptions."""
    if override is not None:
        return override
    return "\n".join(
        f"{e.id} {e.name}：{e.description}" for e in plan.entities if e.description.strip()
    )


def recognize(
    source_format: str,
    *,
    rows: Sequence[Mapping[str, Any]] | None = None,
    articy_data: Any = None,
    text: str | None = None,
    engine_data: Any = None,
    mapping: Any = None,
    source_file: str = "",
    canon_ids: Iterable[str] = (),
    enable_llm: bool = False,
    llm_proposer: RelationProposer | None = None,
    llm_text: str | None = None,
    allowed_kinds: Sequence[str] | None = None,
) -> ImportPlan:
    """Dispatch to the format adapter; optionally add §8-guarded LLM relations (default off).

    LLM relations are only *proposed* — they pass the same closed-world / evidence / kind / conf
    guards as everything else, are marked ``method='llm'``, and carry evidence into review."""
    plan = _dispatch(
        source_format,
        rows=rows, articy_data=articy_data, text=text, engine_data=engine_data,
        mapping=mapping, source_file=source_file, canon_ids=canon_ids,
    )
    if enable_llm and llm_proposer is not None:
        known = [e.id for e in plan.entities]
        source = _llm_source_text(plan, llm_text)
        if known and source.strip():
            kept, dropped = propose_relations_guarded(
                source, known, proposer=llm_proposer,
                allowed_kinds=allowed_kinds, source_file=source_file,
            )
            plan.relations.extend(kept)
            if dropped:
                plan.warnings.append(f"LLM 提议经 §8 护栏丢弃 {len(dropped)} 条（详见日志）")
    return plan


def _entity_fingerprint(payload: Mapping[str, Any]) -> str:
    """Content-only fingerprint (ignores provenance) so a clean re-import reads as unchanged."""
    return etag_for(
        {
            "name": payload.get("name", ""),
            "type": payload.get("type", ""),
            "description": payload.get("description", ""),
            "metadata": payload.get("metadata", {}),
        }
    )


def _content_source_ref(ref: Any) -> SourceRef | None:
    """Map a recognize SourceRef (file + locator) onto the content model's SourceRef (path)."""
    return SourceRef(path=ref.file) if ref and ref.file else None


def _coerce_type(raw: str) -> tuple[EntityType, dict[str, Any]]:
    try:
        return EntityType(str(raw).lower()), {}
    except ValueError:
        return EntityType.CONCEPT, {"source_type": raw}  # keep the original label for the reviewer


def _to_entity(proposed: ProposedEntity) -> Entity:
    etype, extra = _coerce_type(proposed.type)
    metadata: dict[str, Any] = dict(proposed.fields)
    metadata.update(extra)
    if proposed.source_ref and proposed.source_ref.locator:
        metadata["import_locator"] = proposed.source_ref.locator
    if proposed.method == "llm":
        metadata["import_confidence"] = proposed.confidence
    return Entity(
        id=proposed.id,
        name=proposed.name,
        type=etype,
        description=proposed.description,
        metadata=metadata,
        origin=Origin.AI_DRAFT if proposed.method == "llm" else Origin.HUMAN,
        review_status=ReviewStatus.PENDING_REVIEW,
        source_ref=_content_source_ref(proposed.source_ref),
    )


def _to_relation(proposed: ProposedRelation) -> Relation:
    metadata: dict[str, Any] = {"import_method": proposed.method}
    if proposed.evidence:
        metadata["evidence"] = proposed.evidence
    if proposed.method == "llm":
        metadata["import_confidence"] = proposed.confidence
    return Relation(
        source=proposed.source,
        target=proposed.target,
        kind=proposed.kind,
        metadata=metadata,
        origin=Origin.AI_DRAFT if proposed.method == "llm" else Origin.HUMAN,
        review_status=ReviewStatus.PENDING_REVIEW,
        source_ref=_content_source_ref(proposed.source_ref),
    )


def diff_against_canon(plan: ImportPlan, bundle: ContentBundle) -> ImportPlan:
    """Return a copy of ``plan`` with new / changed / unchanged entity-id lists filled in."""
    new: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    for proposed in plan.entities:
        entity = _to_entity(proposed)
        payload = entity.model_dump(mode="json")
        fingerprint = _entity_fingerprint(payload)
        existing = bundle.entities.get(proposed.id)
        if existing is None:
            new.append(proposed.id)
        elif _entity_fingerprint(existing.model_dump(mode="json")) != fingerprint:
            changed.append(proposed.id)
        else:
            unchanged.append(proposed.id)
    return plan.model_copy(update={"new": new, "changed": changed, "unchanged": unchanged})


def plan_to_bundle(plan: ImportPlan, *, only_ids: Iterable[str] | None = None) -> ContentBundle:
    """Materialize proposals as a ContentBundle. ``only_ids`` restricts to e.g. just new+changed."""
    allow = {str(i) for i in only_ids} if only_ids is not None else None
    bundle = ContentBundle()
    for proposed in plan.entities:
        if allow is None or proposed.id in allow:
            bundle.add_entity(_to_entity(proposed))
    for relation in plan.relations:
        if allow is None or relation.source in allow:
            bundle.add_relation(_to_relation(relation))
    return bundle
