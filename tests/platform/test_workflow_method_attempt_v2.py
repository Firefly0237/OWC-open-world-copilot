"""Controlled model protocol, real method ledger/compiler/version persistence.

The source is an explicitly prepared development graph, not a completed narrative
or evidence of author satisfaction. No real model or production database is used.
"""

import json

from owcopilot.content.hash import content_hash
from owcopilot.llm.gateway import ToolCall, ToolCallResponse
from owcopilot.workflow import v2_service, v2_skills
from owcopilot.workflow.graph_delta import (
    baseline_to_constrained_delta,
    project_legacy_graph_delta,
)
from owcopilot.workflow.graph_v2 import GraphSpecV2, create_narrative_graph
from owcopilot.workflow.harness import HarnessStore
from tests.platform import test_workflow_v2_service as source_fixtures

studio = source_fixtures.studio


class BusinessEditTransport:
    model = "controlled-business-edit-v2"
    max_retries = 0
    supports_tools = True

    def __init__(self, graph, *, wrong_identity=False):
        self.graph = graph
        self.wrong_identity = wrong_identity
        self.native_calls = []
        self.curator_calls = []
        self.context = None

    def complete_with_tools(self, *, messages, tools, model):
        self.native_calls.append(messages)
        if len(self.native_calls) == 1:
            self.context = json.loads(messages[-1]["content"])
            evidence = tools[0]["function"]["parameters"]["properties"]["evidence_id"]["enum"][0]
            return ToolCallResponse(
                tool_calls=[ToolCall("read", "read_feedback_evidence", {"evidence_id": evidence})]
            )
        outcome = json.loads(messages[-1]["content"])
        assert outcome["is_error"] is False
        snapshot = json.loads(outcome["value"]["evidence_json"])
        assert content_hash(snapshot) == outcome["value"]["snapshot_hash"]
        problem = self.context["problem_id"]
        assert self.context["output_schema"]["properties"]["problem_id"]["const"] == problem
        return ToolCallResponse(
            text=json.dumps(
                {
                    "problem_id": problem,
                    "target_description": "缺少显式决策合同",
                    "diagnosis": "Prepared graph has no typed decision gate; effect is untested.",
                    "evidence_refs": self.context["allowed_source_refs"],
                    "proposed_change": "structure",
                }
            )
        )

    def complete(self, *, system, user, model):
        context = json.loads(user)
        self.curator_calls.append(context)
        assert "Curator" in system
        assert all("product_key" not in row for row in context["graph"]["nodes"])
        old = baseline_to_constrained_delta(GraphSpecV2.model_validate(self.graph))
        edit = project_legacy_graph_delta(old.model_dump(mode="json"))["edit"]
        assert edit["expected_graph_hash"] == context["expected_graph_hash"]
        return (
            json.dumps(
                {
                    "problem_id": "wrong" if self.wrong_identity else context["problem_id"],
                    "target_description": "The typed decision contract is absent",
                    "rationale": "Controlled structural proposal, not a claim of goal improvement",
                    "change_kind": "structure",
                    "graph_edit": edit,
                }
            ),
            12,
            10,
            0,
            self.model,
        )


def test_known_failure_new_attempt_preserves_original_owner_and_installs_real_edit(studio):
    root, store, task = studio
    skills = v2_skills.NarrativeEvolutionStore(store)
    graph = create_narrative_graph().model_dump(mode="json")
    family = skills.create_family("demo", "Controlled protocol fixture")
    parent = skills.add_version(
        "demo", family["id"], graph=graph, expected_family_hash=family["family_hash"]
    )
    prepared = v2_service.prepare_v2_run(
        store,
        root,
        task,
        graph,
        skill_version_id=parent["id"],
    )
    broken = BusinessEditTransport(graph, wrong_identity=True)
    failed = v2_skills.evolve_family(
        store,
        root,
        "demo",
        family["id"],
        prepared["run_id"],
        "Add typed decision evidence to this prepared development graph.",
        provider=broken,
        source_kind="development_input",
    )
    assert failed["result"]["status"] == "failed"
    immutable = skills.get_episode("demo", failed["id"])["result"]
    fixed = BusinessEditTransport(graph)
    retried = v2_skills.evolve_family(
        store,
        root,
        "demo",
        family["id"],
        prepared["run_id"],
        "Add typed decision evidence to this prepared development graph.",
        provider=fixed,
        source_kind="development_input",
        attempt_reason="Fix immutable identity with new v2 schema; old response is known.",
    )
    assert retried["id"] == failed["id"]
    assert retried["result"]["status"] == "candidate", retried
    assert skills.get_episode("demo", failed["id"])["result"] == immutable
    old_requests = HarnessStore(store).requests(failed["trace_run_id"])
    new_requests = HarnessStore(store).requests(retried["trace_run_id"])
    assert [row["attempt"] for row in [*old_requests, *new_requests]] == [1, 2, 3, 4, 5, 6]
    assert len({row["budget_id"] for row in [*old_requests, *new_requests]}) == 1
    candidate = skills.get_version("demo", retried["result"]["candidate_version_id"])
    receipt = candidate["delta"]["graph_compilation"]
    assert receipt["edit_hash"] == content_hash(receipt["edit"])
    assert receipt["applied"]["graph_hash"] == candidate["execution_graph_hash"]
    gate = next(
        row for row in candidate["graph"]["nodes"] if row["operation"] == "validate_decisions"
    )
    assert gate["context_bindings"] == {} and gate["product_key"] == "slice/logic"
    assert candidate["state"]["author_acceptance"] == "unknown"
    duplicate = v2_skills.evolve_family(
        store,
        root,
        "demo",
        family["id"],
        prepared["run_id"],
        "Add typed decision evidence to this prepared development graph.",
        provider=fixed,
        source_kind="development_input",
    )
    assert duplicate["result"]["candidate_version_id"] == candidate["id"]
    assert len(fixed.native_calls) == 2 and len(fixed.curator_calls) == 1
