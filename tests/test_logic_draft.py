"""WS-M · B7 — AI-assisted quest logic drafting: the audit is the gate, review is final (HITL)."""

from __future__ import annotations

import json

import pytest

from owcopilot.app.actions import (
    decide_review_action,
    draft_quest_logic_action,
    list_review_items_action,
)
from owcopilot.content.models import ContentBundle, Quest, QuestStage
from owcopilot.content.store import ContentStore
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.structured import StructuredOutputError
from owcopilot.logic.draft import draft_quest_logic


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


# --- T9: the generate step now runs through the structured-output layer -------------------------
#
# draft_quest_logic's inner generate was migrated from a hand-rolled extract_json + model_validate +
# manual JSON-only retry to complete_structured(model_cls=QuestLogic). QuestLogic is a single model
# with no lossy pre-coercion (its list fields all default to []), so the layer owns
# run->validate->repair, quoting the specific pydantic error on the one re-ask (sharper than the old
# generic nudge). These drive the site directly with a scripted provider through a real LLMGateway
# (the test_gateway/test_structured pattern) at $0. tier is unset, so StaticRouter's default_tier
# ("cheap") is the tier the double registers under.


def _logic_quest() -> Quest:
    return Quest(
        id="q_relief",
        title="盐风驰援",
        objective="护送补给穿越盐风峡谷",
        stages=[
            QuestStage(id="s1", summary="接受任务"),
            QuestStage(id="s2", summary="穿越峡谷"),
            QuestStage(id="s3", summary="抵达交付"),
        ],
    )


class _ScriptedLogicProvider:
    """Canned replies by call index; records each turn's user message (test_structured pattern)."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.calls = 0
        self.seen_users: list[str] = []

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        self.seen_users.append(user)
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return reply, 1, 1


# a clean draft the audit passes, and a parseable-but-flawed one (undeclared branch var) it flags
_CLEAN_LOGIC = json.dumps(
    {"variables": [{"id": "has_key", "name": "持有钥匙", "type": "bool", "default": False}]}
)
_FLAWED_LOGIC = json.dumps(
    {"branches": [{"id": "b1", "from_stage": "s1", "condition": "needs_key", "to_stage": "s2"}]}
)


def test_structured_generate_returns_validated_logic_first_try() -> None:
    prov = _ScriptedLogicProvider([_CLEAN_LOGIC])
    res = draft_quest_logic(
        gateway=LLMGateway(providers={"cheap": prov}), quest=_logic_quest(), max_rounds=0
    )
    assert prov.calls == 1  # one provider call, no wasted repair — equivalence with the old path
    assert [v.id for v in res.logic.variables] == ["has_key"]
    assert res.trail == []  # max_rounds=0 → no refine round
    assert res.issues == []  # the clean draft audits clean


def test_structured_repair_reasks_with_specific_error_then_succeeds() -> None:
    # first reply has a wrong-typed field (variables must be a list); the one re-ask carries the
    # concrete pydantic error + a snippet of the bad output and recovers a valid draft.
    prov = _ScriptedLogicProvider(['{"variables": "oops"}', _CLEAN_LOGIC])
    res = draft_quest_logic(
        gateway=LLMGateway(providers={"cheap": prov}), quest=_logic_quest(), max_rounds=0
    )
    assert prov.calls == 2
    assert [v.id for v in res.logic.variables] == ["has_key"]
    repair_user = prov.seen_users[1]
    assert "variables" in repair_user  # the specific failing field, not a generic message
    assert '"oops"' in repair_user  # a snippet of the bad reply is echoed back


def test_structured_exhaustion_raises_honest_value_error() -> None:
    # model never returns parseable logic: complete_structured exhausts its one repair and raises
    # StructuredOutputError, which generate folds into None → draft_quest_logic raises the honest
    # ValueError (never a fabricated default). Same failure surface as the pre-migration None→raise.
    prov = _ScriptedLogicProvider(["not json at all"])
    with pytest.raises(ValueError, match="未能生成可解析") as excinfo:
        draft_quest_logic(
            gateway=LLMGateway(providers={"cheap": prov}), quest=_logic_quest(), max_rounds=0
        )
    assert not isinstance(excinfo.value, StructuredOutputError)  # folded, not leaked raw
    assert prov.calls == 2  # initial call + one repair re-ask, then give up


def test_structured_refine_round_keeps_last_draft_when_unparsable() -> None:
    # the salvage this migration must preserve: a refine round whose reply won't parse keeps the
    # last good draft (flagged auto_review_incomplete), never crashing or fabricating a default.
    prov = _ScriptedLogicProvider([_FLAWED_LOGIC, "not json", "not json"])
    res = draft_quest_logic(
        gateway=LLMGateway(providers={"cheap": prov}), quest=_logic_quest(), max_rounds=1
    )
    assert prov.calls == 3  # initial (1) + the refine round's repair-exhausted generate (2)
    assert res.auto_review_incomplete is True
    # the flawed-but-parsed initial draft is kept verbatim, not replaced by a fabricated default
    assert [b.condition for b in res.logic.branches] == ["needs_key"]
    assert any(i.code == "LOGIC_UNDEFINED_VAR" for i in res.issues)
