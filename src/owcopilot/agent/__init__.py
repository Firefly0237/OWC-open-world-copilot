# agent 包：思考与工具循环 循环 + 技能面。循环见 react.py，离线确定性替身见 offline.py（测试/CI
# 用）。
"""Agent package: a canonical reasoning-and-tool loop loop over the OWCopilot skill surface.

See :mod:`owcopilot.agent.react` for the loop and :mod:`owcopilot.agent.offline` for the
deterministic offline reasoning double used in tests/CI.
"""

from __future__ import annotations

from .episodic import (
    EpisodicHit,
    ReflectionHit,
    format_history_block,
    persist_reflection,
    persist_run_embedding,
    recall_agent_memory,
    recall_reflections,
    recall_similar_runs,
    run_audit_signal,
)
from .history import persist_agent_run
from .react import AgentResult, AgentStep, ReActAgent
from .reflexion import ReflexionError

__all__ = [
    "AgentResult",
    "AgentStep",
    "EpisodicHit",
    "ReActAgent",
    "ReflectionHit",
    "ReflexionError",
    "format_history_block",
    "persist_agent_run",
    "persist_reflection",
    "persist_run_embedding",
    "recall_agent_memory",
    "recall_reflections",
    "recall_similar_runs",
    "run_audit_signal",
]
