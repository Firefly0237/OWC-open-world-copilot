# 多智能体系统：Orchestrator-Worker + SQLite 黑板。
"""OWCopilot Multi-Agent System — Anthropic Orchestrator-Worker + SQLite Blackboard.

Provides a runtime multi-agent system where:
  - OrchestratorAgent decomposes a high-level goal and routes subtasks to workers.
  - DiagWorker and RepairWorker each run an independent ReActAgent with their own transcript.
  - VerifierAgent independently re-runs the deterministic audit to validate worker outputs.
  - AgentBlackboard (SQLite append-only log) is the sole communication channel.

Usage::

    from owcopilot.multi_agent import MultiAgentSession
    from owcopilot.skills import default_skill_registry
    from owcopilot.llm.chat_model import ChatModelProvider

    registry = default_skill_registry(content_root="/path/to/world")
    chat_model = ChatModelProvider(model="deepseek-chat").chat_model()
    session = MultiAgentSession(
        orchestrator_model=chat_model, model=chat_model, registry=registry
    )
    report = session.run("Bring this world to exportable state")
    print(report.synthesis)

评测覆盖诚实说明（W1-2 标注,评估逻辑未改动）:
    本模块**未评测**——`evaluation/` 全目录对 `multi_agent` 零引用（无 orchestrator-worker-verifier
    协作质量的评测器：没有任务成功率、没有协作正确率、没有错误级联率、没有 worker 间通信开销这类
    可比较指标）。`tests/test_multi_agent.py` 只验证架构约束（SUPERVISOR_rubric P3-1~P3-7:独立
    agent_id/黑板通信/verifier 独立性等结构性断言，全部离线 $0 fake model），不产出任何质量分数。
    CLI `multi-agent` 命令（``cli/main.py::_cmd_multi_agent``）只回显原始 telemetry（成本/步数）与
    blackboard 消息流，不做任何打分或声称"已评估"。真正的多 agent 协作评测是 Wave 5 W5-2（见
    docs/研究/一轮修改优化_总实施方案.md 与 docs/研究/helloagents对照与补强方案.md 条目 12-19）。
    在此之前，任何对外材料都不得声称这套编排"已评估"或引用任何协作质量数字——因为没有这样的数字。
"""

from .blackboard import AgentBlackboard
from .messages import (
    AgentMessage,
    ReflectionPayload,
    TaskAssignPayload,
    TaskResultPayload,
    VerifyResultPayload,
)
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
    "ReflectionPayload",
    "RepairWorker",
    "TaskAssignPayload",
    "TaskResultPayload",
    "VerifierAgent",
    "VerifyResultPayload",
    "WorkerAgent",
]
