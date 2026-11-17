"""Presentation isolation checks, not a model or human prose evaluation."""

from copy import deepcopy

import pytest

from owcopilot.app.actions.workflow import task_summary
from owcopilot.content.hash import content_hash
from owcopilot.workflow.mechanic_ir_v2 import compile_mechanic_design, compile_mechanic_dialogue
from owcopilot.workflow.models import TaskCreateInput, TaskInput, TaskRecord
from tests.domain.test_mechanic_ir_v2 import design_case


def test_display_metadata_does_not_change_generation_request_or_infer_author_origin():
    request = TaskInput(brief="开发验收输入，非作者评价，保留完整原文")
    task = TaskRecord(
        project="demo", request=request, base_version="v1", base_hash="base", base_bundle={}
    )
    before = content_hash(task.request)
    task.display_title = "封蜡之后"
    task.display_synopsis = "一封尚未送出的信"
    assert task_summary(task)["display_title"] == "封蜡之后"
    assert task_summary(task)["origin_kind"] == "unspecified"
    assert content_hash(task.request) == before
    assert task.request.brief.startswith("开发验收输入")
    old = task.model_dump(exclude={"display_title", "display_synopsis", "origin_kind"})
    assert task_summary(TaskRecord.model_validate(old))["display_title"] == "未命名故事"
    incoming = TaskCreateInput(brief=request.brief, display_title="新名称", origin_kind="author")
    generation = TaskInput.model_validate(
        incoming.model_dump(exclude={"display_title", "display_synopsis", "origin_kind"})
    )
    assert content_hash(generation) == before


def player_case():
    plan, quest, ir, prose = design_case()
    prose = deepcopy(prose)
    prose["schema_version"] = 3
    prose["stages"][-1]["options"][0]["ending_text"] = "The audience reads the report."
    prose["stages"][-1]["options"][1]["ending_text"] = "You seal the report in your desk."
    return plan, compile_mechanic_design(ir, plan, quest), prose


def test_player_channels_bind_real_choices_and_keep_legacy_ir_unchanged():
    plan, compilation, prose = player_case()
    new = compile_mechanic_dialogue(prose, compilation, plan, tree_id="t")
    metadata = new.tree.metadata["player_presentation_v1"]
    assert len(metadata["bindings"]) == 4
    assert len(metadata["automatic_branch_ids"]) == 2
    assert set(metadata["endings"].values()) == {
        "The audience reads the report.", "You seal the report in your desk."
    }
    for binding in metadata["bindings"]:
        actual = new.tree.nodes[binding["node_id"]].choices[binding["choice_index"]]
        assert actual.effects[0].var == binding["selector_variable"]
        assert actual.effects[0].value == binding["option_value"]
    legacy = design_case()[-1]
    old = compile_mechanic_dialogue(legacy, compilation, plan, tree_id="t")
    assert "player_presentation_v1" not in old.tree.metadata
    assert old.raw_ir["schema_version"] == 2
    assert "ending_text" not in old.raw_ir["stages"][-1]["options"][0]


def test_terminal_player_text_is_not_invented_from_mechanical_outcome():
    plan, compilation, prose = player_case()
    prose["stages"][-1]["options"][0].pop("ending_text")
    with pytest.raises(ValueError, match="玩家结局正文"):
        compile_mechanic_dialogue(prose, compilation, plan, tree_id="t")
