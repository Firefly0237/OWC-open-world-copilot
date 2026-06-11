"""Dialogue tree + flavor batch generation, audit rules and review landing."""

from __future__ import annotations

from pathlib import Path

from owcopilot.app.actions import (
    decide_review_action,
    run_dialogue_tree_action,
    run_flavor_action,
)
from owcopilot.assist.dialogue_trees import (
    DialogueTreeService,
    OfflineDialogueTreeProvider,
    tree_structure_problems,
)
from owcopilot.assist.flavor import FlavorBatchService, OfflineFlavorProvider
from owcopilot.audit.context import AuditContext
from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.runner import AuditRunner
from owcopilot.content.models import (
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Entity,
    EntityType,
)
from owcopilot.content.store import ContentStore
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter


def _bundle_with_npcs() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_mara": Entity(
                id="npc_mara", name="玛拉", type=EntityType.NPC, description="边境斥候。"
            ),
            "npc_doran": Entity(
                id="npc_doran", name="多兰", type=EntityType.NPC, description="老练商人。"
            ),
        }
    )


def _gateway(provider, task: str) -> LLMGateway:
    return LLMGateway(providers={"cheap": provider}, router=StaticRouter(mapping={task: "cheap"}))


def _write_project(root: Path) -> None:
    ContentStore(root).save(_bundle_with_npcs())


def test_dialogue_tree_offline_generation_is_structurally_sound() -> None:
    bundle = _bundle_with_npcs()
    service = DialogueTreeService(
        gateway=_gateway(OfflineDialogueTreeProvider(), "dialogue_tree"), bundle=bundle
    )
    result = service.generate(participant_ids=["npc_mara", "npc_doran"], brief="关于商队失踪的对质")
    assert result.structure_problems == []
    assert result.tree.root_node in result.tree.nodes
    assert any(node.choices for node in result.tree.nodes.values())
    assert result.lint_issues == []


def test_tree_structure_problems_flags_broken_links_and_unknown_speakers() -> None:
    tree = DialogueTree(
        id="dlg_bad",
        title="坏树",
        participants=["npc_mara"],
        root_node="missing_root",
        nodes={
            "n1": DialogueNode(
                id="n1",
                speaker_id="npc_ghost",
                text="hello",
                choices=[DialogueChoice(text="去哪", next_node="nowhere")],
            )
        },
    )
    problems = tree_structure_problems(tree, known_entities={"npc_mara"})
    assert any("根节点" in problem for problem in problems)
    assert any("npc_ghost" in problem for problem in problems)
    assert any("nowhere" in problem for problem in problems)


def test_dialogue_audit_rules_catch_broken_unknown_and_unreachable() -> None:
    bundle = _bundle_with_npcs()
    bundle.dialogue_trees["dlg_bad"] = DialogueTree(
        id="dlg_bad",
        title="坏树",
        root_node="n1",
        nodes={
            "n1": DialogueNode(id="n1", speaker_id="npc_ghost", text="x", next_node="missing"),
            "orphan": DialogueNode(id="orphan", speaker_id="npc_mara", text="y"),
        },
    )
    issues = AuditRunner(build_default_rule_registry()).run(AuditContext.from_bundle(bundle)).issues
    codes = {issue.rule_code for issue in issues}
    assert "DIALOGUE_TREE_BROKEN_LINK" in codes
    assert "DIALOGUE_TREE_UNKNOWN_SPEAKER" in codes
    assert "DIALOGUE_TREE_UNREACHABLE_NODE" in codes


def test_dialogue_tree_action_lands_via_review(tmp_path: Path) -> None:
    root = tmp_path / "content"
    _write_project(root)
    result = run_dialogue_tree_action(
        root, participant_ids=["npc_mara", "npc_doran"], brief="雨夜交接情报"
    )
    assert result["structure_problems"] == []
    decided = decide_review_action(
        root, item_id=result["review_item_id"], decision="accepted", operator="tester"
    )
    assert decided["written_ref"].startswith("dialogue_tree:")
    assert decided["post_audit_open_errors"] == 0
    reloaded = ContentStore(root).load()
    assert len(reloaded.dialogue_trees) == 1
    tree = next(iter(reloaded.dialogue_trees.values()))
    assert tree.review_status.value == "approved"
    assert tree.origin.value == "ai_draft"


def test_flavor_batch_offline_generates_typed_entities() -> None:
    bundle = _bundle_with_npcs()
    service = FlavorBatchService(
        gateway=_gateway(OfflineFlavorProvider(), "flavor_batch"), bundle=bundle
    )
    result = service.generate(category="skill", names=["疾风步", "落叶斩"], theme="武侠")
    assert [entry.name for entry in result.accepted] == ["疾风步", "落叶斩"]
    assert all(entity.type is EntityType.SKILL for entity in result.entities)
    assert all(entity.metadata.get("flavor_text") for entity in result.entities)


def test_flavor_action_lands_entities_via_review(tmp_path: Path) -> None:
    root = tmp_path / "content"
    _write_project(root)
    result = run_flavor_action(root, category="item", names=["雾隐灯"], theme="异象")
    assert len(result["accepted"]) == 1
    decided = decide_review_action(
        root, item_id=result["review_item_id"], decision="accepted", operator="tester"
    )
    assert decided["written_ref"].startswith("flavor_batch:")
    reloaded = ContentStore(root).load()
    items = [e for e in reloaded.entities.values() if e.type is EntityType.ITEM]
    assert len(items) == 1
    assert items[0].review_status.value == "approved"
