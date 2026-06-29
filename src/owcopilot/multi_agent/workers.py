"""Worker agents — DiagWorker and RepairWorker.

Each worker is an *independent* object with its own ``agent_id``, its own ``ReActAgent``
instance, and its own transcript.  Workers do NOT share any state with each other or with
the orchestrator.

Architecture compliance (SUPERVISOR_rubric):
  P3-1: DiagWorker and RepairWorker are distinct, independently instantiable classes — not
        one ReActAgent with a different system prompt.
  P3-4: The goal string passed to ReActAgent is always
        "{original_goal}\\n\\n[Your subtask]:\\n{subtask}" — both fields present.
  P3-6: Each instance exposes a unique ``agent_id``.
  P3-7: Workers are driven from MultiAgentSession, which calls them in sequence after
        posting via the blackboard, not via a shared function call stack (the blackboard
        message is the hand-off, not a Python return value).

Offline / $0 operation:
  Workers accept an ``LLMGateway``.  In tests, the gateway is built with MockProvider so
  no real LLM calls happen.  The mock's canned output will NOT match the ReAct format
  exactly, so the worker will hit max_steps and return a "max_steps" stop_reason — this
  is expected and tested for.  The important things are:
  (a) A WorkerAgent instance is created and has an independent transcript.
  (b) It writes a task_result message to the blackboard.
  (c) It never shares transcript with any other agent.
"""

from __future__ import annotations

from ..agent.react import AgentResult, ReActAgent
from ..core.skills import SkillRegistry
from ..llm.gateway import LLMGateway
from .blackboard import AgentBlackboard
from .messages import AgentMessage, TaskAssignPayload, TaskResultPayload
from .skill_scope import scoped_registry


class WorkerAgent:
    """Base worker: receives a task_assign from the blackboard, runs a ReAct loop, posts result.

    Subclasses set ``role`` and ``default_allowed_skills`` to specialise their tool face.
    Each instance has a unique ``agent_id`` and a fresh ``ReActAgent`` so there is zero
    shared state between any two workers.
    """

    role: str = "generic_worker"
    default_allowed_skills: list[str] = []

    def __init__(
        self,
        *,
        agent_id: str,
        gateway: LLMGateway,
        registry: SkillRegistry,
        max_steps: int = 4,
    ) -> None:
        self.agent_id = agent_id
        self._gateway = gateway
        self._registry = registry
        self._max_steps = max_steps
        # The ReActAgent is created fresh per instance — its transcript list is private.
        # Creating it here (not inside run_task) lets tests inspect agent._transcript after
        # run_task to verify transcript isolation between two WorkerAgent instances.
        self.agent: ReActAgent = self._make_agent(allowed_skills=None, max_steps=self._max_steps)

    def _make_agent(
        self, *, allowed_skills: set[str] | None, max_steps: int | None = None
    ) -> ReActAgent:
        """Instantiate a fresh ReActAgent bound to this worker's settings.

        ``allowed_skills`` is enforced at BOTH layers:
          - prompt: the manifest only shows the allowed tools (via ReActAgent.allowed_skills);
          - execution: the registry is wrapped by ``scoped_registry`` so any out-of-scope
            skill is DENIED at ``run()`` time — a read-only worker physically cannot invoke
            ``propose_fix``, it only ever sees an is_error observation and self-corrects.
        """
        return ReActAgent(
            gateway=self._gateway,
            registry=scoped_registry(self._registry, allowed_skills),
            max_steps=max_steps if max_steps is not None else self._max_steps,
            task=f"worker_{self.agent_id}",
            allowed_skills=allowed_skills,
        )

    def run_task(
        self,
        task_msg: AgentMessage,
        blackboard: AgentBlackboard,
    ) -> AgentMessage:
        """Execute the subtask and post a task_result message to the blackboard.

        Key contract:
        - Reads ``original_goal`` AND ``subtask`` from task_msg.payload (P3-4).
        - Creates a fresh ReActAgent with the allowed_skills from the payload.
        - Runs the ReAct loop (may be offline mock — stop_reason can be "max_steps").
        - Posts a task_result message with the worker's answer.
        - Returns the posted task_result AgentMessage.
        """
        assign = TaskAssignPayload.from_dict(task_msg.payload)

        # CRITICAL: goal includes BOTH original_goal + subtask (P3-4 / cascade-hallucination guard)
        goal = (
            f"Overall goal: {assign.original_goal}\n\n"
            f"[Your specific subtask as {self.role}]:\n{assign.subtask}"
        )

        # Build allowed skills set — merge defaults with what orchestrator specified
        allowed: set[str] | None = None
        task_skills = assign.allowed_skills or self.default_allowed_skills
        if task_skills:
            allowed = set(task_skills)

        # Re-instantiate agent with the per-task allowed_skills AND the per-task step budget
        # the orchestrator assigned (assign.max_steps) — the protocol knob is now live, not a
        # field the worker silently ignored.  Keeps transcript isolated.
        self.agent = self._make_agent(allowed_skills=allowed, max_steps=assign.max_steps)

        result: AgentResult = self.agent.run(goal)

        # The worker's "claimed open errors" is the REAL integer from its own audit_project
        # tool call (extracted from the ReAct transcript), NOT a substring count of its prose.
        # If the worker never ran a successful audit_project, this is None — an honest
        # "no audit-backed claim" the verifier must not misread as a claim of zero.
        open_errors = _extract_claimed_open_errors(result)

        payload = TaskResultPayload(
            task_msg_id=task_msg.id,
            worker_role=self.role,
            final_answer=result.final_answer,
            open_errors=open_errors,
            stop_reason=result.stop_reason,
            step_count=result.step_count,
        )

        result_msg = AgentMessage(
            session_id=task_msg.session_id,
            from_agent=self.agent_id,
            to_agent="orchestrator",  # results always go back to orchestrator
            msg_type="task_result",
            payload=payload.to_dict(),
            # Terminal record — it is never claimed, so it must not linger as 'pending'.
            status="done",
        )
        blackboard.post_message(result_msg)
        blackboard.update_status(task_msg.id, "done")
        return result_msg


class DiagWorker(WorkerAgent):
    """Diagnosis worker: runs audit tools to find open errors.

    Allowed skills: audit_project, list_issues, build_context_pack.
    These are all READ_ONLY / DETERMINISTIC tools — no writes to canon.
    """

    role = "diagnosis"
    default_allowed_skills = ["audit_project", "list_issues", "build_context_pack"]

    def __init__(
        self,
        *,
        agent_id: str = "diag_01",
        gateway: LLMGateway,
        registry: SkillRegistry,
        max_steps: int = 4,
    ) -> None:
        super().__init__(
            agent_id=agent_id,
            gateway=gateway,
            registry=registry,
            max_steps=max_steps,
        )


class RepairWorker(WorkerAgent):
    """Repair-proposal worker: proposes fixes for issues found by DiagWorker.

    Allowed skills: propose_fix, quality_harness.
    All proposals are PROPOSES_PATCH — never WRITES_CANON.
    """

    role = "repair_proposal"
    default_allowed_skills = ["propose_fix", "quality_harness"]

    def __init__(
        self,
        *,
        agent_id: str = "repair_01",
        gateway: LLMGateway,
        registry: SkillRegistry,
        max_steps: int = 4,
    ) -> None:
        super().__init__(
            agent_id=agent_id,
            gateway=gateway,
            registry=registry,
            max_steps=max_steps,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# The audit tools whose structured result carries an authoritative ``open_errors`` integer.
_AUDIT_ACTIONS: frozenset[str] = frozenset({"audit_project"})


def _extract_claimed_open_errors(result: AgentResult) -> int | None:
    """Read the worker's REAL open-error claim from its own ``audit_project`` tool result.

    Scans the ReAct transcript for a successful ``audit_project`` step and returns the
    ``open_errors`` integer straight from the tool's *structured* result (``AgentStep.result``,
    captured before the observation string is truncated).  The last successful audit wins, so a
    worker that re-audits after proposing fixes reports its final measured count.

    Returns ``None`` when the worker never ran a successful audit — an honest "no audit-backed
    claim".  This is deliberately NOT 0: a missing audit is not a claim of zero errors, and the
    verifier must be able to tell the difference (so it does not flag an honest non-auditing
    worker as having under-reported).

    This replaces the previous ``answer.lower().count("error")`` substring heuristic, which was
    not the worker's claim at all — it drifted with prose verbosity and was always 0 for Chinese
    answers (中文 uses 「错误」, no Latin "error" substring), causing honest workers to be
    systematically misjudged as liars against the verifier's true audit count.
    """
    claimed: int | None = None
    for step in result.steps:
        if step.is_error or step.action not in _AUDIT_ACTIONS:
            continue
        if not isinstance(step.result, dict):
            continue
        value = step.result.get("open_errors")
        # Guard: must be a real non-negative int (reject bool, which is an int subclass).
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        claimed = value  # keep the latest successful audit's count
    return claimed
