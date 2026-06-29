"""OWCopilot Multi-Agent System — Anthropic Orchestrator-Worker + SQLite Blackboard.

Provides a runtime multi-agent system where:
  - OrchestratorAgent decomposes a high-level goal and routes subtasks to workers.
  - DiagWorker and RepairWorker each run an independent ReActAgent with their own transcript.
  - VerifierAgent independently re-runs the deterministic audit to validate worker outputs.
  - AgentBlackboard (SQLite append-only log) is the sole communication channel.

Usage::

    from owcopilot.multi_agent import MultiAgentSession
    from owcopilot.core.skills import default_skill_registry
    from owcopilot.llm.gateway import LLMGateway
    from owcopilot.fakes import MockProvider

    registry = default_skill_registry(content_root="/path/to/world")
    gateway = LLMGateway({"default": MockProvider()})
    session = MultiAgentSession(gateway=gateway, registry=registry)
    report = session.run("Bring this world to exportable state")
    print(report.synthesis)
"""

from .blackboard import AgentBlackboard
from .messages import AgentMessage, TaskAssignPayload, TaskResultPayload, VerifyResultPayload
from .orchestrator import MultiAgentReport, OrchestratorAgent
from .session import MultiAgentSession
from .verifier import VerifierAgent
from .workers import DiagWorker, RepairWorker, WorkerAgent

__all__ = [
    "AgentBlackboard",
    "AgentMessage",
    "DiagWorker",
    "MultiAgentReport",
    "MultiAgentSession",
    "OrchestratorAgent",
    "RepairWorker",
    "TaskAssignPayload",
    "TaskResultPayload",
    "VerifierAgent",
    "VerifyResultPayload",
    "WorkerAgent",
]
