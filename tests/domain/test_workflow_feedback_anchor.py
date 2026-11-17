"""Exact production feedback resolution, using actual preview sessions and no model calls."""

from copy import deepcopy

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.content.models import DialogueTree, Quest
from owcopilot.logic.stepper import advance_preview, start_preview
from owcopilot.workflow.anchors import FeedbackAnchor, resolve_feedback_anchor
from owcopilot.workflow.models import Artifact, TaskInput, TaskRecord


def objects():
    task = TaskRecord(
        project="anchor-contract",
        request=TaskInput(brief="改变语气，保留选择后果"),
        base_version="v1",
        base_hash=content_hash({}),
        base_bundle={},
    )
    payload = {
        "quest": {
            "id": "current_quest",
            "title": "归还口信",
            "objective": "在众人面前说明误会",
            "stages": [{"id": "explain", "summary": "说明未转达的后半句"}],
            "logic": {},
        },
        "tree": {
            "id": "current_tree",
            "quest_id": "current_quest",
            "root_node": "door/~",
            "nodes": {
                "door/~": {
                    "id": "door/~",
                    "text": "我没有把整句话说完。",
                    "choices": [{"text": "告诉我后半句。", "next_node": "tail"}],
                },
                "tail": {"id": "tail", "text": "我没有把整句话说完。"},
            },
        },
        "metadata": {"summary": "不能当作可编辑正文"},
    }
    artifact = Artifact(
        task_id=task.id,
        revision=task.revision,
        run_id="source-run",
        node_id="bind_dialogue",
        kind="bound_dialogue",
        payload=payload,
        input_hash=content_hash({}),
        content_hash=content_hash(payload),
    )
    return task, artifact


def anchor_for(artifact, path, **kwargs):
    return FeedbackAnchor(
        artifact_id=artifact.id, artifact_hash=artifact.content_hash, path=path, **kwargs
    )


def test_exact_existing_text_routes_bound_dialogue_to_its_writer_without_mutation():
    task, artifact = objects()
    before = deepcopy((task.model_dump(mode="json"), artifact.model_dump(mode="json")))
    for path, operation, text in (
        ("/quest/objective", "draft_quest", "在众人面前说明误会"),
        ("/quest/stages/0/summary", "draft_quest", "说明未转达的后半句"),
        ("/tree/nodes/door~1~0/text", "draft_dialogue", "我没有把整句话说完。"),
        ("/tree/nodes/door~1~0/choices/0/text", "draft_dialogue", "告诉我后半句。"),
    ):
        result = resolve_feedback_anchor(task, artifact, anchor_for(artifact, path))
        assert result["operation"] == operation
        assert result["target_text"] == text
        assert result["target_path"] == path
        assert result["source_hash"] == artifact.content_hash
        assert result["artifact_revision"] == artifact.revision
    assert (task.model_dump(mode="json"), artifact.model_dump(mode="json")) == before


def test_changed_hash_wrong_artifact_and_other_task_are_rejected():
    task, artifact = objects()
    anchor = anchor_for(artifact, "/quest/objective")
    wrong_hash = anchor.model_copy(update={"artifact_hash": "0" * 64})
    with pytest.raises(ValueError, match="hash"):
        resolve_feedback_anchor(task, artifact, wrong_hash)
    changed = artifact.model_copy(deep=True)
    changed.payload["quest"]["objective"] = "相似的新目标不能代替原文"
    with pytest.raises(ValueError, match="hash"):
        resolve_feedback_anchor(task, changed, anchor)
    with pytest.raises(ValueError, match="ID"):
        resolve_feedback_anchor(task, artifact.model_copy(update={"id": "other"}), anchor)
    with pytest.raises(ValueError, match="不属于"):
        resolve_feedback_anchor(task.model_copy(update={"id": "other-task"}), artifact, anchor)


def test_non_text_metadata_missing_and_noncanonical_pointer_paths_are_rejected():
    task, artifact = objects()
    for path in (
        "/quest",
        "/metadata/summary",
        "/quest/logic/precondition",
        "/quest/stages/01/summary",
        "/quest/stages/-1/summary",
        "/quest/stages/99/summary",
        "/tree/nodes/missing/text",
        "/tree/nodes/door~2/text",
        "/tree/nodes/door~1~0/choices/0/next_node",
    ):
        with pytest.raises(ValueError):
            resolve_feedback_anchor(task, artifact, anchor_for(artifact, path))
    raw = anchor_for(artifact, "/quest/title").model_dump()
    with pytest.raises(ValueError):
        FeedbackAnchor.model_validate({**raw, "unknown": "ignored permissions"})
    with pytest.raises(ValueError):
        FeedbackAnchor.model_validate({**raw, "preview_id": "preview", "action_seq": True})


def test_preview_anchor_uses_current_source_cursor_and_action_sequence():
    task, artifact = objects()
    session = start_preview(
        quest=Quest.model_validate(artifact.payload["quest"]),
        dialogue_tree=DialogueTree.model_validate(artifact.payload["tree"]),
    )
    preview = {
        "task_id": task.id,
        "project": task.project,
        "revision": task.revision,
        "artifact_id": artifact.id,
        "session": session.model_dump(mode="json"),
    }
    anchor = anchor_for(
        artifact,
        "/tree/nodes/door~1~0/choices/0/text",
        preview_id=session.id,
        action_seq=session.action_seq,
    )
    result = resolve_feedback_anchor(task, artifact, anchor, preview)
    assert result["operation"] == "draft_dialogue"
    assert result["target_text"] == "告诉我后半句。"
    for changed in (
        {**preview, "task_id": "other-task"},
        {**preview, "revision": task.revision + 1},
        {**preview, "artifact_id": "similar-artifact"},
        {**preview, "session": {**preview["session"], "action_seq": 1}},
    ):
        with pytest.raises(ValueError):
            resolve_feedback_anchor(task, artifact, anchor, changed)
    later = advance_preview(session, action_id="choose", choice_id="door/~:0", expected_seq=0)
    # Identical text at another node must never substitute for the original location.
    line_anchor = anchor_for(
        artifact, "/tree/nodes/door~1~0/text", preview_id=later.id, action_seq=later.action_seq
    )
    with pytest.raises(ValueError, match="当前节点"):
        resolve_feedback_anchor(
            task, artifact, line_anchor, {**preview, "session": later.model_dump(mode="json")}
        )
