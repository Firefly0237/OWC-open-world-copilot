# Orchestrator：把高层目标拆成子任务、路由给 worker、汇总结果。只编排，不自己干活。
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
  ``_decompose_goal`` sends a JSON-structured prompt to the chat model.  In offline/mock mode
  the model returns a canned non-JSON string; ``_parse_subtasks`` tries to extract a JSON list
  and falls back to two built-in subtasks (diagnosis + repair_proposal) if parsing fails.  This
  ensures offline tests run at $0 while the production path uses real LLM reasoning.

Decomposition strategy (no ReAct theatre):
  ``_decompose_goal`` is a TWO-pass direct chat-model strategy — both passes are direct
  ``model.invoke([SystemMessage, HumanMessage])`` calls, not a ReActAgent loop.  Pass-1 asks
  for a JSON array of subtasks; if that does not parse, pass-2 retries with a stricter "JSON
  ONLY, no prose" system prompt (the most common real-LLM failure is prose wrapped around
  valid JSON, which a re-ask with a harder constraint fixes).  Only if BOTH passes fail does
  it fall back to the static template (``degraded=True``) — surfaced, never silent.

  Earlier revisions wired a ReActAgent loaded with *audit* tools into pass-2 to "extract JSON
  from verbose output".  That was structurally doomed (audit tools are irrelevant to JSON
  extraction, and the offline double never reaches a Final Answer in 2 steps), so it
  contributed nothing but the appearance of agent activation.  It has been removed in favour
  of the honest second model call above — no performative ReActAgent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from ..agent.middleware import SettledCostBudget, TotalModelCallBudget
from ..agent.react import _AGENT_TIER, _model_id_of
from ..llm.jsonio import extract_json
from ..llm.telemetry import TelemetryCollector
from ..llm.telemetry_callback import TelemetryCallbackHandler
from ..skills import SkillRegistry
from .blackboard import AgentBlackboard
from .messages import AgentMessage, TaskAssignPayload, TaskResultPayload, VerifyResultPayload

logger = logging.getLogger(__name__)


@dataclass
class SubTask:
    """One unit of work to dispatch to a worker."""

    worker_role: str  # "diagnosis" | "repair_proposal"
    description: str  # the subtask instruction for the worker
    allowed_skills: list[str]
    worker_id: str  # the target agent_id (e.g. "diag_01", "repair_01")
    # Anthropic effort scaling: the per-worker ReAct step (== tool-call) budget the
    # orchestrator allocated for this subtask's complexity. ``None`` means the decomposition
    # decided no budget → defer to ``TaskAssignPayload``'s own default (current behaviour).
    max_steps: int | None = None


@dataclass
class MultiAgentReport:
    """Final synthesized report from one orchestrator session."""

    session_id: str
    goal: str
    participants: list[str]  # all agent_ids that participated
    worker_summaries: list[dict[str, Any]]
    verifier_verdicts: list[dict[str, Any]]
    synthesis: str  # orchestrator's human-readable conclusion
    # ② True when LLM decomposition failed and a static fallback template was used.
    # Callers MUST check this flag — silent downgrade is forbidden (no-silent-downgrade policy).
    decomposition_degraded: bool = field(default=False)


class OrchestratorAgent:
    """Decomposes a goal, routes subtasks to workers via blackboard, reads results, synthesizes.

    This is a first-class agent with its own ``agent_id`` and its own independent model call
    path.  It does NOT share transcript or context with any WorkerAgent or VerifierAgent — its
    decomposition runs through dedicated ``orchestrator_decompose`` chat-model calls, distinct
    from the ``worker_*`` / ``verifier_*`` tasks the other agents use.
    """

    def __init__(
        self,
        *,
        agent_id: str = "orchestrator",
        model: BaseChatModel,
        registry: SkillRegistry,
        # role → registered worker agent_id. Injected into the decomposition prompt (the model
        # must address real workers, not invent ids) and used to normalise near-miss ids after
        # parsing. None keeps the legacy behaviour (no constraint, no normalisation).
        registered_workers: dict[str, str] | None = None,
        # Per-call accounting for the decompose chat-model calls. Decomposition happens OUTSIDE
        # an agent.invoke context, so the TelemetryCallbackHandler is attached explicitly on the
        # invoke config (§1.3) — None ⇒ no accounting (its cost simply is not recorded).
        telemetry: TelemetryCollector | None = None,
        shared_model_budget: TotalModelCallBudget | None = None,
        shared_cost_budget: SettledCostBudget | None = None,
    ) -> None:
        self.agent_id = agent_id
        self._model = model
        self._registry = registry
        self._registered_workers = dict(registered_workers or {})
        self._telemetry = telemetry
        self._shared_model_budget = shared_model_budget
        self._shared_cost_budget = shared_cost_budget

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
            # Anthropic effort scaling: the orchestrator allocates the per-worker step budget
            # from the decomposition. When the decomposition decided no budget
            # (subtask.max_steps is None), construct exactly as before so the payload defers to
            # TaskAssignPayload's own default (byte-identical to the pre-effort-scaling path —
            # no magic default duplicated here).
            if subtask.max_steps is None:
                payload = TaskAssignPayload(
                    original_goal=goal,  # P3-4: always inject original_goal
                    subtask=subtask.description,
                    allowed_skills=subtask.allowed_skills,
                )
            else:
                payload = TaskAssignPayload(
                    original_goal=goal,  # P3-4: always inject original_goal
                    subtask=subtask.description,
                    allowed_skills=subtask.allowed_skills,
                    max_steps=subtask.max_steps,
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
            goal,
            worker_summaries,
            verifier_verdicts,
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

    def _invoke_decompose(self, system: str, user: str) -> str:
        """One decompose chat-model call — the direct-invoke analogue of ``gateway.complete``.

        Mirrors :func:`owcopilot.agent.reflexion.distill_reflection`: the call happens OUTSIDE an
        ``agent.invoke`` context, so the :class:`TelemetryCallbackHandler` (billing each call into
        the shared collector under ``orchestrator_decompose``) must be attached explicitly on the
        invoke config. Returns the reply text for ``_parse_subtasks`` — no ``response_format`` is
        set (the decomposition asks for a JSON *array*, which ``json_object`` mode would reject).
        """
        # Reserve before dispatch so concurrent/strict-retry decomposition cannot overspend the
        # session contract. Cost is necessarily a settled-cost gate: an in-flight provider call
        # can cross the threshold, but no subsequent call is dispatched once it has done so.
        if self._shared_cost_budget is not None:
            self._shared_cost_budget.check_before_dispatch("orchestrator_decompose")
        if self._shared_model_budget is not None:
            self._shared_model_budget.spend("orchestrator_decompose")

        config: RunnableConfig | None = None
        if self._telemetry is not None:
            config = {
                "callbacks": [
                    TelemetryCallbackHandler(
                        self._telemetry,
                        tier=_AGENT_TIER,
                        model=_model_id_of(self._model),
                        default_task="orchestrator_decompose",
                    )
                ]
            }
        reply = self._model.invoke(
            [SystemMessage(content=system), HumanMessage(content=user)], config=config
        )
        return str(reply.text)

    def _decompose_goal(self, goal: str) -> tuple[list[SubTask], bool]:
        """Break a high-level goal into worker subtasks.

        Two-pass direct chat-model strategy (both passes are direct ``model.invoke`` calls — no
        ReAct loop):
        1. Ask for a JSON array of subtasks.
        2. If that does not parse, re-ask with a stricter "JSON ONLY, no prose, no fences"
           system prompt.  The common real-LLM failure is valid JSON wrapped in prose; a
           harder constraint on the retry recovers it.
        3. If both passes fail, fall back to the static template (degraded=True).

        Returns:
            (subtasks, degraded) — degraded=True means both passes failed and the static
            fallback template is being used.  ② This must NOT be silent.
        """
        # Live finding: with only a format example ("string like 'diag_01'") the model invents
        # worker ids (diag_02, rep_01…) that route nowhere and the subtask is dropped. Constrain
        # the id vocabulary to the actual routing table when one was provided.
        worker_constraint = (
            "worker_id MUST be exactly one of: "
            + ", ".join(f"'{wid}' (role={role})" for role, wid in self._registered_workers.items())
            + ". Do not invent other worker ids — reuse the same worker for multiple subtasks "
            "of its role instead. "
            if self._registered_workers
            else "worker_id (string like 'diag_01'). "
        )
        system = (
            "You are a task-decomposition specialist for an open-world game content pipeline. "
            "Given a high-level goal, output a JSON array of subtasks. "
            "Each subtask must have: role (diagnosis|repair_proposal), description (string), "
            "allowed_skills (array of skill names), worker_id, "
            "max_steps (integer, the step/tool-call budget for that worker). "
            + worker_constraint
            + "Always include at least one diagnosis subtask and one repair_proposal subtask. "
            # Anthropic effort scaling: complexity → budget guidance lives in the prompt so the
            # model does not over-allocate simple subtasks (its common failure mode).
            "Set max_steps to scale effort to the subtask's complexity, not a fixed default: "
            "a narrow single-tool check needs a small budget (about 3); a broad multi-tool "
            "investigation needs more (about 6-8). Do not over-allocate simple subtasks. "
            f"Keep max_steps between {_MIN_SUBTASK_STEPS} and {_MAX_SUBTASK_STEPS}. "
            "Output ONLY valid JSON — no prose, no markdown fences."
        )
        user = (
            f"Goal: {goal}\n\n"
            "Decompose into subtasks for a multi-agent system. "
            "Diagnosis workers use skills: audit_project, list_issues, build_context_pack. "
            "Repair workers use skills: propose_fix, quality_harness. "
            "Output JSON array only."
        )
        # Pass 1: direct chat-model call
        raw = self._invoke_decompose(system, user)
        subtasks, degraded = _parse_subtasks(raw, goal)

        if not degraded:
            return self._normalise_worker_ids(subtasks), False

        # Pass 2: honest second chat-model call with a stricter JSON-only constraint.
        # No ReActAgent / no audit tools — those are irrelevant to JSON extraction and would
        # only be performative.  We just re-ask harder.
        logger.debug(
            "orchestrator: pass-1 decomposition parse failed; retrying with strict JSON prompt"
        )
        strict_system = (
            "Output ONLY a raw JSON array. No prose. No explanation. No markdown code fences. "
            "Your entire response must start with '[' and end with ']'. "
            "Each element is an object with keys: role (diagnosis|repair_proposal), "
            "description (string), allowed_skills (array of strings), worker_id (string), "
            "max_steps (integer). "
            f"Scale max_steps {_MIN_SUBTASK_STEPS}-{_MAX_SUBTASK_STEPS} to complexity. "
            "Include at least one diagnosis and one repair_proposal subtask."
        )
        strict_user = f"Goal: {goal}\n\nReturn the JSON array of subtasks now. JSON only."
        try:
            raw2 = self._invoke_decompose(strict_system, strict_user)
            subtasks2, degraded2 = _parse_subtasks(raw2, goal)
            if not degraded2:
                logger.debug("orchestrator: pass-2 strict-JSON decomposition succeeded")
                return self._normalise_worker_ids(subtasks2), False
        except Exception as exc:  # noqa: BLE001
            logger.warning("orchestrator: pass-2 strict-JSON retry raised: %s", exc)

        # Both passes failed — return static fallback (degraded=True)
        return _FALLBACK_SUBTASKS, True

    def _normalise_worker_ids(self, subtasks: list[SubTask]) -> list[SubTask]:
        """Remap invented worker ids to the registered worker for the subtask's role.

        Defence-in-depth behind the prompt constraint: even when told the exact id vocabulary,
        a model can still emit near-misses (rep_01 for repair_01). A subtask whose worker_id is
        not registered is re-addressed to the registered worker of its role — with a warning,
        never silently — so the work is executed instead of dropped as UNROUTED. A subtask
        whose role has no registered worker is left untouched (the session's honest UNROUTED
        reporting remains the terminal fallback).
        """
        if not self._registered_workers:
            return subtasks
        known_ids = set(self._registered_workers.values())
        normalised: list[SubTask] = []
        for subtask in subtasks:
            if subtask.worker_id in known_ids:
                normalised.append(subtask)
                continue
            replacement = self._registered_workers.get(subtask.worker_role)
            if replacement is None:
                normalised.append(subtask)  # unknown role too — leave for UNROUTED reporting
                continue
            logger.warning(
                "orchestrator: decomposition addressed unregistered worker '%s' "
                "(role=%s) — remapped to registered '%s'",
                subtask.worker_id,
                subtask.worker_role,
                replacement,
            )
            normalised.append(
                SubTask(
                    worker_role=subtask.worker_role,
                    description=subtask.description,
                    allowed_skills=subtask.allowed_skills,
                    worker_id=replacement,
                    # Preserve the allocated budget on an id remap — dropping it here would be a
                    # silent budget downgrade (no-silent-downgrade policy).
                    max_steps=subtask.max_steps,
                )
            )
        return normalised


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Anthropic effort-scaling clamp bounds for a per-subtask step (== tool-call) budget.
# The prompt tells the model "keep max_steps between 2 and 12"; these are the code-side
# guardrail so an over-scaling model (Anthropic's early versions spawned 50 subagents for a
# trivial query) cannot blow up cost. The prompt bounds MUST equal these two constants — the
# scaling-rules test asserts both numbers appear in the decompose prompt so they cannot drift.
#   _MIN = 2: ReActAgent requires max_steps >= 1; 2 = at least one tool call + a final answer.
#   _MAX = 12: conservative ceiling above the prompt's "complex ~8", well under runaway cost.
_MIN_SUBTASK_STEPS = 2
_MAX_SUBTASK_STEPS = 12
# A decomposition is untrusted model output. Bound fan-out independently from per-task step/tool
# limits so a verbose model cannot multiply cost by emitting dozens of otherwise-valid workers.
MAX_DECOMPOSED_SUBTASKS = 6
MAX_DECOMPOSED_SUBTASKS_PER_ROLE = 3


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
            worker_role,
            value,
        )
        return [value]
    if not isinstance(value, list):
        logger.warning(
            "orchestrator: allowed_skills for role %r had unexpected type %s; treating as empty.",
            worker_role,
            type(value).__name__,
        )
        return []
    cleaned = [s for s in value if isinstance(s, str)]
    if len(cleaned) != len(value):
        logger.warning(
            "orchestrator: allowed_skills for role %r contained %d non-string element(s); "
            "dropped them.",
            worker_role,
            len(value) - len(cleaned),
        )
    return cleaned


def _coerce_subtask_steps(value: Any) -> int | None:
    """Normalise an LLM-supplied ``max_steps`` budget into a clamped int (or ``None``).

    Isolates the "defend against the model's weird budget value" concept behind the Anthropic
    effort-scaling prompt, mirroring ``_coerce_allowed_skills``:
      - ``None`` (missing field) → ``None``: no budget decided, fall back to the payload default
        (regression-safe — this is the pre-effort-scaling behaviour).
      - ``bool`` → ``None``: ``isinstance(True, int)`` is True, so ``max_steps: true`` would
        otherwise become budget 1; reject it explicitly and fall back.
      - ``int``/``float`` → ``int(value)`` CLAMPED to ``[_MIN_SUBTASK_STEPS, _MAX_SUBTASK_STEPS]``
        (out-of-range is clamped, never rejected — the model still gets a runnable budget).
      - anything else (str/list/dict) → ``None``: unusable, fall back to the default.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        logger.warning(
            "orchestrator: max_steps was a bool %r; ignoring it (a bool is not a step budget) "
            "and deferring to the default.",
            value,
        )
        return None
    if isinstance(value, int | float):
        clamped = max(_MIN_SUBTASK_STEPS, min(_MAX_SUBTASK_STEPS, int(value)))
        if clamped != int(value):
            logger.warning(
                "orchestrator: max_steps %r out of range; clamped to %d (bounds [%d, %d]).",
                value,
                clamped,
                _MIN_SUBTASK_STEPS,
                _MAX_SUBTASK_STEPS,
            )
        return clamped
    logger.warning(
        "orchestrator: max_steps had unexpected type %s; ignoring it and deferring to the default.",
        type(value).__name__,
    )
    return None


def _parse_subtasks(raw: str, goal: str) -> tuple[list[SubTask], bool]:
    """Try to parse LLM JSON response; fall back to built-in subtasks on any failure.

    Extraction uses :func:`~owcopilot.llm.jsonio.extract_json` — the array-capable member of the
    same shared tolerant JSON-extraction module every other generation surface in the codebase
    uses (``barks.py``'s variant list reads this same function; ``worldgen/service.py``'s
    per-stage payload reads the object-only sibling ``extract_json_object``) — rather than a
    hand-rolled fence-strip + bracket-find. That also makes this MORE tolerant than the previous
    ad hoc version: it locates the JSON span regardless of surrounding prose, not just a fence at
    the very start of the reply (the module docstring's own "most common real-LLM failure" is
    prose wrapped AROUND the JSON, which a start-anchored fence strip does not handle).

    ``complete_structured`` (``llm/structured.py``, the project's Pydantic-validated structured
    output layer) is deliberately NOT used here: it validates a single ``BaseModel`` per call via
    ``LLMGateway.complete``, whereas decomposition emits a JSON *array* of heterogeneous subtask
    dicts into a plain ``@dataclass`` (not a ``BaseModel``) via a direct ``BaseChatModel.invoke``
    (no gateway) with a graceful-degrade contract (fall back to the static template, never raise) —
    the opposite of ``complete_structured``'s "never return a default, raise
    ``StructuredOutputError`` instead" discipline. Forcing that shape through it would mean
    introducing an envelope ``BaseModel`` (changing the model-facing wire contract this module's
    own prompt tests pin), replacing the two-pass *different-system-prompt* retry with its
    single-prompt error-appending repair loop, and inverting the degrade-vs-raise contract every
    caller here depends on — a redesign, not a reuse. This mirrors the same, already-established
    call in ``worldgen/service.py``'s ``parse_world_seed_payload`` (see its "Kept off
    complete_structured" comment) for the same reason: a loose multi-shape payload, not one
    Pydantic model per call.

    Returns:
        (subtasks, degraded) — degraded=True means parse failed and _FALLBACK_SUBTASKS used.
        ② The degraded flag must be surfaced by callers — never silent.
    """
    try:
        data = extract_json(raw)
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
            max_steps = _coerce_subtask_steps(item.get("max_steps"))
            subtasks.append(
                SubTask(
                    worker_role=role,
                    description=description,
                    allowed_skills=allowed,
                    worker_id=worker_id,
                    max_steps=max_steps,
                )
            )

        if not subtasks:
            return _FALLBACK_SUBTASKS, True

        if len(subtasks) > MAX_DECOMPOSED_SUBTASKS:
            logger.warning(
                "orchestrator: decomposition emitted %d subtasks; enforcing total cap %d.",
                len(subtasks),
                MAX_DECOMPOSED_SUBTASKS,
            )
        capped: list[SubTask] = []
        role_counts: dict[str, int] = {}
        for subtask in subtasks:
            if len(capped) >= MAX_DECOMPOSED_SUBTASKS:
                break
            role_count = role_counts.get(subtask.worker_role, 0)
            if role_count >= MAX_DECOMPOSED_SUBTASKS_PER_ROLE:
                logger.warning(
                    "orchestrator: dropping excess %s subtask; per-role cap is %d.",
                    subtask.worker_role,
                    MAX_DECOMPOSED_SUBTASKS_PER_ROLE,
                )
                continue
            capped.append(subtask)
            role_counts[subtask.worker_role] = role_count + 1
        subtasks = capped

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
                missing,
                sorted(roles),
            )
            return _FALLBACK_SUBTASKS, True

        return subtasks, False  # successfully parsed AND role-complete — not degraded

    # ValueError covers extract_json's "no balanced span" / "invalid JSON" raises (and
    # json.JSONDecodeError, itself a ValueError subclass, should extract_json ever let one through).
    except (KeyError, TypeError, ValueError):
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
            f"  [{ws['agent_id']}] role={ws['role']} open_errors={ws['open_errors']} stop={stop}"
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
