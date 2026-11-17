"""Controlled feedback decisions exercise the real episode and budget store."""

import json

import pytest

from owcopilot.llm.gateway import ToolCallResponse
from owcopilot.workflow import v2_method_runtime, v2_service, v2_skills
from owcopilot.workflow.feedback_routing import route_feedback
from owcopilot.workflow.graph_v2 import create_narrative_graph
from owcopilot.workflow.harness import HarnessStore
from tests.platform.test_workflow_method_attempt_v2 import BusinessEditTransport
from tests.platform.test_workflow_v2_service import studio as studio


def source(studio):
    root, store, task = studio
    skills = v2_skills.NarrativeEvolutionStore(store)
    family = skills.create_family("demo", "Feedback contract fixture")
    graph = create_narrative_graph().model_dump(mode="json")
    parent = skills.add_version(
        "demo", family["id"], graph=graph, expected_family_hash=family["family_hash"]
    )
    run = v2_service.prepare_v2_run(store, root, task, graph, skill_version_id=parent["id"])
    return root, store, family, graph, run


class UnchangedTransport(BusinessEditTransport):
    def __init__(self, graph, evidence_status):
        super().__init__(graph)
        self.evidence_status = evidence_status

    def complete_with_tools(self, **kwargs):
        result = super().complete_with_tools(**kwargs)
        if result.text:
            report = json.loads(result.text)
            report.update(proposed_change="none", evidence_status=self.evidence_status)
            return ToolCallResponse(text=json.dumps(report))
        return result

    def complete(self, **kwargs):
        raise AssertionError("No-change or insufficient evidence must not dispatch a curator")


@pytest.mark.parametrize(
    "evidence,status",
    [
        ("sufficient", "no_change"),
        ("insufficient", "insufficient_evidence"),
    ],
)
def test_unchanged_reflection_finishes_without_curator_or_version(studio, evidence, status):
    root, store, family, graph, run = source(studio)
    provider = UnchangedTransport(graph, evidence)
    first = v2_method_runtime.evolve_family(
        store,
        root,
        "demo",
        family["id"],
        run["run_id"],
        "Review this method",
        provider=provider,
        source_kind="development_input",
    )
    assert first["result"]["status"] == status, first
    assert first["result"]["candidate_version_id"] is None
    assert first["attempt_state"]["status"] == "completed"
    assert len(HarnessStore(store).requests(first["trace_run_id"])) == 2
    assert len(v2_skills.NarrativeEvolutionStore(store).versions("demo", family["id"])) == 1
    repeated = v2_method_runtime.evolve_family(
        store,
        root,
        "demo",
        family["id"],
        run["run_id"],
        "Review this method",
        provider=provider,
        source_kind="development_input",
    )
    assert repeated["result"]["status"] == status
    assert len(provider.native_calls) == 2 and provider.curator_calls == []


@pytest.mark.parametrize(
    "kind,destination",
    [
        ("content_fact", "content_review"),
        ("project_preference", "project_review"),
        ("technical_failure", "technical_repair"),
        ("provider_failure", "operational_diagnosis"),
        ("unclassified", "needs_evidence"),
    ],
)
def test_non_method_feedback_is_deduplicated_without_model_or_creative_experience(
    studio,
    kind,
    destination,
):
    root, store, family, _graph, run = source(studio)
    args = (store, root, "demo", family["id"], run["run_id"], "An explicit feedback event")
    options = dict(feedback_kind=kind, feedback_event_id="same-event", provider=object())
    result = v2_method_runtime.evolve_family(*args, **options)
    assert result["feedback_route"]["destination"] == destination
    assert result["feedback_route"]["method_eligible"] is False
    repeated = v2_method_runtime.evolve_family(*args, **options)
    assert repeated["feedback_route"] == result["feedback_route"]
    assert v2_skills.NarrativeEvolutionStore(store).experiences("demo", family["id"]) == []
    with pytest.raises(ValueError, match="同一反馈事件"):
        v2_method_runtime.evolve_family(*args, **{**options, "feedback_kind": "method"})


def test_failed_source_routes_to_repair_without_keyword_diagnosis(studio):
    _root, store, family, _graph, run = source(studio)
    snapshot = dict(run, result={"outcomes": {"logic": {"status": "failed", "error": "bad"}}})
    route = route_feedback(
        store,
        "demo",
        family["id"],
        snapshot,
        "A connection in the story is fine",
        source_kind="development_input",
    )
    assert route["kind"] == "technical_failure"
    assert route["failed_nodes"] == ["logic"] and not route["method_eligible"]
