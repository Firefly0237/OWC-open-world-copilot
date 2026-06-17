"""Native quest logic/state layer: a safe expression language, evaluation, and deterministic audit.

Single source of truth for quest logic (variables/conditions/effects/branches); exported to ink and
YarnSpinner by the exporters. Reused by WS-E (playtest) via the evaluator and WorldState.
"""

from __future__ import annotations

from .audit import LogicIssue, audit_quest_logic
from .expr import LogicSyntaxError, parse_expr, refs_in, render_expr
from .semantics import LogicEvalError, WorldState, evaluate, type_errors
from .simulate import SimRun, SimStep, simulate_quest

__all__ = [
    "LogicEvalError",
    "LogicIssue",
    "LogicSyntaxError",
    "SimRun",
    "SimStep",
    "WorldState",
    "audit_quest_logic",
    "evaluate",
    "parse_expr",
    "refs_in",
    "render_expr",
    "simulate_quest",
    "type_errors",
]
