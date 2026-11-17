"""Compare immutable candidate revisions through the real store and comparison operator."""

from types import SimpleNamespace

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.content.models import Branch, ContentBundle, Quest, QuestLogic, QuestStage
from owcopilot.workflow.models import Artifact, TaskInput, TaskRecord, WorkflowNode
from owcopilot.workflow.operators import execute_operator
from owcopilot.workflow.runtime import _comparison_baseline_input
from owcopilot.workflow.store import WorkflowStore


def quest(quest_id="q", outcome="old"):
    return Quest(
        id=quest_id,
        title=outcome,
        stages=[QuestStage(id="start", summary="Begin")],
        logic=QuestLogic(branches=[Branch(id="ending", from_stage="start", outcome=outcome)]),
    )


def setup(tmp_path, bundle=None):
    bundle = bundle or ContentBundle()
    task = TaskRecord(
        project="test",
        revision=5,
        request=TaskInput(brief="Refresh the report without regenerating content"),
        base_version="v1",
        base_hash=content_hash(bundle),
        base_bundle=bundle.model_dump(mode="json"),
    )
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    store.save_task(task)
    return bundle, task, store


def artifact(store, task, candidate, revision, kind="quest_with_logic"):
    value = Artifact(
        task_id=task.id,
        revision=revision,
        run_id="explicit-test-input",
        node_id="logic" if kind == "quest_with_logic" else "binding",
        kind=kind,
        payload={"quest": candidate.model_dump(mode="json")},
        input_hash="explicit-test-input",
        content_hash=content_hash(candidate),
    )
    store.append_artifact(value)
    return value


def compare(bundle, task, node, current, baseline):
    kind, report = execute_operator(
        node,
        task=task,
        project=SimpleNamespace(bundle=bundle),
        gateway=None,
        inputs={"draft_logic": current.payload, "comparison_baseline": baseline},
    )
    assert kind == "behavior_report"
    return report


def test_report_refresh_uses_previous_candidate_before_actual_logic_revision(tmp_path):
    bundle, task, store = setup(tmp_path)
    artifact(store, task, quest(outcome="v2"), 2)
    previous = artifact(store, task, quest(outcome="v3"), 3, "bound_dialogue")
    current = artifact(store, task, quest(outcome="v4"), 4)
    artifact(store, task, quest(outcome="same-revision-must-not-be-baseline"), 4, "bound_dialogue")
    artifact(store, task, quest("another", "unrelated"), 3)
    node = WorkflowNode(id="compare", operation="compare", depends_on=["logic"])

    baseline = _comparison_baseline_input(store, task, current, node)
    report = compare(bundle, task, node, current, baseline)
    assert task.revision == 5 and report["current_candidate"]["revision"] == 4
    assert report["baseline"]["source"] == "previous_candidate"
    assert report["baseline"]["source_kind"] == "bound_dialogue"
    assert report["baseline"]["artifact_id"] == previous.id
    assert report["baseline"]["revision"] == 3
    assert report["before_hash"] == content_hash(quest(outcome="v3"))
    assert report["after_hash"] == content_hash(quest(outcome="v4"))
    assert report["before_hash"] != report["after_hash"]
    assert "quest" in baseline  # This full payload is a normal, signed runtime input.


@pytest.mark.parametrize("mode", ["explicit_project", "same_id_project", "no_baseline"])
def test_project_baseline_priority_and_honest_absence(tmp_path, mode):
    before = quest("canonical" if mode == "explicit_project" else "q", "project")
    bundle, task, store = setup(
        tmp_path, ContentBundle(quests={before.id: before}) if mode != "no_baseline" else None
    )
    current = artifact(store, task, quest(outcome="v4"), 4)
    parameters = {"baseline_quest_id": before.id} if mode == "explicit_project" else {}
    node = WorkflowNode(
        id="compare", operation="compare", depends_on=["logic"], parameters=parameters
    )
    if mode == "explicit_project":
        artifact(store, task, quest(outcome="history-must-not-override-explicit"), 3)
    baseline = _comparison_baseline_input(store, task, current, node)
    report = compare(bundle, task, node, current, baseline)
    if mode == "no_baseline":
        assert report["status"] == "no_baseline"
        assert report["baseline"]["source"] == "unavailable"
        assert "before_hash" not in report
    else:
        assert report["baseline"]["source"] == "project"
        assert report["before_hash"] == content_hash(before)
    invalid = node.model_copy(update={"parameters": {"baseline_quest_id": "absent"}})
    with pytest.raises(ValueError, match="不能自动换用"):
        _comparison_baseline_input(store, task, current, invalid)
