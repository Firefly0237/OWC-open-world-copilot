"""Necessary production contracts; fixtures are controlled inputs, not model efficacy evidence."""

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.llm.cache import HashingEmbedder
from owcopilot.workflow.evolution import GuidanceEntry, SourceRef
from owcopilot.workflow.evolution_v2 import (
    ParameterSlot,
    PlanIR,
    PlannedNode,
    ReflectionReport,
    ReusableEntry,
    SemanticMaintenance,
    TypedEvolutionProposalV2,
    compile_plan_ir,
    render_parameterized_guidance,
    verify_grounding,
)
from owcopilot.workflow.graph_delta import (
    baseline_to_constrained_delta,
    project_legacy_graph_delta,
)
from owcopilot.workflow.graph_v2 import NodeSpecV2, create_narrative_graph, narrative_role_registry
from owcopilot.workflow.store import WorkflowStore
from owcopilot.workflow.v2_method_runtime import _curate
from owcopilot.workflow.v2_skills import NarrativeEvolutionStore


def test_plan_ir_assigns_budget_in_host_and_rejects_planner_supplied_identity():
    seed = create_narrative_graph()
    plan = PlanIR(
        rationale="Controlled input for actual compiler",
        nodes=[PlannedNode(id=node.id, operation=node.operation) for node in seed.nodes],
        data_edges=seed.data_edges,
        control_edges=seed.control_edges,
        outputs=seed.outputs,
    )
    compiled = compile_plan_ir(plan, narrative_role_registry())
    assert compiled.id != seed.id
    assert next(node for node in compiled.nodes if node.id == "logic").problem_key == (
        "slice/logic/produce"
    )
    unsafe = plan.model_dump(mode="json")
    unsafe["nodes"][0]["product_key"] = "planner-grants-fresh-quota"
    with pytest.raises(ValueError):
        PlanIR.model_validate(unsafe)
    unsafe = plan.model_dump(mode="json")
    unsafe["data_edges"] = [
        edge
        for edge in unsafe["data_edges"]
        if not (edge["target_node"] == "audit" and edge["target_port"] == "bound")
    ]
    with pytest.raises(ValueError):
        compile_plan_ir(PlanIR.model_validate(unsafe), narrative_role_registry())


def test_parameterized_experience_binds_new_entities_and_keeps_source_and_counterexample():
    source = SourceRef(kind="run", id="source", content_hash=content_hash("frozen"))
    entry = ReusableEntry(
        id="current_speaker",
        text="Ask {{speaker}} to decide; keep {{count}} stage.",
        operation="draft_quest",
        support_refs=[source],
        counterexample_refs=[source],
        slots=[
            ParameterSlot(
                name="speaker", kind="entity", binding_key="participant:0", source_value="old_npc"
            ),
            ParameterSlot(
                name="count", kind="requirement", binding_key="stage_count", source_value="1"
            ),
        ],
    )
    report = ReflectionReport(
        diagnosis="Use task-local bindings",
        target_problem="stage mismatch",
        evidence_refs=[source],
        reusable_entries=[entry],
        proposed_change="guidance",
    )
    verify_grounding(report, None, [source], {"old_npc"}, {"stage_count": 1}, author_feedback=False)
    node = NodeSpecV2(
        id="writer",
        operation="draft_quest",
        product_key="quest",
        problem_key="produce",
        guidance=entry.text,
        parameters={"skill_entry_templates": [entry.model_dump(mode="json")]},
    )
    rendered = render_parameterized_guidance(
        node,
        {
            "entities": [{"id": "new_npc"}],
            "machine_requirements": {"participant_ids": ["new_npc"], "stage_count": 2},
        },
    )
    assert rendered == "Ask new_npc to decide; keep 2 stage."
    assert "old_npc" not in rendered
    assert entry.counterexample_refs == [source]
    with pytest.raises(ValueError):
        render_parameterized_guidance(node, {"entities": [{"id": "another_npc"}]})
    forged = report.model_copy(
        update={
            "evidence_refs": [
                SourceRef(kind="run", id="invented", content_hash=source.content_hash)
            ]
        }
    )
    with pytest.raises(ValueError):
        verify_grounding(
            forged, None, [source], {"old_npc"}, {"stage_count": 1}, author_feedback=False
        )


def test_semantic_maintenance_rejects_hashing_without_creating_semantic_vectors(tmp_path):
    workflow = WorkflowStore(tmp_path / "workflow.sqlite3")
    maintenance = SemanticMaintenance(workflow, HashingEmbedder())
    with pytest.raises(ValueError, match="hashing"):
        maintenance.candidates(
            "project", "family", [GuidanceEntry(id="rule", text="Keep consequences explicit")]
        )
    with workflow.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM evolution_entry_vectors").fetchone()[0] == 0


def test_curator_applies_real_typed_patch_and_preserves_unknown_acceptance(tmp_path):
    workflow = WorkflowStore(tmp_path / "workflow.sqlite3")
    skills = NarrativeEvolutionStore(workflow)
    family = skills.create_family("project", "Controlled method")
    source = SourceRef(
        kind="development_input", id="source-run", content_hash=content_hash("fixture")
    )
    seed = create_narrative_graph()
    parent = skills.add_version(
        "project",
        family["id"],
        graph=seed.model_dump(mode="json"),
        expected_family_hash=family["family_hash"],
        source_refs=[source],
    )
    edit = project_legacy_graph_delta(baseline_to_constrained_delta(seed).model_dump(mode="json"))[
        "edit"
    ]
    proposal = TypedEvolutionProposalV2(
        problem_id="evolution:" + content_hash(["project", family["id"], "decision-handoff"]),
        rationale="Add explicit state contract consumption",
        target_description="missing decision handoff",
        change_kind="structure",
        graph_edit=edit,
    )
    child = _curate(
        skills,
        "project",
        parent,
        proposal,
        {"status": "not_needed", "pairs": []},
        source,
        skills.get_family("project", family["id"])["family_hash"],
    )
    assert child["parent_version_id"] == parent["id"]
    assert child["execution_graph_hash"] != parent["execution_graph_hash"]
    assert any(node["operation"] == "validate_decisions" for node in child["graph"]["nodes"])
    assert child["state"]["technical_status"] == "compiled"
    assert child["state"]["author_acceptance"] == "unknown"
    assert skills.get_family("project", family["id"])["default_version_id"] is None
    assert child["delta"]["proposal"]["change_kind"] == "structure"
