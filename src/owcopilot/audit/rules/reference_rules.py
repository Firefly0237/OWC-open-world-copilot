"""Reference integrity rules."""

from __future__ import annotations

from collections.abc import Iterable

from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity


class MissingEntityReferenceRule:
    code = "UNKNOWN_ENTITY_REF"
    severity = Severity.ERROR
    category = Category.REFERENCE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for target_ref, field_path, entity_id in _entity_references(ctx):
            if entity_id and not _object_exists(ctx, entity_id):
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=f"{field_path} references unknown entity '{entity_id}'",
                    evidence=[Evidence(kind="field_path", target_ref=target_ref, path=field_path)],
                )


class DeprecatedEntityReferenceRule:
    code = "DEPRECATED_ENTITY_REF"
    severity = Severity.WARNING
    category = Category.REFERENCE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for target_ref, field_path, entity_id in _entity_references(ctx):
            entity = ctx.bundle.entities.get(entity_id)
            if entity is not None and entity.status == "deprecated":
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=f"{field_path} references deprecated entity '{entity_id}'",
                    evidence=[Evidence(kind="field_path", target_ref=target_ref, path=field_path)],
                )


class MissingPrerequisiteRule:
    code = "PREREQ_MISSING"
    severity = Severity.ERROR
    category = Category.REFERENCE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for quest in ctx.bundle.quests.values():
            for prereq_id in quest.prerequisites:
                if prereq_id not in ctx.bundle.quests:
                    target_ref = f"quest:{quest.id}"
                    yield Issue(
                        rule_code=self.code,
                        severity=self.severity,
                        category=self.category,
                        target_ref=target_ref,
                        message=(
                            f"Quest '{quest.id}' references missing prerequisite "
                            f"'{prereq_id}'"
                        ),
                        evidence=[
                            Evidence(
                                kind="field_path",
                                target_ref=target_ref,
                                path=f"prerequisites.{prereq_id}",
                            )
                        ],
                    )


class MissingDialogueReferenceRule:
    code = "MISSING_DIALOGUE_REF"
    severity = Severity.ERROR
    category = Category.REFERENCE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for quest in ctx.bundle.quests.values():
            for dialogue_id in quest.dialogue_refs:
                if dialogue_id not in ctx.bundle.dialogues:
                    target_ref = f"quest:{quest.id}"
                    yield Issue(
                        rule_code=self.code,
                        severity=self.severity,
                        category=self.category,
                        target_ref=target_ref,
                        message=f"Quest '{quest.id}' references missing dialogue '{dialogue_id}'",
                        evidence=[
                            Evidence(
                                kind="field_path",
                                target_ref=target_ref,
                                path=f"dialogue_refs.{dialogue_id}",
                            )
                        ],
                    )


def _entity_references(ctx: AuditContext) -> Iterable[tuple[str, str, str]]:
    for quest in ctx.bundle.quests.values():
        target_ref = f"quest:{quest.id}"
        if quest.giver_npc:
            yield target_ref, "giver_npc", quest.giver_npc
        if quest.location:
            yield target_ref, "location", quest.location
        for index, stage in enumerate(quest.stages):
            if stage.location:
                yield target_ref, f"stages.{index}.location", stage.location
            for entity_id in stage.required_entities:
                yield target_ref, f"stages.{index}.required_entities", entity_id

    for poi in ctx.bundle.pois.values():
        if poi.controlling_faction:
            yield f"poi:{poi.id}", "controlling_faction", poi.controlling_faction

    for dialogue in ctx.bundle.dialogues.values():
        if dialogue.speaker_id:
            yield f"dialogue:{dialogue.id}", "speaker_id", dialogue.speaker_id

    for event_ref in ctx.bundle.quest_event_refs.values():
        if event_ref.event_id:
            yield f"quest_event_ref:{event_ref.id}", "event_id", event_ref.event_id


def _object_exists(ctx: AuditContext, object_id: str) -> bool:
    return (
        object_id in ctx.bundle.entities
        or object_id in ctx.bundle.pois
        or object_id in ctx.bundle.regions
        or object_id in ctx.bundle.quests
    )
