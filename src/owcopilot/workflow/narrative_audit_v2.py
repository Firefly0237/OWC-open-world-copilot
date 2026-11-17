"""One production audit contract for generated and deterministically recompiled content."""

from __future__ import annotations

from pathlib import Path

from ..audit.context import AuditContext
from ..content.models import ContentBundle, DialogueTree, Quest
from ..pipeline.project import ProjectContext
from .decision_contract import validate_choice_coverage
from .mechanic_ir_v2 import read_compilation_quest, validate_compiled_playability
from .models import TaskRecord
from .narrative_localization_v2 import source_localization_entries


def audit_narrative_bound(
    root: str | Path,
    task: TaskRecord,
    *,
    plan: dict,
    bound: dict,
    decisions: dict | None = None,
    choices: dict | None = None,
    require_distinct: bool = False,
) -> dict:
    quest = Quest.model_validate(bound["quest"])
    tree = DialogueTree.model_validate(bound["tree"])
    candidate = ContentBundle.model_validate(task.base_bundle).model_copy(deep=True)
    candidate.quests[quest.id] = quest
    candidate.dialogue_trees[tree.id] = tree
    for text in source_localization_entries(quest):
        candidate.localized_texts[text.id] = text
    context = ProjectContext.open(root, world_id=task.request.world_id, version=task.base_version)
    try:
        report = context.audit_runner.run(AuditContext.from_bundle(candidate))
    finally:
        context.close()
    coverage = None
    playability = None
    if decisions is not None and choices is not None:
        coverage = validate_choice_coverage(plan, quest, tree, decisions, choices)
        if quest.metadata.get("mechanic_compilation_v2"):
            read_compilation_quest(quest, plan)
            playability = validate_compiled_playability(
                plan, quest, decisions, tree, choices, require_distinct=require_distinct
            )
    return {
        "status": "checked",
        "issues": [row.model_dump(mode="json") for row in report.issues],
        "coverage": coverage,
        "playability": playability,
        "semantic_status": "withheld",
    }
