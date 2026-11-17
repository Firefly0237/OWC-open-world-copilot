# 多智能体系统：Orchestrator-Worker + SQLite 黑板。
"""OWCopilot Multi-Agent System — bounded execution Orchestrator-Worker + SQLite Blackboard.

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
    chat_model = ChatModelProvider(model="deepseek-chat").chat_model
    session = MultiAgentSession(
        orchestrator_model=chat_model, model=chat_model, registry=registry
    )
    report = session.run("Bring this world to exportable state")
    print(report.synthesis)

评测覆盖诚实说明:
    ``evaluation.formal_registry`` 已预注册等总预算 single-agent baseline、任务完成率、fan-out/join
    和唯一结果率，并为重复可见结果设置 hard-zero 攻击/良性对照门；冻结 contract/manifest 与结构测试
    会阻止评测协议静默漂移。``tests/agent/test_multi_agent.py`` 和 system demo 另验证独立
    transcript、SQLite 黑板、确定性 verifier、并发 join 与 session 级模型/工具硬账本。

    这些是协议、控制面与链路证据，不是 live-model 质量结论。当前 deterministic formal runner 明确只
    运行 harness controls；仓库没有发布 representative dev/sealed trial 的真实模型比较结果，因此不能
    对外引用 multi-agent 任务完成率、协作增益或可靠性数字。生成这类数字必须在冻结数据、固定模型/预算
    与独立 scorer 下另跑正式实验，不能由离线 fake 或结构测试替代。
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
