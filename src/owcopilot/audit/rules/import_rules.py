"""Import-time audit rules."""

from __future__ import annotations

from collections.abc import Mapping

from ...content.hash import content_hash
from ...content.models import ContentBundle
from ..models import Category, Evidence, Issue, Severity


def detect_import_conflicts(existing: ContentBundle, incoming: ContentBundle) -> list[Issue]:
    issues: list[Issue] = []
    for object_type, old, new in _object_maps(existing, incoming):
        for object_id, incoming_obj in new.items():
            existing_obj = old.get(object_id)
            if existing_obj is None:
                continue
            if content_hash(existing_obj) == content_hash(incoming_obj):
                continue
            issues.append(
                Issue(
                    rule_code="IMPORT_CONFLICT",
                    severity=Severity.ERROR,
                    category=Category.IMPORT,
                    target_ref=f"{object_type}:{object_id}",
                    message=(
                        f"Imported {object_type} '{object_id}' conflicts with existing content; "
                        "the existing file was not overwritten."
                    ),
                    evidence=[
                        Evidence(
                            kind="field_path",
                            target_ref=f"{object_type}:{object_id}",
                            path=object_id,
                        )
                    ],
                    fingerprint=content_hash(
                        {
                            "rule": "IMPORT_CONFLICT",
                            "object_type": object_type,
                            "object_id": object_id,
                        }
                    ),
                )
            )
    return issues


def _object_maps(
    existing: ContentBundle, incoming: ContentBundle
) -> list[tuple[str, Mapping[str, object], Mapping[str, object]]]:
    return [
        ("entity", existing.entities, incoming.entities),
        ("quest_event_ref", existing.quest_event_refs, incoming.quest_event_refs),
        ("quest", existing.quests, incoming.quests),
        ("region", existing.regions, incoming.regions),
        ("poi", existing.pois, incoming.pois),
        ("dialogue", existing.dialogues, incoming.dialogues),
        ("localized_text", existing.localized_texts, incoming.localized_texts),
        ("term", existing.terms, incoming.terms),
        ("style_guide", existing.style_guides, incoming.style_guides),
    ]
