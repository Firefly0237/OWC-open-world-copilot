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
from .explore import DeadEnd, ExplorationReport, OutcomeReach, explore_quest
from .expr import LogicSyntaxError, parse_expr, refs_in, render_expr
from .semantics import LogicEvalError, WorldState, evaluate, type_errors
from .simulate import SimRun, SimStep, simulate_quest

__all__ = [
    "DeadEnd",
    "DialogueExploration",
    "DialogueWalk",
    "DialogueWalkStep",
    "ExplorationReport",
    "LogicEvalError",
    "LogicIssue",
    "LogicSyntaxError",
    "OutcomeReach",
    "SimRun",
    "SimStep",
    "WorldState",
    "audit_quest_logic",
    "evaluate",
    "explore_dialogue_tree",
    "explore_quest",
    "parse_expr",
    "refs_in",
    "render_expr",
    "simulate_quest",
    "type_errors",
    "walk_dialogue_tree",
]
