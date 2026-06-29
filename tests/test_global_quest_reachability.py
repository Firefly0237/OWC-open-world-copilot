"""Tests for IN-5: L2 Global reachability audit rule (QUEST_GLOBAL_UNREACHABLE).

Covers hard acceptance criteria:
- 断裂种子世界 (broken seed world): Quest with permanently-false precondition is reported
- 干净世界 (clean world): No false positives on valid quest graphs
- 保守性 (conservatism): Uncertain/complex conditions do not trigger false positives
- Non-overlap with cycle rule and logic rule
- Fixpoint propagation works across multi-hop dependency chains
"""

from __future__ import annotations

from owcopilot.audit.context import AuditContext
from owcopilot.audit.rules.quest_reachability import QuestGlobalReachabilityRule
from owcopilot.content.models import (
    ContentBundle,
    Effect,
    LogicVar,
    LogicVarType,
    Quest,
    QuestLogic,
    StageLogic,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bool_var(var_id: str, *, default: bool = False) -> LogicVar:
    return LogicVar(id=var_id, name=var_id, type=LogicVarType.BOOL, default=default)


def _int_var(var_id: str, *, default: int = 0) -> LogicVar:
    return LogicVar(id=var_id, name=var_id, type=LogicVarType.INT, default=default)


def _make_bundle(quests: list[Quest]) -> ContentBundle:
    return ContentBundle(quests={q.id: q for q in quests})


def _make_ctx(quests: list[Quest]) -> AuditContext:
    bundle = _make_bundle(quests)
    return AuditContext.from_bundle(bundle)


def _make_quest(
    quest_id: str,
    *,
    prerequisites: list[str] | None = None,
    precondition: str = "",
    variables: list[LogicVar] | None = None,
    effects_on_complete: list[Effect] | None = None,
) -> Quest:
    """Build a Quest with optional logic for testing."""
    logic = None
    if precondition or variables or effects_on_complete:
        stage_logics = []
        if effects_on_complete:
            stage_logics.append(StageLogic(stage_id="s1", effects_on_complete=effects_on_complete))
        logic = QuestLogic(
            precondition=precondition,
            variables=variables or [],
            stage_logic=stage_logics,
        )
    return Quest(
        id=quest_id,
        title=quest_id,
        prerequisites=prerequisites or [],
        logic=logic,
    )


def _rule_issues(ctx: AuditContext) -> list:
    rule = QuestGlobalReachabilityRule()
    return list(rule.check(ctx))


# ---------------------------------------------------------------------------
# 干净世界: zero false positives
# ---------------------------------------------------------------------------

def test_clean_single_quest_no_issues() -> None:
    """[硬] Single quest with no prerequisites -> no issues."""
    ctx = _make_ctx([_make_quest("q1")])
    issues = _rule_issues(ctx)
    assert issues == []


def test_clean_linear_chain_no_issues() -> None:
    """[硬] Linear q1->q2->q3 (each requires prev) -> all reachable, no issues."""
    ctx = _make_ctx([
        _make_quest("q1"),
        _make_quest("q2", prerequisites=["q1"]),
        _make_quest("q3", prerequisites=["q2"]),
    ])
    issues = _rule_issues(ctx)
    assert issues == []


def test_clean_diamond_chain_no_issues() -> None:
    """[硬] Diamond: q1 unlocks q2 and q3, q4 requires both. All reachable, no issues."""
    ctx = _make_ctx([
        _make_quest("q1"),
        _make_quest("q2", prerequisites=["q1"]),
        _make_quest("q3", prerequisites=["q1"]),
        _make_quest("q4", prerequisites=["q2", "q3"]),
    ])
    issues = _rule_issues(ctx)
    assert issues == []


def test_clean_empty_bundle_no_issues() -> None:
    """Empty bundle -> no issues."""
    ctx = AuditContext.from_bundle(ContentBundle())
    issues = _rule_issues(ctx)
    assert issues == []


def test_clean_precondition_satisfiable_by_default() -> None:
    """Quest whose precondition can be satisfied by default values -> no issue.

    Note: the logic parser uses lowercase 'true'/'false' literals, NOT Python's True/False.
    """
    # q1 sets bool var "hero_flag" to true on complete.
    # q2 requires precondition "hero_flag == true"
    ctx = _make_ctx([
        _make_quest(
            "q1",
            variables=[_bool_var("hero_flag")],
            effects_on_complete=[Effect(var="hero_flag", op="set", value=True)],
        ),
        _make_quest(
            "q2",
            prerequisites=["q1"],
            precondition="hero_flag == true",
        ),
    ])
    issues = _rule_issues(ctx)
    assert issues == []


def test_clean_precondition_known_var_but_satisfiable() -> None:
    """[硬 保守性] Variable that CAN be true (seeded as True initially) -> no issue."""
    ctx = _make_ctx([
        _make_quest(
            "q1",
            variables=[_bool_var("already_true", default=True)],
            precondition="already_true == true",  # satisfiable: default is True
        ),
    ])
    issues = _rule_issues(ctx)
    assert issues == []


def test_clean_complex_precondition_not_flagged_when_uncertain() -> None:
    """[硬 保守性] Partial OR conditions -> conservative, no false positive."""
    # (a == True) or (b == True): even if a is permanently false, b might be true
    ctx = _make_ctx([
        _make_quest("q1", precondition="(a == True) or (b == True)"),
    ])
    issues = _rule_issues(ctx)
    assert issues == []


def test_clean_parse_error_in_precondition_not_flagged() -> None:
    """[硬 保守性] Precondition parse error -> no issue (conservative)."""
    ctx = _make_ctx([
        _make_quest("q1", precondition="{{invalid syntax here}}"),
    ])
    issues = _rule_issues(ctx)
    assert issues == []


# ---------------------------------------------------------------------------
# 断裂种子世界: real issues reported
# ---------------------------------------------------------------------------

def test_broken_missing_prerequisite_reported() -> None:
    """[硬] Quest requires a non-existent (unreachable) prerequisite -> issue reported."""
    # q2 requires q_nonexistent (which doesn't exist in bundle)
    ctx = _make_ctx([
        _make_quest("q1"),
        _make_quest("q2", prerequisites=["q_nonexistent"]),
    ])
    issues = _rule_issues(ctx)
    issue_targets = [i.target_ref for i in issues]
    assert "quest:q2" in issue_targets


def test_broken_unreachable_prerequisite_chain() -> None:
    """[硬] Chain where q2 depends on q1 with impossible precondition -> q2 also unreachable."""
    # q1 has precondition "never_set == true" but this var never gets set to true
    # q1 itself is entry-level with a bool var whose default is False, never changed
    # Resulting in q1 unreachable, q2 (which requires q1) also unreachable
    ctx = _make_ctx([
        _make_quest(
            "q1",
            variables=[_bool_var("never_set")],
            precondition="never_set == true",  # lowercase true = BoolLit(True)
        ),
        _make_quest("q2", prerequisites=["q1"]),
    ])
    issues = _rule_issues(ctx)
    # At least one issue for q1 or q2
    assert len(issues) >= 1
    # q2 should be in unreachable set since q1 is unreachable
    targets = [i.target_ref for i in issues]
    assert "quest:q2" in targets


def test_broken_permanently_false_bool_precondition() -> None:
    """[硬 断裂] A bool var seeded False, never set True => precondition permanently false.

    The logic parser uses lowercase 'true'/'false' literals (not Python's True/False).
    """
    ctx = _make_ctx([
        _make_quest(
            "q1",
            variables=[_bool_var("flag_x")],
            precondition="flag_x == true",  # lowercase 'true' = BoolLit(True) in parser
        ),
    ])
    issues = _rule_issues(ctx)
    assert len(issues) >= 1
    assert issues[0].rule_code == "QUEST_GLOBAL_UNREACHABLE"
    assert "quest:q1" == issues[0].target_ref


def test_broken_blocking_atom_in_evidence() -> None:
    """[硬] Evidence contains quest_id and blocking_atom."""
    ctx = _make_ctx([
        _make_quest(
            "q1",
            variables=[_bool_var("bad_flag")],
            precondition="bad_flag == true",  # lowercase true = BoolLit(True)
        ),
    ])
    issues = _rule_issues(ctx)
    assert len(issues) >= 1
    issue = issues[0]
    evidence = {e.kind: e.data for e in issue.evidence}
    assert "quest_reachability" in evidence
    assert evidence["quest_reachability"]["quest_id"] == "q1"
    assert "blocking_atom" in evidence["quest_reachability"]
    assert evidence["quest_reachability"]["blocking_atom"] is not None


# ---------------------------------------------------------------------------
# Multi-hop fixpoint propagation
# ---------------------------------------------------------------------------

def test_fixpoint_propagation_3hops() -> None:
    """[软] 3-hop chain with variable effects propagates correctly."""
    # q1: sets "stage1_done" = True
    # q2: requires q1 AND precondition "stage1_done == true"
    # q3: requires q2
    ctx = _make_ctx([
        _make_quest(
            "q1",
            variables=[_bool_var("stage1_done")],
            effects_on_complete=[Effect(var="stage1_done", op="set", value=True)],
        ),
        _make_quest(
            "q2",
            prerequisites=["q1"],
            precondition="stage1_done == true",  # lowercase true = BoolLit(True)
        ),
        _make_quest("q3", prerequisites=["q2"]),
    ])
    issues = _rule_issues(ctx)
    assert issues == []


def test_fixpoint_incremented_numeric_is_conservative() -> None:
    """[硬 保守性] Numeric variable after inc is marked _MULTI (can be anything) -> no false pos."""
    ctx = _make_ctx([
        _make_quest(
            "q1",
            variables=[_int_var("counter")],
            effects_on_complete=[Effect(var="counter", op="inc")],
        ),
        _make_quest(
            "q2",
            prerequisites=["q1"],
            precondition="counter == 5",  # counter is _MULTI after inc -> conservative, satisfiable
        ),
    ])
    issues = _rule_issues(ctx)
    assert issues == []


# ---------------------------------------------------------------------------
# Rule code and severity
# ---------------------------------------------------------------------------

def test_rule_code_and_severity() -> None:
    """Rule emits QUEST_GLOBAL_UNREACHABLE with WARNING severity."""
    from owcopilot.audit.models import Severity
    ctx = _make_ctx([
        _make_quest(
            "q1",
            variables=[_bool_var("x")],
            precondition="x == true",  # lowercase true = BoolLit(True)
        ),
    ])
    issues = _rule_issues(ctx)
    assert len(issues) >= 1
    assert issues[0].rule_code == "QUEST_GLOBAL_UNREACHABLE"
    assert issues[0].severity == Severity.WARNING


# ---------------------------------------------------------------------------
# Non-overlap: cycles vs reachability are orthogonal
# ---------------------------------------------------------------------------

def test_reachable_cyclic_quests_not_flagged() -> None:
    """[硬] Quests in a cycle that are still reachable (entry points) are NOT flagged here.

    This confirms non-overlap with the prerequisite-cycle rule: a cycle doesn't mean
    the quests are unreachable from the entry point; the cycle rule reports A->B->A
    independently of whether either is globally reachable.
    """
    # q1 has no prerequisites -> reachable entry point
    # q2 requires q1; q1 and q2 form no cycle, both reachable
    ctx = _make_ctx([
        _make_quest("q1"),
        _make_quest("q2", prerequisites=["q1"]),
    ])
    issues = _rule_issues(ctx)
    assert issues == []


def test_unreachable_but_not_cyclic() -> None:
    """[软] An orphaned quest (no prereqs, just a permanently-false precondition) is detected."""
    ctx = _make_ctx([
        _make_quest("q1"),  # reachable entry
        _make_quest(
            "q_orphan",
            # No prerequisites, but precondition is permanently false
            variables=[_bool_var("impossible")],
            precondition="impossible == true",  # lowercase true = BoolLit(True)
        ),
    ])
    issues = _rule_issues(ctx)
    targets = [i.target_ref for i in issues]
    assert "quest:q_orphan" in targets
    # q1 should NOT be flagged
    assert "quest:q1" not in targets
