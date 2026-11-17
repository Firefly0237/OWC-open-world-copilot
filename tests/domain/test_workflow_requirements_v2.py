"""Unexecuted candidate checks; all author inputs are explicit development fixtures."""

from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle, Entity, Quest
from owcopilot.workflow.models import EventPlan, TaskInput, TaskRecord
from owcopilot.workflow.requirements_v2 import (
    BusinessRoleClaims,
    MachineRequirements,
    RequirementSourceBinding,
    build_requirements,
    check_requirement_outputs,
    check_role_requirements,
    compile_role_requirements,
    verify_requirement_binding,
)


@pytest.fixture
def task():
    bundle = ContentBundle(entities={
        "merchant": Entity(id="merchant", name="Merchant", type="npc"),
        "house": Entity(id="house", name="House", type="organization"),
    })
    return TaskRecord(
        project="development", request=TaskInput(
            brief="The merchant supplies medicine; the house owes the merchant.",
            protected=["Keep the mixed motives; the final choice should feel costly."],
        ),
        base_version="v1", base_hash=content_hash(bundle),
        base_bundle=bundle.model_dump(mode="json"),
    )


def _machine():
    return {
        "stage_count": 1, "decision_count": 1, "decision_option_count": 2,
        "participant_ids": ["merchant", "house"],
        "business_roles": [
            {"id": "supplier", "role": "supplier", "entity_id": "merchant",
             "source_refs": ["author_structured", "entity:merchant"]},
            {"id": "debtor", "role": "debtor", "entity_id": "house",
             "source_refs": ["author_structured", "entity:house"]},
        ],
    }


def _claims():
    text = "The merchant supplies medicine; the house owes the merchant."
    return BusinessRoleClaims.model_validate({"assignments": [
        {"binding_id": row["id"], "role": row["role"], "entity_id": row["entity_id"],
         "source_refs": row["source_refs"], "text_evidence": [{"path": "/quest/objective",
         "start": 0, "end": len(text), "quote": text, "interpretation": "explicit"}]}
         for row in _machine()["business_roles"]
    ]})


def _plan():
    return EventPlan.model_validate({
        "premise": "The house considers a medicine purchase", "beats": ["Choose"],
        "participant_ids": ["merchant", "house"],
        "events": [{"id": "choice", "summary": "Pay or defer"}],
        "decisions": [{"id": "pay", "event_id": "choice", "options": ["Pay", "Defer"],
                       "consequences": ["Cash decreases", "Debt increases"]}],
    })


@pytest.mark.parametrize("scenario", [
    "single_writer", "idea", "reference", "story_to_playable", "local_repair",
])
def test_all_original_entry_modes_share_one_bound_contract_without_relabeling_use_cases(
    task, scenario
):
    sources = []
    if scenario in {"reference", "story_to_playable"}:
        sources.append(RequirementSourceBinding(
            id="reference-1", kind="reference_excerpt", source_id="approved-source/chunk-1",
            content_hash=content_hash("Development reference excerpt"), locator="chunk-1",
        ))
    if scenario == "local_repair":
        sources.append(RequirementSourceBinding(
            id="draft-1", kind="draft_artifact", source_id="development-artifact",
            content_hash=content_hash({"quest": "development baseline"}), locator="/quest",
        ))
    contract = build_requirements(
        task, _machine(), scenario=scenario, authority="development_input", confirmed=True,
        sources=sources, use_case_ids=["UC-007", "UC-045"],
    )
    verify_requirement_binding(contract, task)
    for operation in ("plan_events", "draft_quest", "logic_contract", "dialogue_constrained"):
        inputs = compile_role_requirements(contract, operation)
        assert inputs["contract_hash"] == contract.contract_hash
        assert inputs["machine"]["stage_count"] == 1
        assert inputs["use_case_ids"] == ["UC-007", "UC-045"]
        assert inputs["original_brief"] == task.request.brief
        assert inputs["free_text"][1]["text"] == task.request.protected[0]
        assert inputs["free_text"][1]["status"] == "unknown"
        assert inputs["business_role_claims_schema"]["properties"]["assignments"]


def test_strict_counts_check_actual_outputs_and_do_not_read_the_old_logic_input(task):
    contract = build_requirements(
        task, {"stage_count": 1, "decision_count": 1, "decision_option_count": 2},
        authority="development_input", confirmed=True,
    )
    old = Quest(id="q", title="Old draft", stages=[{"id": "one", "summary": "One"}])
    changed = old.model_copy(update={"stages": [*old.stages, old.stages[0], old.stages[0]]})
    report = check_role_requirements(
        contract, "logic_contract", {"logic": changed.model_dump(mode="json")},
        inputs={"quest": old.model_dump(mode="json"), "plan": _plan().model_dump(mode="json")},
    )
    check = next(row for row in report.checks if row.key == "stage_count")
    assert check.status == "failed" and check.observed == 3
    assert report.can_continue is False
    plan = _plan().model_copy(update={"decisions": []})
    result = check_role_requirements(contract, "plan_events", {"plan": plan})
    assert result.machine_status == "failed"
    assert any(
        row.key == "decision_option_count" and row.status == "failed" for row in result.checks
    )


def test_business_source_roles_are_actual_claims_not_expected_metadata_or_name_matching(task):
    contract = build_requirements(
        task, _machine(), authority="development_input", confirmed=True,
    )
    quest = Quest(id="q", title="Medicine purchase",
                  objective="The merchant supplies medicine; the house owes the merchant.",
                  stages=[{"id": "one", "summary": "Choose"}])
    correct = check_role_requirements(contract, "draft_quest", {
        "quest": quest, "business_role_claims": _claims()},
    )
    assert correct.can_continue is True
    assert correct.status == "unknown" and correct.semantic_status == "unknown"
    missing = check_role_requirements(
        contract, "draft_quest", {"quest": quest}, inputs={"business_role_claims": _claims()},
    )
    assert missing.can_continue is False  # Upstream claims cannot impersonate this role's output.
    reversed_roles = _claims().model_dump(mode="json")
    reversed_roles["assignments"][0]["entity_id"] = "house"
    wrong = check_role_requirements(
        contract, "draft_quest", {"quest": quest, "business_role_claims": reversed_roles},
    )
    assert wrong.machine_status == "failed"
    no_source = deepcopy(_machine())
    no_source["business_roles"][0]["source_refs"] = ["LLM-invented-evidence"]
    with pytest.raises(ValueError, match="未绑定"):
        build_requirements(task, no_source, authority="development_input", confirmed=True)


def test_model_interpretations_and_free_text_never_become_confirmed_machine_proof(task):
    proposal = build_requirements(task, {"stage_count": 1}, authority="model_proposal")
    report = check_requirement_outputs(proposal, quest=Quest(id="q", title="Not confirmed"))
    assert report.machine_status == "unknown" and report.status == "unknown"
    assert report.author_satisfaction == "unknown"
    assert proposal.original_protected == task.request.protected
    with pytest.raises(ValueError, match="模型解释"):
        build_requirements(task, {"stage_count": 1}, authority="model_proposal", confirmed=True)
    with pytest.raises(ValidationError):
        MachineRequirements(stage_count=True)
    with pytest.raises(ValidationError):
        MachineRequirements.model_validate({"stage_count": 1, "emotion_score": 9})
    with pytest.raises(ValueError, match="未支持"):
        compile_role_requirements(proposal, "invented_writer")
    with pytest.raises(ValueError, match="真实参考"):
        build_requirements(task, scenario="reference")


def test_requirement_hash_scope_and_strict_distinctness_reject_unsupported_evidence(task):
    contract = build_requirements(
        task, {"stage_count": 1, "distinct_outcomes": True},
        authority="development_input", confirmed=True,
    )
    altered = contract.model_dump(mode="json")
    altered["machine"]["stage_count"] = 3
    with pytest.raises(ValueError, match="hash"):
        compile_role_requirements(altered, "draft_quest")
    with pytest.raises(ValueError, match="版本"):
        verify_requirement_binding(contract, task.model_copy(update={"revision": 2}))
    with pytest.raises(ValueError, match="项目"):
        verify_requirement_binding(contract, task.model_copy(update={"project": "other"}))
    quest = Quest(id="q", title="No v2 selector contract", stages=[{"id": "one", "summary": "One"}])
    report = check_role_requirements(
        contract, "logic_contract", {"logic": quest, "decisions": {"schema_version": 1}},
        inputs={"plan": _plan()},
    )
    assert report.machine_status == "failed"
    assert any("禁止回退v1" in row.reason for row in report.checks)
