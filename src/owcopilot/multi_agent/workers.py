# Worker：DiagWorker(诊断) 与 RepairWorker(修复提议)，各自只读/提议，不写 canon。
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
  Workers accept a LangChain chat model.  In tests, that is a deterministic fake (a routed
  or static chat double), so no real LLM calls happen.  The important things are:
  (a) A WorkerAgent instance is created and has an independent transcript.
  (b) It writes a task_result message to the blackboard.
  (c) It never shares transcript with any other agent.
"""

from __future__ import annotations

import logging

from langchain_core.language_models.chat_models import BaseChatModel

from ..agent.react import AgentResult, ReActAgent
from ..llm.telemetry import TelemetryCollector
from ..skills import SkillRegistry
from .blackboard import AgentBlackboard
from .messages import AgentMessage, TaskAssignPayload, TaskResultPayload
from .skill_scope import scoped_registry

logger = logging.getLogger(__name__)


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
        model: BaseChatModel,
        registry: SkillRegistry,
        max_steps: int = 4,
        telemetry: TelemetryCollector | None = None,
    ) -> None:
        self.agent_id = agent_id
        self._model = model
        self._registry = registry
        self._max_steps = max_steps
        self._telemetry = telemetry
        # The ReActAgent is created fresh per instance — its transcript list is private.
        # Creating it here (not inside run_task) lets tests inspect agent._transcript after
        # run_task to verify transcript isolation between two WorkerAgent instances.
        self.agent: ReActAgent = self._make_agent(allowed_skills=None, max_steps=self._max_steps)

    def _make_agent(
        self, *, allowed_skills: set[str] | None, max_steps: int | None = None
    ) -> ReActAgent:
        """Instantiate a fresh ReActAgent bound to this worker's settings.

        ``allowed_skills`` is enforced at BOTH layers:
          - schema: the model is bound only the allowed tools (via ReActAgent.allowed_skills);
          - execution: the registry is wrapped by ``scoped_registry`` so any out-of-scope
            skill is DENIED at ``run()`` time — a read-only worker physically cannot invoke
            ``propose_fix``, it only ever sees an is_error observation and self-corrects.
        """
        return ReActAgent(
            model=self._model,
            registry=scoped_registry(self._registry, allowed_skills),
            max_steps=max_steps if max_steps is not None else self._max_steps,
            task=f"worker_{self.agent_id}",
            telemetry=self._telemetry,
            allowed_skills=allowed_skills,
        )

    def build_goal(self, assign: TaskAssignPayload) -> str:
        """Compose the ReActAgent goal for a subtask — BOTH original_goal + subtask (P3-4).

        Injecting the overall goal alongside the narrow subtask is the cascade-hallucination
        guard.  This is the single source of truth for the goal string, reused by the T2
        Reflexion retry driver so the Self-Reflection prompt and the retry actor see the exact
        goal the initial attempt ran (no format drift between the two).
        """
        return (
            f"Overall goal: {assign.original_goal}\n\n"
            f"[Your specific subtask as {self.role}]:\n{assign.subtask}"
        )

    def run_task(
        self,
        task_msg: AgentMessage,
        blackboard: AgentBlackboard,
        *,
        reflections: list[str] | None = None,
    ) -> AgentMessage:
        """Execute the subtask and post a task_result message to the blackboard.

        Key contract:
        - Reads ``original_goal`` AND ``subtask`` from task_msg.payload (P3-4).
        - Creates a fresh ReActAgent with the allowed_skills from the payload.
        - Runs the ReAct loop (may be offline mock — stop_reason can be "max_steps").
        - Posts a task_result message with the worker's answer.
        - Returns the posted task_result AgentMessage.

        ``reflections`` is the T2 Reflexion (Shinn et al., 2023) episodic memory: verbal notes
        distilled from a PRIOR failed attempt at this same subtask, forwarded verbatim to
        ``ReActAgent.run`` so the retry actor conditions on what went wrong.  Default ``None`` ⇒
        byte-identical to the pre-Reflexion behaviour (``ReActAgent.run`` renders no reflection
        block, so the worker prompt and posted result are unchanged).
        """
        assign = TaskAssignPayload.from_dict(task_msg.payload)

        # CRITICAL: goal includes BOTH original_goal + subtask (P3-4 / cascade-hallucination guard)
        goal = self.build_goal(assign)

        # Build the allowed-skill set by INTERSECTING the orchestrator's proposal with the role's
        # default set — never by replacing it.  ``self.default_allowed_skills`` is the role's safe
        # UPPER BOUND (a diagnosis worker may only ever reach read-only audit tools); the
        # orchestrator's ``assign.allowed_skills`` comes straight from the decomposer LLM's JSON
        # (orchestrator._parse_subtasks) and is therefore untrusted.  Letting that proposal REPLACE
        # the default would let a hallucinated (or injection-steered) decomposition hand a read-only
        # worker a PROPOSES_PATCH tool — Excessive Agency (OWASP LLM06:2025).  Intersection lets the
        # LLM only NARROW the role's tool face, never widen it (least privilege).
        allowed: set[str]
        default_allowed = set(self.default_allowed_skills)
        if assign.allowed_skills:
            granted = {s for s in assign.allowed_skills if s in default_allowed}
            dropped = [s for s in assign.allowed_skills if s not in default_allowed]
            if dropped:
                logger.warning(
                    "worker %s (%s): dropped %d out-of-scope skill(s) %s proposed by the "
                    "orchestrator; a worker's tool set may only narrow the role default %s, never "
                    "expand it (least privilege). Remaining granted skills: %s.",
                    self.agent_id,
                    self.role,
                    len(dropped),
                    sorted(dropped),
                    sorted(default_allowed),
                    sorted(granted),
                )
            # Fail-safe: an empty intersection (the LLM proposed ONLY out-of-scope skills) yields an
            # empty allowed set — the worker gets NO tools and must answer from context alone. We do
            # NOT fall back to the broad default here, which would reward an out-of-bounds proposal
            # with the role's full access. ``set()`` (not ``None``) keeps the execution gate active.
            allowed = granted
        else:
            # No proposal → the role's full default set (unchanged pre-existing behaviour).
            allowed = default_allowed

        # Re-instantiate agent with the per-task allowed_skills AND the per-task step budget
        # the orchestrator assigned (assign.max_steps) — the protocol knob is now live, not a
        # field the worker silently ignored.  Keeps transcript isolated.
        self.agent = self._make_agent(allowed_skills=allowed, max_steps=assign.max_steps)

        result: AgentResult = self.agent.run(goal, reflections=reflections)

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
        model: BaseChatModel,
        registry: SkillRegistry,
        max_steps: int = 4,
        telemetry: TelemetryCollector | None = None,
    ) -> None:
        super().__init__(
            agent_id=agent_id,
            model=model,
            registry=registry,
            max_steps=max_steps,
            telemetry=telemetry,
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
        model: BaseChatModel,
        registry: SkillRegistry,
        max_steps: int = 4,
        telemetry: TelemetryCollector | None = None,
    ) -> None:
        super().__init__(
            agent_id=agent_id,
            model=model,
            registry=registry,
            max_steps=max_steps,
            telemetry=telemetry,
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
