"""Controlled native transport; real Harness, capability lifecycle, versioning and driver.

The fixture is not evidence of a real model discovering a graph or author approval.
"""

import json

from owcopilot.content.hash import content_hash
from owcopilot.llm.gateway import ToolCall, ToolCallResponse
from owcopilot.workflow import v2_service, v2_skills
from owcopilot.workflow.collaboration_v2 import CollaborationStore
from owcopilot.workflow.graph_delta import GraphEditIR
from owcopilot.workflow.graph_driver import execute_persistent_graph
from owcopilot.workflow.graph_v2 import (
    DataEdge,
    GraphSpecV2,
    NodeSpecV2,
    OutputRef,
    PortSpec,
    RoleContract,
    compile_graph,
    create_narrative_graph,
)
from owcopilot.workflow.harness import HarnessStore
from owcopilot.workflow.method_trial_scope_v2 import grant_trial_scope
from owcopilot.workflow.models import RunRecord
from tests.platform.test_workflow_v2_service import ControlledNarrativeProvider
from tests.platform.test_workflow_v2_service import studio as studio


class VersionedNarrativeTransport(ControlledNarrativeProvider):
    """Keep the decision protocol explicit in the controlled transport."""
    def complete(self, **kwargs):
        text, *usage = super().complete(**kwargs)
        value = json.loads(text)
        if isinstance(value.get("decisions"), dict):
            value["decisions"].update(schema_version=2, selector_variables=["route"])
        return (json.dumps(value), *usage)


class SplitRoleTransport:
    model = "controlled-split-role-transport"
    max_retries = 0
    supports_tools = True

    def __init__(self):
        self.native_calls = []
        self.curator_calls = []
        self.reflection_context = None

    def complete_with_tools(self, *, messages, tools, model):
        self.native_calls.append(messages)
        if len(self.native_calls) == 1:
            self.reflection_context = json.loads(messages[-1]["content"])
            evidence_id = tools[0]["function"]["parameters"]["properties"]["evidence_id"]["enum"][0]
            return ToolCallResponse(
                tool_calls=[
                    ToolCall(
                        "frozen-evidence", "read_feedback_evidence", {"evidence_id": evidence_id}
                    )
                ],
                reasoning_content="Controlled protocol fixture: request the authorized evidence.",
            )
        assert len(self.native_calls) == 2
        outcome = json.loads(messages[-1]["content"])
        assert outcome["is_error"] is False
        snapshot = json.loads(outcome["value"]["evidence_json"])
        assert content_hash(snapshot) == outcome["value"]["snapshot_hash"]
        assert "decision contract" in snapshot["feedback"]
        return ToolCallResponse(
            text=json.dumps(
                {
                    "protocol": "method-reflection-v3",
                    "problem_id": self.reflection_context["problem_id"],
                    "diagnosis": "The baseline lacks a typed contract before dialogue.",
                    "target_description": "decision-handoff",
                    "proposed_change": "structure",
                    "evidence_refs": self.reflection_context["allowed_source_refs"],
                    "reusable_entries": [],
                    "risks": ["An extra gate may reject an invalid draft."],
                }
            ),
            reasoning_content="Controlled protocol fixture: emit the grounded report.",
        )

    def complete(self, *, system, user, model):
        context = json.loads(user)
        self.curator_calls.append(context)
        assert "Curator" in system
        assert len(self.native_calls) == 2 and len(self.curator_calls) == 1
        assert context["reflection"]["target_description"] == "decision-handoff"
        assert context["reflection"]["problem_id"] == context["problem_id"]
        by_role = {node["operation"]: node["id"] for node in context["graph"]["nodes"]}
        plan, logic, dialogue, bind, audit = (
            by_role[operation] for operation in (
                "plan_events", "draft_logic", "draft_dialogue", "bind_dialogue", "coverage_audit"
            )
        )
        operations = [
            {"op": "replace_registered_role", "node_id": logic, "operation": "logic_contract"},
            {
                "op": "replace_registered_role", "node_id": dialogue,
                "operation": "dialogue_constrained",
            },
            {
                "op": "insert_registered_role", "node_id": "decision_gate",
                "operation": "validate_decisions", "source_node": logic,
            },
        ]
        for source, source_port, target, target_port in (
            (plan, "plan", logic, "plan"),
            (plan, "plan", "decision_gate", "plan"),
            (logic, "logic", "decision_gate", "logic"),
            (logic, "decisions", "decision_gate", "decisions"),
            ("decision_gate", "contract", dialogue, "contract"),
            (logic, "decisions", bind, "decisions"),
            (dialogue, "choices", bind, "choices"),
            (logic, "decisions", audit, "decisions"),
            (dialogue, "choices", audit, "choices"),
        ):
            operations.append({
                "op": "connect_data", "edge": {
                    "source_node": source, "source_port": source_port,
                    "target_node": target, "target_port": target_port,
                },
            })
        edit = GraphEditIR(
            expected_graph_hash=context["expected_graph_hash"],
            rationale="Consume a checked decision contract before dialogue and final audit",
            operations=operations,
        ).model_dump(mode="json")
        proposal = {
            "protocol": "method-proposal-v3",
            "problem_id": context["problem_id"],
            "rationale": "Controlled typed structural candidate, not an efficacy claim.",
            "target_description": "decision-handoff",
            "change_kind": "structure",
            "graph_edit": edit,
        }
        return json.dumps(proposal), 12, 10, 0, self.model


def test_native_reflector_curator_share_budget_and_candidate_is_really_consumed(studio):
    root, store, task = studio
    prepared = v2_service.prepare_v2_run(
        store, root, task, create_narrative_graph().model_dump(mode="json")
    )
    baseline = v2_service.execute_v2_run(
        store, root, "demo", prepared["run_id"], provider=VersionedNarrativeTransport()
    )
    assert baseline["run"]["status"] == "completed", baseline
    baseline_quest = baseline["result"]["outputs"]["bound"]["quest"]
    prefix = "quest." + baseline_quest["id"]
    expected_sources = {
        prefix + ".title": "Guard at the gate",
        prefix + ".objective": "Choose whether to help",
        prefix + ".stage.gate.summary": "Talk",
        prefix + ".branch.pay.outcome": "paid",
        prefix + ".branch.refuse.outcome": "refused",
    }
    localization = baseline_quest["metadata"]["localization_v2"]
    assert localization["source_strings"] == expected_sources
    assert localization["source_strings_hash"] == content_hash(expected_sources)
    assert baseline_quest["localization_keys"] == list(expected_sources)
    assert baseline_quest["metadata"]["quest_draft_v2"]["source"]["title"] == "Guard at the gate"
    assert baseline_quest["logic"]["branches"][0]["effects"] == [
        {"var": "trust", "op": "inc", "value": 1}
    ]
    saved = v2_skills.save_run_skill(
        store, "demo", prepared["run_id"], name="Controlled split roles"
    )
    native = SplitRoleTransport()
    episode = v2_skills.evolve_family(
        store,
        root,
        "demo",
        saved["family"]["id"],
        prepared["run_id"],
        "Add an actual decision contract consumed by the dialogue role.",
        provider=native,
        source_kind="development_input",
    )
    assert episode["result"]["status"] == "candidate", episode
    dispatches = HarnessStore(store).requests(episode["trace_run_id"])
    assert [row["attempt"] for row in dispatches] == [1, 2, 3]
    assert len({row["budget_id"] for row in dispatches}) == 1
    assert [row["request"]["role_id"] for row in dispatches] == [
        "method_reflector",
        "method_reflector",
        "method_curator",
    ]
    skills = v2_skills.NarrativeEvolutionStore(store)
    candidate_id = episode["result"]["candidate_version_id"]
    version = skills.get_version("demo", candidate_id)
    assert len(version["delta"]["proposal"]["graph_edit"]["operations"]) == 12
    assert version["state"]["author_acceptance"] == "unknown"
    assert skills.get_family("demo", saved["family"]["id"])["default_version_id"] is None
    with store.connection() as db:
        capability = json.loads(
            db.execute(
                "SELECT payload FROM creative_capabilities_v2 WHERE project=?", ("demo",)
            ).fetchone()[0]
        )
    assert capability["descriptor"]["status"] == "revoked"
    skills.authorize_trial(
        "demo",
        candidate_id,
        expected_family_hash=skills.get_family("demo", saved["family"]["id"])["family_hash"],
        allowed=True,
        authority="preauthorized_policy",
        note="Controlled development action",
    )
    machine_requirements = prepared["requirements"]["machine"]
    trial_scope = grant_trial_scope(
        store, "demo", task.id, [candidate_id],
        machine_requirements=machine_requirements,
        authority="development_technical_trial",
        reason="Controlled execution of the compiled candidate, not author adoption",
    )
    child = v2_skills.prepare_skill_run(
        store, root, task, candidate_id, prior_run_id=prepared["run_id"],
        machine_requirements=machine_requirements,
        trial_scope_id=trial_scope["id"], expected_trial_scope_hash=trial_scope["permit_hash"],
    )
    production = VersionedNarrativeTransport()
    result = v2_service.execute_v2_run(store, root, "demo", child["run_id"], provider=production)
    assert result["run"]["status"] == "completed", result
    assert result["result"]["outcomes"]["logic"]["operation"] == "logic_contract"
    assert result["result"]["outcomes"]["plan"]["status"] == "reused"
    assert result["result"]["outcomes"]["quest"]["status"] == "reused"
    dialogue_request = next(
        row for row in HarnessStore(store).requests(child["run_id"]) if row["node_id"] == "dialogue"
    )
    actual_inputs = json.loads(dialogue_request["request"]["user"])["inputs"]
    assert actual_inputs["contract"]["status"] == "validated"
    assert actual_inputs["contract"]["plan_hash"]
    assert actual_inputs["contract"]["quest_hash"]
    assert skills.get_version("demo", candidate_id)["state"]["author_acceptance"] == "unknown"
    repeated = v2_skills.evolve_family(
        store,
        root,
        "demo",
        saved["family"]["id"],
        prepared["run_id"],
        "Add an actual decision contract consumed by the dialogue role.",
        provider=native,
        source_kind="development_input",
    )
    assert repeated["id"] == episode["id"]
    assert len(native.native_calls) == 2 and len(native.curator_calls) == 1


def test_shared_native_sibling_gate_resumes_both_tasks_once_before_dag_join(studio, tmp_path):
    _root, store, task = studio
    run = RunRecord(task_id=task.id, revision=task.revision, status="running")
    with store.connection() as db:
        db.execute("INSERT INTO runs VALUES(?,?,?)", (run.id, task.id, run.model_dump_json()))
    collaboration = CollaborationStore(store)
    collaboration.initialize("demo", run.id, {"mode": "edit", "plan_review": False})
    port = PortSpec(type_name="json")
    registry = {
        operation: RoleContract(
            operation=operation, inputs={"value": port}, outputs={"value": port}
        )
        for operation in ("plan_events", "draft_logic", "draft_dialogue")
    }
    registry["join"] = RoleContract(
        operation="join", inputs={"left": port, "right": port}, outputs={"value": port}
    )
    graph = GraphSpecV2(
        name="Native sibling pause and join",
        nodes=[
            NodeSpecV2(
                id="start",
                operation="plan_events",
                product_key="seed",
                problem_key="seed",
                context_bindings={"value": "seed"},
            ),
            NodeSpecV2(id="left", operation="draft_logic", product_key="left", problem_key="left"),
            NodeSpecV2(
                id="right", operation="draft_dialogue", product_key="right", problem_key="right"
            ),
            NodeSpecV2(id="join", operation="join", product_key="join", problem_key="join"),
        ],
        data_edges=[
            DataEdge(
                source_node="start", source_port="value", target_node="left", target_port="value"
            ),
            DataEdge(
                source_node="start", source_port="value", target_node="right", target_port="value"
            ),
            DataEdge(
                source_node="left", source_port="value", target_node="join", target_port="left"
            ),
            DataEdge(
                source_node="right", source_port="value", target_node="join", target_port="right"
            ),
        ],
        outputs={"answer": OutputRef(node_id="join", port="value")},
    )
    compiled = compile_graph(graph, registry, {"seed": port})
    calls = []

    def handler(node, inputs):
        calls.append(node.id)
        value = inputs["left"] + inputs["right"] if node.id == "join" else inputs["value"]
        return {"value": value}

    handlers = dict.fromkeys(registry, handler)
    common = {
        "checkpoint_path": tmp_path / "siblings.sqlite3",
        "thread_id": run.id,
        "before_node": lambda node, current: collaboration.before_node(
            "demo", run.id, node, current, graph_hash=compiled.graph_hash
        ),
        "consume_resume": lambda gate, decision: collaboration.consume_resume(
            "demo", run.id, gate, decision
        ),
    }
    waiting = execute_persistent_graph(compiled, {"seed": 2}, handlers, **common)
    assert waiting.status == "waiting_user"
    assert calls == ["start"]
    store.update_run(run.id, status="waiting_user")
    gate = collaboration.detail("demo", run.id)["pending_gate"]
    decision = collaboration.authorize_resume("demo", run.id, expected_gate_hash=gate["gate_hash"])
    assert (
        collaboration.authorize_resume("demo", run.id, expected_gate_hash=gate["gate_hash"])
        == decision
    )
    resumed = execute_persistent_graph(
        compiled, {"seed": 2}, handlers, resume=collaboration.resume_value("demo", run.id), **common
    )
    assert resumed.status == "completed", resumed
    assert resumed.outputs["answer"] == 4
    assert all(calls.count(node_id) == 1 for node_id in ("start", "left", "right", "join"))
    store.update_run(run.id, status="completed")
    finished = execute_persistent_graph(compiled, {"seed": 2}, handlers, **common)
    assert finished.outputs["answer"] == 4
    assert len(calls) == 4
    detail = collaboration.detail("demo", run.id)
    assert detail["pending_gate"] is None
    assert len(detail["history"]) == 1
