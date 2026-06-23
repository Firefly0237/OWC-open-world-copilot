"""Agent package: a canonical ReAct loop over the OWCopilot skill surface.

See :mod:`owcopilot.agent.react` for the loop and :mod:`owcopilot.agent.offline` for the
deterministic offline reasoning double used in tests/CI.
"""

from __future__ import annotations

from .react import AgentResult, AgentStep, ParsedStep, ReActAgent, parse_react_step

__all__ = [
    "AgentResult",
    "AgentStep",
    "ParsedStep",
    "ReActAgent",
    "parse_react_step",
]
