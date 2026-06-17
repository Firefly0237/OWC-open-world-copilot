"""WS-M · B7 — AI-assisted quest logic drafting: the audit is the gate, review is final (HITL)."""

from __future__ import annotations

import pytest

from owcopilot.app.actions import (
    decide_review_action,
    draft_quest_logic_action,
    list_review_items_action,
)
from owcopilot.content.models import ContentBundle, Quest, QuestStage
from owcopilot.content.store import ContentStore


def _seed(root) -> None:
    ContentStore(root).save(
        ContentBundle(
            quests={
                "q_relief": Quest(
                    id="q_relief",
                    title="盐风驰援",
                    objective="护送补给穿越盐风峡谷",
                    stages=[
                        QuestStage(id="s1", summary="接受任务"),
                        QuestStage(id="s2", summary="穿越峡谷"),
                        QuestStage(id="s3", summary="抵达交付"),
                    ],
                )
            }
        )
    )


def test_offline_draft_loop_audit_catches_then_fixes(tmp_path) -> None:
    root = tmp_path / "content"
    _seed(root)
    res = draft_quest_logic_action(root, quest_id="q_relief", intent="先见长老", llm_mode="offline")
    # the deterministic audit drove the loop: round 0 flagged the undefined var, round 1 went clean
    verdicts = [(s["round"], s["verdict"], s["blocking_count"]) for s in res["refine_trail"]]
    assert verdicts[0][1] == "revise" and verdicts[0][2] >= 1
    assert verdicts[-1][1] == "pass" and verdicts[-1][2] == 0
    assert res["logic_issues"] == []  # final draft is audit-clean
    assert res["review_item_id"]  # queued, not auto-landed
    # canon is untouched until a human approves
    assert ContentStore(root).load().quests["q_relief"].logic is None


def test_accept_applies_logic_to_existing_quest(tmp_path) -> None:
    root = tmp_path / "content"
    _seed(root)
    res = draft_quest_logic_action(root, quest_id="q_relief", llm_mode="offline")
    item_id = res["review_item_id"]
    out = decide_review_action(root, item_id=item_id, decision="accepted", operator="审校员")
    assert out["written_ref"] == "quest:q_relief"
    landed = ContentStore(root).load().quests["q_relief"]
    assert landed.logic is not None  # only the logic was applied
    assert landed.title == "盐风驰援"  # everything else untouched
    assert [v.id for v in landed.logic.variables]  # the drafted variables are there


def test_reject_leaves_quest_without_logic(tmp_path) -> None:
    root = tmp_path / "content"
    _seed(root)
    res = draft_quest_logic_action(root, quest_id="q_relief", llm_mode="offline")
    decide_review_action(
        root, item_id=res["review_item_id"], decision="rejected", operator="审校员"
    )
    assert ContentStore(root).load().quests["q_relief"].logic is None


def test_draft_unknown_quest_is_clean_error(tmp_path) -> None:
    root = tmp_path / "content"
    _seed(root)
    with pytest.raises(ValueError, match="任务不存在"):
        draft_quest_logic_action(root, quest_id="ghost", llm_mode="offline")


def test_logic_draft_item_visible_in_review_queue(tmp_path) -> None:
    root = tmp_path / "content"
    _seed(root)
    draft_quest_logic_action(root, quest_id="q_relief", llm_mode="offline")
    items = list_review_items_action(root)["items"]
    assert any(it["item_type"] == "quest_logic_draft" for it in items)
