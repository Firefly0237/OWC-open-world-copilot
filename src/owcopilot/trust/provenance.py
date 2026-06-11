"""Provenance and trust summaries for v2 content bundles."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from pydantic import BaseModel, Field

from ..content.models import ContentBundle, Origin, ProvenanceMixin, ReviewStatus


class ProvenanceRecord(BaseModel):
    ref: str
    object_type: str
    origin: Origin
    review_status: ReviewStatus
    source_path: str | None = None


class ProvenanceSummary(BaseModel):
    total: int = 0
    by_origin: dict[str, int] = Field(default_factory=dict)
    by_review_status: dict[str, int] = Field(default_factory=dict)
    unreviewed_ai_refs: list[str] = Field(default_factory=list)


def iter_provenance_records(bundle: ContentBundle) -> Iterable[ProvenanceRecord]:
    for object_type, object_id, obj in _objects(bundle):
        source_path = obj.source_ref.path if obj.source_ref is not None else None
        yield ProvenanceRecord(
            ref=f"{object_type}:{object_id}",
            object_type=object_type,
            origin=obj.origin,
            review_status=obj.review_status,
            source_path=source_path,
        )


def summarize_provenance(bundle: ContentBundle) -> ProvenanceSummary:
    records = list(iter_provenance_records(bundle))
    return ProvenanceSummary(
        total=len(records),
        by_origin=dict(sorted(Counter(record.origin.value for record in records).items())),
        by_review_status=dict(
            sorted(Counter(record.review_status.value for record in records).items())
        ),
        unreviewed_ai_refs=unreviewed_ai_refs(bundle),
    )


def unreviewed_ai_refs(bundle: ContentBundle) -> list[str]:
    return [
        record.ref
        for record in iter_provenance_records(bundle)
        if record.origin is not Origin.HUMAN and record.review_status is not ReviewStatus.APPROVED
    ]


def _objects(bundle: ContentBundle) -> list[tuple[str, str, ProvenanceMixin]]:
    objects: list[tuple[str, str, ProvenanceMixin]] = []
    objects.extend(("entity", object_id, obj) for object_id, obj in bundle.entities.items())
    objects.extend(("quest", object_id, obj) for object_id, obj in bundle.quests.items())
    objects.extend(("region", object_id, obj) for object_id, obj in bundle.regions.items())
    objects.extend(("poi", object_id, obj) for object_id, obj in bundle.pois.items())
    objects.extend(("dialogue", object_id, obj) for object_id, obj in bundle.dialogues.items())
    objects.extend(
        ("dialogue_tree", object_id, obj) for object_id, obj in bundle.dialogue_trees.items()
    )
    objects.extend(
        ("localized_text", object_id, obj) for object_id, obj in bundle.localized_texts.items()
    )
    objects.extend(("term", object_id, obj) for object_id, obj in bundle.terms.items())
    objects.extend(
        ("style_guide", object_id, obj) for object_id, obj in bundle.style_guides.items()
    )
    objects.extend((("relation", str(index), obj) for index, obj in enumerate(bundle.relations)))
    objects.extend(
        ("quest_event_ref", object_id, obj) for object_id, obj in bundle.quest_event_refs.items()
    )
    return objects
