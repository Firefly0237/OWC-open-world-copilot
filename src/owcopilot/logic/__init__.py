# 原生任务逻辑/状态层：安全表达式语言 + 求值 + 确定性审计。
"""Native quest logic/state layer: a safe expression language, evaluation, and deterministic audit.

Single source of truth for quest logic (variables/conditions/effects/branches); serialized in the
content-bundle export. Reused by WS-E (playtest) via the evaluator and WorldState.
"""

from __future__ import annotations

from .audit import LogicIssue, audit_quest_logic
from .dialogue import (
    DialogueExploration,
    DialogueWalk,
    DialogueWalkStep,
    explore_dialogue_tree,
    walk_dialogue_tree,
)
from .dnf import Atom, DnfResult, atoms_in, compile_condition, evaluate_dnf
from .explore import DeadEnd, ExplorationReport, OutcomeReach, explore_quest
from .expr import LogicSyntaxError, parse_expr, refs_in, render_expr
from .semantics import LogicEvalError, WorldState, evaluate, type_errors
from .simulate import SimRun, SimStep, simulate_quest
from .temporal import (
    PropertyPattern,
    SpecStatus,
    TemporalResult,
    TemporalSpec,
    check_soft_lock,
    check_temporal_spec,
    cone_of_influence,
    parse_specs_from_quest,
)
from .xref import (
    FlagDeclaration,
    FlagFacts,
    FlagScope,
    FlagSite,
    FlagXref,
    SiteRole,
    UnparsableExpression,
    build_flag_xref,
)

__all__ = [
    "Atom",
    "DeadEnd",
    "DialogueExploration",
    "DialogueWalk",
    "DialogueWalkStep",
    "DnfResult",
    "ExplorationReport",
    "FlagDeclaration",
    "FlagFacts",
    "FlagScope",
    "FlagSite",
    "FlagXref",
    "LogicEvalError",
    "LogicIssue",
    "LogicSyntaxError",
    "OutcomeReach",
    "PropertyPattern",
    "SimRun",
    "SimStep",
    "SiteRole",
    "SpecStatus",
    "TemporalResult",
    "TemporalSpec",
    "UnparsableExpression",
    "WorldState",
    "atoms_in",
    "audit_quest_logic",
    "build_flag_xref",
    "check_soft_lock",
    "check_temporal_spec",
    "compile_condition",
    "cone_of_influence",
    "evaluate",
    "evaluate_dnf",
    "explore_dialogue_tree",
    "explore_quest",
    "parse_expr",
    "parse_specs_from_quest",
    "refs_in",
    "render_expr",
    "simulate_quest",
    "type_errors",
    "walk_dialogue_tree",
]
