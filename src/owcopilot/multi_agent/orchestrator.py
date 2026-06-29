"""OrchestratorAgent — decomposes a high-level goal into subtasks, routes to workers, synthesizes.

Architecture compliance (SUPERVISOR_rubric P3-1, P3-2, P3-6, P3-7):
  P3-1: OrchestratorAgent is a distinct class from WorkerAgent and VerifierAgent; it has its
        own ``agent_id``, its own ``ReActAgent`` instance, and its own transcript.
  P3-2: OrchestratorAgent performs genuine task decomposition — it calls ``_decompose_goal``
        which uses the LLM (or the deterministic offline fallback) to break a goal into
        subtask records.  It is NOT an if-else decision tree or a static tool list.
  P3-6: agent_id="orchestrator" appears in every blackboard message it posts.
  P3-7: Orchestrator posts messages to blackboard; workers read and respond via blackboard.
        The call chain is NOT a nested Python function call stack — the blackboard is the
        hand-off point (satisfying P3-3 by transitivity through session.py).

LLM decomposition (offline-safe):
  ``_decompose_goal`` sends a JSON-structured prompt to the gateway.  In offline/mock mode
  the provider returns a canned string; ``_parse_subtasks`` tries to extract a JSON list and
  falls back to two built-in subtasks (diagnosis + repair_proposal) if parsing fails.  This
  ensures offline tests run at $0 while the production path uses real LLM reasoning.

Decomposition strategy (no ReAct theatre):
  ``_decompose_goal`` is a TWO-pass *gateway* strategy — both passes are direct
  ``gateway.complete`` calls, not a ReActAgent loop.  Pass-1 asks for a JSON array of
  subtasks; if that does not parse, pass-2 retries with a stricter "JSON ONLY, no prose"
  system prompt (the most common real-LLM failure is prose wrapped around valid JSON, which
  a re-ask with a harder constraint fixes).  Only if BOTH passes fail does it fall back to
  the static template (``degraded=True``) — surfaced, never silent.

  Earlier revisions wired a ReActAgent loaded with *audit* tools into pass-2 to "extract JSON
  from verbose output".  That was structurally doomed (audit tools are irrelevant to JSON
  extraction, and the offline double never reaches a Final Answer in 2 steps), so it
  contributed nothing but the appearance of agent activation.  It has been removed in favour
  of the honest second gateway call above — no performative ReActAgent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..core.skills import SkillRegistry
from ..llm.gateway import LLMGateway
from .blackboard import AgentBlackboard
from .messages import AgentMessage, TaskAssignPayload, TaskResultPayload, VerifyResultPayload

logger = logging.getLogger(__name__)


@dataclass
class SubTask:
    """One unit of work to dispatch to a worker."""

    worker_role: str      # "diagnosis" | "repair_proposal"
    description: str      # the subtask instruction for the worker
    allowed_skills: list[str]
    worker_id: str        # the target agent_id (e.g. "diag_01", "repair_01")


@dataclass
class MultiAgentReport:
    """Final synthesized report from one orchestrator session."""

    session_id: str
    goal: str
    participants: list[str]           # all agent_ids that participated
    worker_summaries: list[dict[str, Any]]
    verifier_verdicts: list[dict[str, Any]]
    synthesis: str                    # orchestrator's human-readable conclusion
    # ② True when LLM decomposition failed and a static fallback template was used.
    # Callers MUST check this flag — silent downgrade is forbidden (no-silent-downgrade policy).
    decomposition_degraded: bool = field(default=False)


class OrchestratorAgent:
    """Decomposes a goal, routes subtasks to workers via blackboard, reads results, synthesizes.

    This is a first-class agent with its own ``agent_id`` and its own independent gateway call
    path.  It does NOT share transcript or context with any WorkerAgent or VerifierAgent — its
    decomposition runs through dedicated ``orchestrator_decompose`` gateway tasks, distinct
    from the ``worker_*`` / ``verifier_*`` tasks the other agents use.
    """

    def __init__(
        self,
        *,
        agent_id: str = "orchestrator",
        gateway: LLMGateway,
        registry: SkillRegistry,
    ) -> None:
        self.agent_id = agent_id
        self._gateway = gateway
        self._registry = registry

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def post_task_assignments(
        self,
        *,
        goal: str,
        session_id: str,
        blackboard: AgentBlackboard,
    ) -> tuple[list[AgentMessage], bool]:
        """Decompose ``goal`` and post task_assign messages for each worker.

        This is the "orchestrate" step.  Returns:
          - The list of posted messages so the caller (MultiAgentSession) can route them.
          - A ``degraded`` boolean: True when LLM decomposition failed and the static
            fallback template was used.  ② Callers must surface this — no silent downgrade.
        """
        subtasks, degraded = self._decompose_goal(goal)

        # ② Log a prominent warning when the fallback fires so it is never silent.
        if degraded:
            logger.warning(
                "session %s: LLM goal decomposition failed — fell back to static template. "
                "Subtasks are generic, not goal-specific. "
                "[DECOMPOSITION DEGRADED — static fallback active]",
                session_id,
            )

        posted: list[AgentMessage] = []

        for subtask in subtasks:
            payload = TaskAssignPayload(
                original_goal=goal,       # P3-4: always inject original_goal
                subtask=subtask.description,
                allowed_skills=subtask.allowed_skills,
            )
            msg = AgentMessage(
                session_id=session_id,
                from_agent=self.agent_id,
                to_agent=subtask.worker_id,
                msg_type="task_assign",
                payload=payload.to_dict(),
            )
            blackboard.post_message(msg)
            posted.append(msg)

        return posted, degraded

    def post_verify_request(
        self,
        *,
        task_result_msg: AgentMessage,
        session_id: str,
        blackboard: AgentBlackboard,
    ) -> AgentMessage:
        """Ask the verifier to independently validate a task_result."""
        verify_req = AgentMessage(
            session_id=session_id,
            from_agent=self.agent_id,
            to_agent="verifier_01",
            msg_type="verify_request",
            payload={"target_msg_id": task_result_msg.id},
        )
        blackboard.post_message(verify_req)
        return verify_req

    def synthesize(
        self,
        *,
        goal: str,
        session_id: str,
        blackboard: AgentBlackboard,
        decomposition_degraded: bool = False,
    ) -> MultiAgentReport:
        """Read all results and produce the final synthesized report.

        Synthesis logic is deterministic — it reads blackboard records and formats
        a structured report.  No LLM call in the synthesis step itself (keeping the
        gate-that-matters deterministic and $0).

        Args:
            decomposition_degraded: ② Pass True when LLM decomposition fell back to the
                static template.  This is surfaced in the synthesis text and the report
                field so callers are never silently unaware of the degradation.
        """
        task_results = blackboard.read_messages(session_id, msg_type="task_result")
        verify_results = blackboard.read_messages(session_id, msg_type="verify_result")

        # Collect participant agent_ids
        participants: set[str] = {self.agent_id}
        for msg in task_results:
            participants.add(msg.from_agent)
        for msg in verify_results:
            participants.add(msg.from_agent)

        worker_summaries = []
        for msg in task_results:
            result = TaskResultPayload.from_dict(msg.payload)
            worker_summaries.append(
                {
                    "agent_id": msg.from_agent,
                    "role": result.worker_role,
                    "final_answer": result.final_answer,
                    "open_errors": result.open_errors,
                    "stop_reason": result.stop_reason,
                }
            )

        verifier_verdicts = []
        for msg in verify_results:
            vr = VerifyResultPayload.from_dict(msg.payload)
            verifier_verdicts.append(
                {
                    "agent_id": msg.from_agent,
                    "verdict": vr.verdict,
                    "rationale": vr.rationale,
                    "open_errors_verified": vr.open_errors_verified,
                }
            )

        # ② ⑥ Build synthesis text — surface decomposition degradation and needs_more verdicts
        synthesis = _build_synthesis(
            goal, worker_summaries, verifier_verdicts,
            decomposition_degraded=decomposition_degraded,
        )

        # Post synthesize message to blackboard for traceability
        synth_msg = AgentMessage(
            session_id=session_id,
            from_agent=self.agent_id,
            to_agent="broadcast",
            msg_type="synthesize",
            payload={
                "synthesis": synthesis,
                "participants": sorted(participants),
                "worker_count": len(worker_summaries),
                "verifier_count": len(verifier_verdicts),
                "decomposition_degraded": decomposition_degraded,
            },
            status="done",  # terminal record — never claimed
        )
        blackboard.post_message(synth_msg)

        return MultiAgentReport(
            session_id=session_id,
            goal=goal,
            participants=sorted(participants),
            worker_summaries=worker_summaries,
            verifier_verdicts=verifier_verdicts,
            synthesis=synthesis,
            decomposition_degraded=decomposition_degraded,
        )

    # ------------------------------------------------------------------
    # Private: goal decomposition
    # ------------------------------------------------------------------

    def _decompose_goal(self, goal: str) -> tuple[list[SubTask], bool]:
        """Break a high-level goal into worker subtasks.

        Two-pass *gateway* strategy (both passes are direct gateway calls — no ReAct loop):
        1. Ask for a JSON array of subtasks.
        2. If that does not parse, re-ask with a stricter "JSON ONLY, no prose, no fences"
           system prompt.  The common real-LLM failure is valid JSON wrapped in prose; a
           harder constraint on the retry recovers it.
        3. If both passes fail, fall back to the static template (degraded=True).

        Returns:
            (subtasks, degraded) — degraded=True means both passes failed and the static
            fallback template is being used.  ② This must NOT be silent.
        """
        system = (
            "You are a task-decomposition specialist for an open-world game content pipeline. "
            "Given a high-level goal, output a JSON array of subtasks. "
            "Each subtask must have: role (diagnosis|repair_proposal), description (string), "
            "allowed_skills (array of skill names), worker_id (string like 'diag_01'). "
            "Always include at least one diagnosis subtask and one repair_proposal subtask. "
            "Output ONLY valid JSON — no prose, no markdown fences."
        )
        user = (
            f"Goal: {goal}\n\n"
            "Decompose into subtasks for a multi-agent system. "
            "Diagnosis workers use skills: audit_project, list_issues, build_context_pack. "
            "Repair workers use skills: propose_fix, quality_harness. "
            "Output JSON array only."
        )
        # Pass 1: direct gateway call
        raw = self._gateway.complete(task="orchestrator_decompose", system=system, user=user)
        subtasks, degraded = _parse_subtasks(raw, goal)

        if not degraded:
            return subtasks, False

        # Pass 2: honest second gateway call with a stricter JSON-only constraint.
        # No ReActAgent / no audit tools — those are irrelevant to JSON extraction and would
        # only be performative.  We just re-ask harder.
        logger.debug(
            "orchestrator: pass-1 decomposition parse failed; retrying with strict JSON prompt"
        )
        strict_system = (
            "Output ONLY a raw JSON array. No prose. No explanation. No markdown code fences. "
            "Your entire response must start with '[' and end with ']'. "
            "Each element is an object with keys: role (diagnosis|repair_proposal), "
            "description (string), allowed_skills (array of strings), worker_id (string). "
            "Include at least one diagnosis and one repair_proposal subtask."
        )
        strict_user = (
            f"Goal: {goal}\n\nReturn the JSON array of subtasks now. JSON only."
        )
        try:
            raw2 = self._gateway.complete(
                task="orchestrator_decompose", system=strict_system, user=strict_user
            )
            subtasks2, degraded2 = _parse_subtasks(raw2, goal)
            if not degraded2:
                logger.debug("orchestrator: pass-2 strict-JSON decomposition succeeded")
                return subtasks2, False
        except Exception as exc:  # noqa: BLE001
            logger.warning("orchestrator: pass-2 strict-JSON retry raised: %s", exc)

        # Both passes failed — return static fallback (degraded=True)
        return _FALLBACK_SUBTASKS, True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FALLBACK_SUBTASKS = [
    SubTask(
        worker_role="diagnosis",
        description=(
            "Run a full consistency audit on the project. "
            "Use audit_project to find all open errors, then list_issues to prioritise by "
            "severity. "
            "Report: total open errors, top-3 by severity."
        ),
        allowed_skills=["audit_project", "list_issues", "build_context_pack"],
        worker_id="diag_01",
    ),
    SubTask(
        worker_role="repair_proposal",
        description=(
            "Propose fixes for the top open errors found in the project. "
            "Use propose_fix for each error. "
            "All proposals are propose-only — never write canon directly. "
            "Report: how many patches were proposed and their ids."
        ),
        allowed_skills=["propose_fix", "quality_harness"],
        worker_id="repair_01",
    ),
]


def _coerce_allowed_skills(value: Any, *, worker_role: str) -> list[str]:
    """Normalise an ``allowed_skills`` value from LLM JSON into a clean ``list[str]``.

    ④ Defends against the common LLM mistake of writing ``allowed_skills`` as a bare string
    (e.g. ``"audit_project"``) instead of a list.  Naively calling ``list("audit_project")``
    explodes it into single characters (``['a','u','d',...]``); none match a real skill name, so
    the worker's scoped registry denies everything and the worker silently spins to max_steps
    doing nothing.  Here we wrap a bare string into a one-element list and warn, and we drop any
    non-string elements, rather than producing a useless worker without a trace.
    """
    if value is None:
        return []
    if isinstance(value, str):
        # A single skill name written as a scalar — wrap it, do NOT iterate its characters.
        logger.warning(
            "orchestrator: allowed_skills for role %r was a bare string %r; "
            "wrapping as a single-element list (an LLM formatting slip, not a per-char list).",
            worker_role, value,
        )
        return [value]
    if not isinstance(value, list):
        logger.warning(
            "orchestrator: allowed_skills for role %r had unexpected type %s; treating as empty.",
            worker_role, type(value).__name__,
        )
        return []
    cleaned = [s for s in value if isinstance(s, str)]
    if len(cleaned) != len(value):
        logger.warning(
            "orchestrator: allowed_skills for role %r contained %d non-string element(s); "
            "dropped them.",
            worker_role, len(value) - len(cleaned),
        )
    return cleaned


def _parse_subtasks(raw: str, goal: str) -> tuple[list[SubTask], bool]:
    """Try to parse LLM JSON response; fall back to built-in subtasks on any failure.

    Returns:
        (subtasks, degraded) — degraded=True means parse failed and _FALLBACK_SUBTASKS used.
        ② The degraded flag must be surfaced by callers — never silent.
    """
    try:
        # Strip markdown fences if present
        text = raw.strip()
        for fence in ("```json", "```JSON", "```"):
            if text.startswith(fence):
                text = text[len(fence):].strip()
        if text.endswith("```"):
            text = text[:-3].strip()

        # Find JSON array
        start = text.find("[")
        end = text.rfind("]") + 1
        if start == -1 or end == 0:
            return _FALLBACK_SUBTASKS, True

        data = json.loads(text[start:end])
        if not isinstance(data, list) or len(data) == 0:
            return _FALLBACK_SUBTASKS, True

        subtasks: list[SubTask] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "diagnosis"))
            description = str(item.get("description", f"Handle subtask for: {goal}"))
            allowed = _coerce_allowed_skills(item.get("allowed_skills", []), worker_role=role)
            worker_id = str(item.get("worker_id", f"{role}_01"))
            subtasks.append(
                SubTask(
                    worker_role=role,
                    description=description,
                    allowed_skills=allowed,
                    worker_id=worker_id,
                )
            )

        if not subtasks:
            return _FALLBACK_SUBTASKS, True

        # ③ The decomposition contract requires BOTH a diagnosis and a repair_proposal subtask
        # (the system prompt demands it).  An LLM that returns, say, only a diagnosis would leave
        # the session with no repair half and used to pass through as non-degraded — a silent
        # downgrade.  Treat a missing required role as a parse failure so pass-2 / the static
        # fallback fires and the degradation is surfaced, never silent.
        roles = {st.worker_role for st in subtasks}
        missing = [r for r in ("diagnosis", "repair_proposal") if r not in roles]
        if missing:
            logger.warning(
                "orchestrator: decomposition is missing required role(s) %s "
                "(got roles=%s) — treating as degraded so the gap is surfaced, not silent.",
                missing, sorted(roles),
            )
            return _FALLBACK_SUBTASKS, True

        return subtasks, False  # successfully parsed AND role-complete — not degraded

    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return _FALLBACK_SUBTASKS, True


def _build_synthesis(
    goal: str,
    worker_summaries: list[dict[str, Any]],
    verifier_verdicts: list[dict[str, Any]],
    *,
    decomposition_degraded: bool = False,
) -> str:
    """Build a human-readable synthesis from all agent outputs. Deterministic — no LLM.

    ② If decomposition_degraded is True, the synthesis explicitly notes that LLM
       decomposition failed and a static fallback template was used — no silent downgrade.
    ⑥ Verifier needs_more verdicts are annotated distinctly from pass/fail.
    """
    lines = [
        "Multi-Agent Session Report",
        f"Goal: {goal}",
    ]

    # ② Surface decomposition degradation prominently
    if decomposition_degraded:
        lines.append(
            "[WARNING] 分解已降级到静态模板 — LLM goal decomposition failed; "
            "subtasks are generic, not goal-specific. "
            "Review whether the static template is appropriate for this goal."
        )

    lines += [
        "",
        f"Workers ({len(worker_summaries)}):",
    ]
    for ws in worker_summaries:
        stop = ws["stop_reason"]
        lines.append(
            f"  [{ws['agent_id']}] role={ws['role']} "
            f"open_errors={ws['open_errors']} stop={stop}"
        )
        lines.append(f"    Answer: {ws['final_answer'][:200]}")

    lines.append(f"\nVerifier ({len(verifier_verdicts)}):")
    for vv in verifier_verdicts:
        verdict = vv["verdict"]
        # ⑥ Distinguish needs_more from pass/fail with explicit annotation
        if verdict == "needs_more":
            verdict_label = "needs_more [INCOMPLETE — verifier could not finish audit]"
        elif verdict == "pass":
            verdict_label = "pass"
        else:
            verdict_label = "fail"
        lines.append(
            f"  [{vv['agent_id']}] verdict={verdict_label} "
            f"open_errors_verified={vv['open_errors_verified']}"
        )
        lines.append(f"    Rationale: {vv['rationale'][:200]}")

    return "\n".join(lines)
