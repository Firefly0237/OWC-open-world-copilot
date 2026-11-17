"""Deterministic recompilation of the original generated IR, never a model replay."""

from __future__ import annotations

from typing import Any

from ..content.hash import content_hash
from ..content.models import DialogueTree, Quest
from .mechanic_ir_v2 import (
    compile_mechanic_design,
    compile_mechanic_dialogue,
    materialize_quest_draft,
    read_compilation_quest,
    seal_compilation_quest,
)
from .models import EventPlan
from .narrative_localization_v2 import (
    DRAFT_COMPILER_VERSION,
    source_localization_entries,
)


def recompile_bound_localization(plan: EventPlan | dict, bound: dict) -> dict[str, Any]:
    original_quest = Quest.model_validate(bound["quest"])
    original_tree = DialogueTree.model_validate(bound["tree"])
    original = read_compilation_quest(original_quest, plan)
    draft = original.input_quest["metadata"]["quest_draft_v2"]["source"]
    new_draft = materialize_quest_draft(
        draft,
        quest_id=original_quest.id,
        provenance={"source_ref": original.input_quest.get("source_ref")},
    )
    compiled = compile_mechanic_design(
        original.raw_ir,
        plan,
        new_draft,
        require_distinct=original.manifest["require_distinct"],
    )
    dialogue_ir = original_tree.metadata["mechanic_ir_v2"]["source_ir"]
    dialogue = compile_mechanic_dialogue(
        dialogue_ir, compiled, plan, tree_id=original_tree.id, title=original_tree.title
    )
    quest = seal_compilation_quest(compiled)
    if quest.logic != original_quest.logic:
        raise ValueError("本地化重编不能改变真实游戏机制")
    before_quest = original_quest.model_dump(mode="json")
    after_quest = quest.model_dump(mode="json")
    for value in (before_quest, after_quest):
        value.pop("metadata", None)
        value.pop("localization_keys", None)
    if before_quest != after_quest:
        raise ValueError("本地化重编不能改变任务原文、来源或审阅状态")
    before = original_tree.model_dump(mode="json")
    after = dialogue.tree.model_dump(mode="json")
    for value in (before, after):
        value.pop("metadata", None)
        for node in value["nodes"].values():
            node.pop("text_key", None)
            for choice in node["choices"]:
                choice.pop("text_key", None)
    if before != after:
        raise ValueError("本地化重编不能改变对白原文、选择、跳转或效果")
    bound_value = {
        "quest": quest.model_dump(mode="json"),
        "tree": dialogue.tree.model_dump(mode="json"),
    }
    payload = {
        "mode": "stored_ir_recompilation_no_model_dispatch",
        "source_bound_hash": content_hash(bound),
        "source_plan_hash": content_hash(EventPlan.model_validate(plan)),
        "source_draft_hash": content_hash(draft),
        "source_mechanic_ir_hash": content_hash(original.raw_ir),
        "source_dialogue_ir_hash": content_hash(dialogue_ir),
        "quest_draft_compiler": DRAFT_COMPILER_VERSION,
        "dialogue_compiler": dialogue.manifest["dialogue_compiler_version"],
        "bound": bound_value,
        "bound_hash": content_hash(bound_value),
        "decisions": compiled.decisions.model_dump(mode="json") if compiled.decisions else None,
        "choices": dialogue.choices.model_dump(mode="json"),
        "playability": dialogue.playability,
        "localization_entries": [
            row.model_dump(mode="json") for row in source_localization_entries(quest, dialogue.tree)
        ],
        "invariants": {
            "quest_logic_unchanged": True,
            "quest_content_and_provenance_unchanged": True,
            "dialogue_content_and_flow_unchanged": True,
        },
    }
    return {**payload, "recompilation_hash": content_hash(payload)}
