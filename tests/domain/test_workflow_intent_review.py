"""Author-confirmed candidate gate over real shadow application, audits and temporal checks."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from owcopilot.audit.baseline import issue_fingerprint
from owcopilot.audit.context import AuditContext
from owcopilot.audit.registry import RuleRegistry
from owcopilot.audit.rules.reference_rules import MissingDialogueReferenceRule
from owcopilot.audit.runner import AuditRunner
from owcopilot.content.hash import content_hash
from owcopilot.content.models import (
    Branch,
    ContentBundle,
    LogicVar,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
)
from owcopilot.patches.models import PatchCandidate
from owcopilot.workflow.intent_models import IntentContract
from owcopilot.workflow.intent_review import review_intent_patch
from owcopilot.workflow.models import Artifact, TaskInput, TaskRecord

ROOT = "/quests/q~1one"


def test_unknown_symbols_and_unconsumed_spec_fields_cannot_create_false_hard_proof():
    task, artifact, contract, candidate, runner = setup()
    for spec in (
        {"pattern": "AG", "expression": "not ghost"},
        {"pattern": "AG", "expression": "alive", "int_ranges": {"ghost": [0, 1]}},
        {"pattern": "EF", "expression": "alive", "antecedent": "trigger"},
        {"pattern": "AG_EF_ENDING", "expression": "alive"},
    ):
        changed = contract.model_copy(
            update={"formal_rules": [type(contract.formal_rules[0])(quest_id="q/one", spec=spec)]}
        )
        report = review_intent_patch(
            changed, task=task, baseline=artifact, candidate=candidate, runner=runner
        )
        assert report.status == "blocked"
        assert report.formal_results[0]["after"]["status"] == "unsupported"
        assert report.candidate_bundle is None


def setup(*, blocked=False, no_logic=False, clean=False, initial_state=None):
    quest = Quest(
        id="q/one",
        title="Keep the sacrifice",
        objective="Original objective",
        dialogue_refs=[] if clean else ["missing"],
        stages=[QuestStage(id="start", summary="Begin"), QuestStage(id="end", summary="Finish")],
        logic=None
        if no_logic
        else QuestLogic(
            variables=[LogicVar(id="alive", default=True), LogicVar(id="trigger", default=False)],
            stage_logic=[StageLogic(stage_id="end", precondition="trigger" if blocked else "")],
            branches=[
                Branch(id="advance", from_stage="start", to_stage="end"),
                Branch(id="finish", from_stage="end", outcome="done"),
            ],
        ),
    )
    other = Quest(id="q/one-more", title="An unrelated quest")
    bundle = ContentBundle(quests={quest.id: quest, other.id: other})
    task = TaskRecord(
        project="intent-check",
        request=TaskInput(
            brief="Explicit candidate review check",
            comparison={"initial_state": initial_state} if initial_state is not None else None,
        ),
        base_version="v1",
        base_hash=content_hash(bundle),
        base_bundle=bundle.model_dump(mode="json"),
    )
    payload = {"quest": quest.model_dump(mode="json")}
    artifact = Artifact(
        task_id=task.id,
        revision=1,
        run_id="fixture",
        node_id="draft_logic",
        kind="quest_with_logic",
        payload=payload,
        input_hash="fixture-input",
        content_hash=content_hash(payload),
    )
    runner = AuditRunner(RuleRegistry([MissingDialogueReferenceRule()]))
    errors = runner.run(AuditContext.from_bundle(bundle)).open_errors
    contract = IntentContract(
        project=task.project,
        task_id=task.id,
        task_revision=task.revision,
        baseline_artifact_id=artifact.id,
        baseline_artifact_hash=artifact.content_hash,
        confirmed=True,
        allowed_paths=[ROOT + "/dialogue_refs"],
        hard_locks=[{"path": ROOT + "/title"}],
        formal_rules=[
            {"quest_id": quest.id, "spec": {"id": "alive", "pattern": "AG", "expression": "alive"}}
        ],
        soft_text=["保留人物的代价"],
        modification_goal="修复悬空对白引用",
        target_error_fingerprints=[issue_fingerprint(issue) for issue in errors],
    )
    candidate = PatchCandidate(
        ops=[{"op": "replace", "path": ROOT + "/dialogue_refs", "value": []}]
    )
    return task, artifact, contract, candidate, runner


def review(context, *, contract=None, candidate=None):
    task, artifact, original, patch, runner = context
    return review_intent_patch(
        contract or original,
        task=task,
        baseline=artifact,
        candidate=candidate or patch,
        runner=runner,
    )


def test_confirmed_local_repair_rechecks_real_error_and_rule_without_mutating_baseline():
    context = setup()
    task, artifact, contract, _, _ = context
    frozen_task, frozen_artifact = task.model_dump(), artifact.model_dump()
    report = review(context)
    assert report.status == "ready_for_author"
    assert report.changed_paths == [ROOT + "/dialogue_refs"]
    assert report.resolved_errors == contract.target_error_fingerprints
    assert report.remaining_targets == report.introduced_errors == []
    assert report.formal_results[0]["after"]["status"] == "proven"
    assert report.goal_status == "withheld"
    assert report.soft_results == [{"text": "保留人物的代价", "status": "unknown"}]
    assert report.candidate_bundle["quests"]["q/one"]["dialogue_refs"] == []
    assert task.model_dump() == frozen_task and artifact.model_dump() == frozen_artifact


def test_permissions_use_decoded_actual_changes_including_parent_and_list_replacement():
    context = setup()
    _, artifact, contract, candidate, _ = context
    replacement = deepcopy(artifact.payload["quest"])
    replacement.update(dialogue_refs=[], title="Erase the sacrifice")
    invalid = [
        (contract, PatchCandidate(ops=[{"op": "replace", "path": ROOT, "value": replacement}])),
        (
            contract,
            PatchCandidate(
                ops=[{"op": "replace", "path": "/quests/q~1one-more/title", "value": "Changed"}]
            ),
        ),
        (
            contract.model_copy(update={"allowed_paths": [ROOT + "/dialogue_refs/0"]}),
            PatchCandidate(ops=[{"op": "remove", "path": ROOT + "/dialogue_refs/0"}]),
        ),
        (
            contract.model_copy(update={"allowed_paths": [ROOT]}),
            PatchCandidate(ops=[{"op": "replace", "path": ROOT, "value": replacement}]),
        ),
        (
            contract,
            PatchCandidate(
                ops=[{"op": "replace", "path": ROOT + "/dialogue_refs/-1", "value": "other"}]
            ),
        ),
    ]
    for limits, patch in invalid:
        result = review(context, contract=limits, candidate=patch)
        assert result.status == "blocked" and result.reasons
        assert result.candidate_bundle is None
    assert review(context, candidate=candidate).status == "ready_for_author"


def test_error_identity_and_nonproven_hard_rules_cannot_be_exchanged_for_a_pass():
    context = setup()
    same_count_new_error = PatchCandidate(
        ops=[{"op": "replace", "path": ROOT + "/dialogue_refs", "value": ["another_missing"]}]
    )
    report = review(context, candidate=same_count_new_error)
    assert report.status == "blocked"
    assert report.resolved_errors and report.introduced_errors
    assert set(report.resolved_errors).isdisjoint(report.introduced_errors)
    vacuous = context[2].model_copy(update={"formal_rules": []}).model_dump()
    vacuous["formal_rules"] = [
        {
            "quest_id": "q/one",
            "spec": {
                "id": "implication",
                "pattern": "AG",
                "expression": "not trigger or alive",
                "antecedent": "trigger",
            },
        }
    ]
    blocked_context = setup(blocked=True)
    withheld = blocked_context[2].model_dump()
    withheld["formal_rules"] = [
        {"quest_id": "q/one", "spec": {"id": "softlock", "pattern": "AG_EF_ENDING"}}
    ]
    for inputs, limits, expected in [
        (context, IntentContract.model_validate(vacuous), "vacuous"),
        (blocked_context, IntentContract.model_validate(withheld), "withheld"),
        (setup(initial_state={"alive": False}), None, "withheld"),
        (setup(no_logic=True), None, "unsupported"),
    ]:
        report = review(inputs, contract=limits)
        assert report.status == "blocked"
        assert report.formal_results[0]["after"]["status"] == expected


def test_author_goals_stay_withheld_and_confirmation_hash_and_new_version_are_explicit():
    context = setup(clean=True)
    contract = context[2].model_copy(update={"allowed_paths": [ROOT + "/objective"]})
    patch = PatchCandidate(
        ops=[{"op": "replace", "path": ROOT + "/objective", "value": "A clearer objective"}]
    )
    original = contract.model_dump()
    report = review(context, contract=contract, candidate=patch)
    assert report.status == "ready_for_author" and report.goal_status == "withheld"
    assert not report.resolved_errors  # No invented audit target for a natural-language request.
    pending = review(
        context, contract=contract.model_copy(update={"confirmed": False}), candidate=patch
    )
    assert pending.status == "needs_confirmation" and pending.candidate_bundle is None
    stale = review(
        context,
        contract=contract.model_copy(update={"baseline_artifact_hash": "0" * 64}),
        candidate=patch,
    )
    assert stale.status == "blocked" and stale.candidate_bundle is None
    next_contract = IntentContract.model_validate(
        {
            **original,
            "id": "new-author-contract",
            "version": 2,
            "supersedes_contract_id": contract.id,
        }
    )
    assert review(context, contract=next_contract, candidate=patch).status == "ready_for_author"
    assert contract.model_dump() == original
    with pytest.raises(ValidationError, match="修改目标原话"):
        IntentContract.model_validate({**original, "modification_goal": ""})
    with pytest.raises(ValidationError, match="supersedes"):
        IntentContract.model_validate({**original, "version": 2})
