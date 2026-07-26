# DialogueUngatedSiblingChoiceRule 的判据测试：正例 + 四条误报边界（全兄弟都有门禁 / 变量本树未建立
#   / 单选项节点 / 语法错条件）。这条规则靠"作者自己给某兄弟加了门禁"来推断遗漏，误报即失效，
#   故负例比正例更重要。
"""Tests for ``DialogueUngatedSiblingChoiceRule`` (audit/rules/dialogue_rules.py).

The rule infers a gap from the author's own gate, so its value rests entirely on not crying wolf.
Four of the five cases below are false-positive boundaries.
"""

from __future__ import annotations

from owcopilot.audit.context import AuditContext
from owcopilot.audit.rules.dialogue_rules import DialogueUngatedSiblingChoiceRule
from owcopilot.content.models import (
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Effect,
)

_RULE = DialogueUngatedSiblingChoiceRule()


def _run(nodes: dict[str, DialogueNode]) -> list:
    tree = DialogueTree(id="dt1", root_node="n1", nodes=nodes)
    bundle = ContentBundle(dialogue_trees={"dt1": tree})
    return list(_RULE.check(AuditContext.from_bundle(bundle)))


def _setter_node(var: str = "said_read_books", target: str = "n2") -> DialogueNode:
    """n1: 一个把 var 置 true 的选项 —— 让该变量成为"本树建立过"的变量。"""
    return DialogueNode(
        id="n1",
        choices=[
            DialogueChoice(
                text="说读过书", next_node=target, effects=[Effect(var=var, op="set", value=True)]
            )
        ],
    )


def test_gated_choice_with_ungated_sibling_is_flagged() -> None:
    findings = _run(
        {
            "n1": _setter_node(),
            "n2": DialogueNode(
                id="n2",
                choices=[
                    DialogueChoice(text="也曾读过几年书", condition="said_read_books == true"),
                    DialogueChoice(text="不曾读书"),  # 相斥却无门禁 —— 就是要抓的形态
                ],
            ),
        }
    )
    assert len(findings) == 1
    assert findings[0].rule_code == "DIALOGUE_UNGATED_SIBLING_CHOICE"
    paths = {evidence.path for evidence in findings[0].evidence}
    assert paths == {"nodes/n2/choices/0", "nodes/n2/choices/1"}


def test_all_siblings_gated_is_not_flagged() -> None:
    """作者已经给每条兄弟都设了门禁 —— 分歧被处理过，不得报。"""
    findings = _run(
        {
            "n1": _setter_node(),
            "n2": DialogueNode(
                id="n2",
                choices=[
                    DialogueChoice(text="也曾读过几年书", condition="said_read_books == true"),
                    DialogueChoice(text="不曾读书", condition="said_read_books == false"),
                ],
            ),
        }
    )
    assert findings == []


def test_variable_never_set_in_tree_is_not_flagged() -> None:
    """门禁引用的变量本树从未 set —— 那是外部状态，本树无从矛盾，不得报。"""
    findings = _run(
        {
            "n1": DialogueNode(id="n1", choices=[DialogueChoice(text="进门", next_node="n2")]),
            "n2": DialogueNode(
                id="n2",
                choices=[
                    DialogueChoice(text="出示信物", condition="has_token == true"),
                    DialogueChoice(text="空手而来"),
                ],
            ),
        }
    )
    assert findings == []


def test_single_choice_node_is_not_flagged() -> None:
    """只有一个选项就无所谓"兄弟"，不得报。"""
    findings = _run(
        {
            "n1": _setter_node(),
            "n2": DialogueNode(
                id="n2",
                choices=[
                    DialogueChoice(text="也曾读过几年书", condition="said_read_books == true")
                ],
            ),
        }
    )
    assert findings == []


def test_unparsable_condition_is_not_flagged() -> None:
    """条件语法错时无法证明它门禁了什么（该错误由 DialogueChoiceConditionRule 单独报），
    本规则必须闭嘴而不是乱报。"""
    findings = _run(
        {
            "n1": _setter_node(),
            "n2": DialogueNode(
                id="n2",
                choices=[
                    DialogueChoice(text="坏条件", condition="said_read_books =="),
                    DialogueChoice(text="不曾读书"),
                ],
            ),
        }
    )
    assert findings == []
