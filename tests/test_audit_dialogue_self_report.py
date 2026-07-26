# DialogueSelfReportInconsistencyRule 的判据测试：正例/门禁负例/不可达负例/异变量负例/环形安全。
"""Tests for ``DialogueSelfReportInconsistencyRule`` (audit/rules/dialogue_rules.py).

Five cases, matching the rule's own criteria exactly:
  1. positive  -- A sets v=true with no gate, B (reachable, ungated) sets v=false -> one finding.
  2. negative  -- B's condition mentions v -> no finding (the author already gated on it).
  3. negative  -- B is not reachable from A -> no finding.
  4. negative  -- B sets a DIFFERENT variable -> no finding.
  5. cyclic tree -- the reachability search must terminate and still find the real hit.
"""

from __future__ import annotations

from owcopilot.audit.context import AuditContext
from owcopilot.audit.rules.dialogue_rules import DialogueSelfReportInconsistencyRule
from owcopilot.content.models import (
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Effect,
)

_RULE = DialogueSelfReportInconsistencyRule()


def _tree(nodes: dict[str, DialogueNode], root: str = "n1") -> DialogueTree:
    return DialogueTree(id="dt1", root_node=root, nodes=nodes)


def _bundle(tree: DialogueTree) -> ContentBundle:
    return ContentBundle(dialogue_trees={"dt1": tree})


def test_reachable_ungated_flip_is_flagged() -> None:
    tree = _tree(
        {
            "n1": DialogueNode(
                id="n1",
                choices=[
                    DialogueChoice(
                        text="A",
                        next_node="n2",
                        effects=[Effect(var="said_read_books", op="set", value=True)],
                    )
                ],
            ),
            "n2": DialogueNode(id="n2", next_node="n3"),
            "n3": DialogueNode(
                id="n3",
                choices=[
                    DialogueChoice(
                        text="B",
                        next_node=None,
                        effects=[Effect(var="said_read_books", op="set", value=False)],
                    )
                ],
            ),
        }
    )
    issues = list(_RULE.check(AuditContext.from_bundle(_bundle(tree))))
    assert len(issues) == 1
    issue = issues[0]
    assert issue.rule_code == "DIALOGUE_SELF_REPORT_INCONSISTENCY"
    assert "said_read_books" in issue.message
    assert len(issue.evidence) == 2
    assert issue.evidence[0].path == "nodes/n1/choices/0"
    assert issue.evidence[1].path == "nodes/n3/choices/0"


def test_gated_flip_is_not_flagged() -> None:
    tree = _tree(
        {
            "n1": DialogueNode(
                id="n1",
                choices=[
                    DialogueChoice(
                        text="A",
                        next_node="n2",
                        effects=[Effect(var="said_read_books", op="set", value=True)],
                    )
                ],
            ),
            "n2": DialogueNode(id="n2", next_node="n3"),
            "n3": DialogueNode(
                id="n3",
                choices=[
                    DialogueChoice(
                        text="B, gated on the very flag A set",
                        next_node=None,
                        condition="said_read_books == false",
                        effects=[Effect(var="said_read_books", op="set", value=False)],
                    )
                ],
            ),
        }
    )
    issues = list(_RULE.check(AuditContext.from_bundle(_bundle(tree))))
    assert issues == []


def test_unreachable_flip_is_not_flagged() -> None:
    tree = _tree(
        {
            "n1": DialogueNode(
                id="n1",
                choices=[
                    DialogueChoice(
                        text="A",
                        next_node="n2",
                        effects=[Effect(var="said_read_books", op="set", value=True)],
                    )
                ],
            ),
            "n2": DialogueNode(id="n2"),  # dead end -- A leads nowhere further
            "n_isolated": DialogueNode(
                id="n_isolated",
                choices=[
                    DialogueChoice(
                        text="B, but on a branch A never reaches",
                        next_node=None,
                        effects=[Effect(var="said_read_books", op="set", value=False)],
                    )
                ],
            ),
        }
    )
    issues = list(_RULE.check(AuditContext.from_bundle(_bundle(tree))))
    assert issues == []


def test_different_variable_is_not_flagged() -> None:
    tree = _tree(
        {
            "n1": DialogueNode(
                id="n1",
                choices=[
                    DialogueChoice(
                        text="A",
                        next_node="n2",
                        effects=[Effect(var="said_read_books", op="set", value=True)],
                    )
                ],
            ),
            "n2": DialogueNode(
                id="n2",
                choices=[
                    DialogueChoice(
                        text="B, sets an unrelated variable",
                        next_node=None,
                        effects=[Effect(var="baoyu_impression", op="inc", value=1)],
                    )
                ],
            ),
        }
    )
    issues = list(_RULE.check(AuditContext.from_bundle(_bundle(tree))))
    assert issues == []


def test_cyclic_tree_terminates_and_still_finds_the_hit() -> None:
    # n1 --choice A--> n2 --next_node--> n1 (cycle back). Choice B lives on n1 itself, at a
    # DIFFERENT index than A, and is only reachable by looping back through the cycle.
    tree = _tree(
        {
            "n1": DialogueNode(
                id="n1",
                choices=[
                    DialogueChoice(
                        text="A",
                        next_node="n2",
                        effects=[Effect(var="said_read_books", op="set", value=True)],
                    ),
                    DialogueChoice(
                        text="B, reachable only via the cycle back to n1",
                        next_node=None,
                        effects=[Effect(var="said_read_books", op="set", value=False)],
                    ),
                ],
            ),
            "n2": DialogueNode(id="n2", next_node="n1"),
        }
    )
    issues = list(_RULE.check(AuditContext.from_bundle(_bundle(tree))))
    assert len(issues) == 1
    assert issues[0].evidence[0].path == "nodes/n1/choices/0"
    assert issues[0].evidence[1].path == "nodes/n1/choices/1"
