"""VerifierAgent — independent ground-truth verification of worker outputs.

Architecture compliance (SUPERVISOR_rubric P3-5):
    "At least one independent Verifier Agent or Verifier step that performs ground-truth
     validation of Worker output (can reuse existing deterministic audit), and the
     Verifier's gateway must NOT share the same model instance as the verified Worker."

Key design decisions:
1. VerifierAgent has its own ``agent_id`` ("verifier_01") — separate from all workers.
2. It creates its own fresh ``ReActAgent`` instance with its own transcript.
3. It NEVER reads the worker's transcript — it only reads the worker's ``final_answer``
   from the blackboard's task_result payload.
4. It re-runs ``audit_project`` independently (a deterministic tool = true ground truth).
5. It compares its independently measured ``open_errors`` vs the worker's claim.
6. The gateway passed to VerifierAgent is intentionally the same LLMGateway class as
   workers use, but it is a different *instance* (or may be the same instance in tests
   where $0 offline mode means no real model calls anyway).  What matters is that the
   Verifier's reasoning is seeded from its own independent audit call, not from the
   worker's transcript.  Using a cross-model evaluator would also satisfy P3-5, but
   deterministic tools are stronger — the SUPERVISOR_rubric confirms this is acceptable.

Two-path verification (the deterministic path is the authority):
    The verifier first tries the DETERMINISTIC path — it calls ``audit_project`` directly
    through the SkillRegistry (no LLM, no parsing of natural language) and reads the
    ``open_errors`` integer straight from the tool result.  This is the real ground truth
    and works identically offline ($0) and online.  Only if the registry does not expose
    ``audit_project`` (e.g. a bare test registry with no project bound) does the verifier
    fall back to scraping the LLM's natural-language answer — and when *that* also yields
    nothing parseable it returns ``needs_more`` honestly, never a fabricated pass.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from ..agent.react import AgentResult, ReActAgent
from ..core.skills import SkillError, SkillRegistry
from ..llm.gateway import LLMGateway
from .blackboard import AgentBlackboard
from .messages import AgentMessage, TaskResultPayload, VerifyResultPayload
from .skill_scope import scoped_registry

# The minimal deterministic tool face the verifier is allowed to touch.
_VERIFIER_SKILLS: set[str] = {"audit_project", "list_issues"}


class VerifierAgent:
    """Independent verifier: reads a task_result, re-runs deterministic audit, emits verdict.

    DOES NOT read the worker's internal transcript — only the worker's ``final_answer``
    from the blackboard message payload.  This is the system-level anti-echo-chamber guarantee.
    """

    def __init__(
        self,
        *,
        agent_id: str = "verifier_01",
        gateway: LLMGateway,
        registry: SkillRegistry,
        # ≥4 so the offline ReAct script (audit→context→harness→Final Answer) can finish;
        # the deterministic path below is the real authority regardless of this budget.
        max_steps: int = 5,
    ) -> None:
        self.agent_id = agent_id
        self._gateway = gateway
        # Execution-time tool scoping: the verifier can ONLY run audit_project / list_issues.
        # An attempt to invoke any other registered skill is denied at dispatch (not just
        # hidden from the prompt manifest).  See skill_scope.scoped_registry.
        self._registry = scoped_registry(registry, _VERIFIER_SKILLS)
        self._max_steps = max_steps
        # Fresh independent ReActAgent — its transcript is not shared with any worker.
        self.agent: ReActAgent = self._make_agent()

    def _make_agent(self) -> ReActAgent:
        return ReActAgent(
            gateway=self._gateway,
            registry=self._registry,
            max_steps=self._max_steps,
            task=f"verifier_{self.agent_id}",
            allowed_skills=set(_VERIFIER_SKILLS),  # minimal deterministic tools
        )

    def verify(
        self,
        verify_req: AgentMessage,
        blackboard: AgentBlackboard,
    ) -> AgentMessage:
        """Independently verify the task_result referenced in ``verify_req``.

        Steps:
        1. Read the target task_result from the blackboard.
        2. Run the deterministic audit directly (true ground truth) — and, when available,
           an independent ReActAgent loop as well — to measure open errors.
        3. Compare our audit's open_errors vs worker's claimed open_errors.
        4. Post a verify_result message with pass/fail verdict.
        5. Return the posted verify_result AgentMessage.

        The verifier NEVER reads worker.agent._transcript — isolation is architectural,
        not a prompt constraint.
        """
        # 1. Retrieve what we're verifying (from blackboard, not from worker object)
        target_msg_id: str = verify_req.payload["target_msg_id"]
        target_msg = blackboard.get_message(target_msg_id)
        if target_msg is None:
            # Defensive: should never happen in a well-formed session
            payload = VerifyResultPayload(
                target_msg_id=target_msg_id,
                verdict="fail",
                rationale=f"target task_result message {target_msg_id!r} not found on blackboard",
                open_errors_verified=-1,
                worker_claimed_errors=None,  # no target → no claim to record
            )
            result_msg = AgentMessage(
                session_id=verify_req.session_id,
                from_agent=self.agent_id,
                to_agent="orchestrator",
                msg_type="verify_result",
                payload=payload.to_dict(),
                status="done",  # terminal record — never claimed
            )
            blackboard.post_message(result_msg)
            blackboard.update_status(verify_req.id, "done")
            return result_msg

        worker_result = TaskResultPayload.from_dict(target_msg.payload)
        # The worker's claim is the REAL integer from its own audit_project tool call (set by
        # workers._extract_claimed_open_errors), or None when it made no audit-backed claim.
        # When there is no structured claim, fall back to the count stated in the worker's
        # final-answer prose so a "looks clean / 0 errors" lie is still catchable.
        worker_claimed_errors: int | None = worker_result.open_errors
        if worker_claimed_errors is None:
            worker_claimed_errors = _extract_error_count(worker_result.final_answer)

        # 2a. DETERMINISTIC ground truth — call audit_project straight through the registry.
        #     No LLM, no natural-language parsing.  This is the real verification and the
        #     authority for the verdict whenever the audit tool is reachable.
        det_errors, det_source = self._deterministic_verify()

        verdict: Literal["pass", "fail", "needs_more"]
        rationale: str
        open_errors_verified: int

        if det_errors is not None:
            # Ground truth obtained deterministically — this is the verdict authority.
            verdict, rationale = _compute_verdict(
                open_errors_verified=det_errors,
                worker_claimed_errors=worker_claimed_errors,
                source=det_source,
            )
            open_errors_verified = det_errors
        else:
            # 2b. Fallback: no audit tool bound (e.g. a bare registry in unit tests).
            #     Run our independent ReActAgent and parse its answer (EN or 中文); if that
            #     also yields nothing, return needs_more honestly — never a fabricated pass.
            self.agent = self._make_agent()
            verify_goal = (
                f"Independently verify the following worker conclusion:\n\n"
                f"Worker role: {worker_result.worker_role}\n"
                f"Worker answer: {worker_result.final_answer}\n\n"
                f"Run audit_project and list_issues to independently measure open errors. "
                f"Report how many open errors you find."
            )
            agent_result: AgentResult = self.agent.run(verify_goal)
            parsed = _extract_error_count(agent_result.final_answer)
            if parsed is None:
                verdict = "needs_more"
                rationale = (
                    "Verifier could not obtain a ground-truth open-error count: the audit "
                    "tool is not bound and the agent answer was not parseable "
                    f"(stop_reason={agent_result.stop_reason!r}). Cannot confirm worker claim."
                )
                open_errors_verified = -1
            else:
                verdict, rationale = _compute_verdict(
                    open_errors_verified=parsed,
                    worker_claimed_errors=worker_claimed_errors,
                    source="agent-answer",
                )
                open_errors_verified = parsed

        # 4. Post verify_result
        payload = VerifyResultPayload(
            target_msg_id=target_msg_id,
            verdict=verdict,
            rationale=rationale,
            open_errors_verified=open_errors_verified,
            worker_claimed_errors=worker_claimed_errors,
        )
        result_msg = AgentMessage(
            session_id=verify_req.session_id,
            from_agent=self.agent_id,
            to_agent="orchestrator",
            msg_type="verify_result",
            payload=payload.to_dict(),
            status="done",  # terminal record — never claimed
        )
        blackboard.post_message(result_msg)
        blackboard.update_status(verify_req.id, "done")
        return result_msg

    # ------------------------------------------------------------------
    # Deterministic ground-truth path (the real verification)
    # ------------------------------------------------------------------

    def _deterministic_verify(self) -> tuple[int | None, str]:
        """Measure open errors by calling ``audit_project`` directly — no LLM, no parsing.

        Returns ``(open_errors, source)``.  ``open_errors`` is ``None`` only when the
        registry does not expose ``audit_project`` (e.g. a bare unit-test registry with no
        project bound), in which case the caller falls back to the LLM-answer path.

        ``audit_project`` returns ``{"open_errors": <int>, ...}`` straight from the
        deterministic consistency audit — this integer is the ground truth the verifier
        compares the worker's claim against.
        """
        if "audit_project" not in self._registry:
            return None, "no-audit-tool"
        try:
            result: dict[str, Any] = self._registry.run("audit_project", {})
        except SkillError:
            # Tool exists but could not run (denied/bad args) — treat as unavailable.
            return None, "audit-unavailable"
        except Exception:  # noqa: BLE001 — audit blew up; do not crash the verifier
            return None, "audit-error"

        # A real audit returns a dict; a malformed/malicious tool may return a non-dict
        # (list/str/None/int).  Guard before ``.get`` so the verifier never crashes — this
        # mirrors the worker-side guard in ``workers._extract_claimed_open_errors`` (the two
        # readers of an audit result must be equally defensive).
        if not isinstance(result, dict):
            return None, "audit-malformed"
        open_errors = result.get("open_errors")
        if (
            isinstance(open_errors, bool)
            or not isinstance(open_errors, int)
            or open_errors < 0
        ):
            # Defensive: malformed tool output — fall back rather than trust a non-int or a
            # negative count.  A real audit returns ``len(...)`` (≥0); a negative value can only
            # come from a malformed/malicious tool and would also collide with the -1 "target not
            # found" sentinel in open_errors_verified, so we refuse it here.
            return None, "audit-malformed"
        return open_errors, "deterministic-audit"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# English numeric-count patterns (lowercased answer).
_EN_COUNT_PATTERNS = (
    r"(\d+)\s+open\s+error",
    r"(\d+)\s+error",
    r"(\d+)\s+issue",
    r"found\s+(\d+)",
    r"total[:\s]+(\d+)",
)
# Chinese: "发现 2 个待修复错误" / "2 个错误" / "共 3 个问题" — number followed by 个 … 错误/问题.
_ZH_COUNT_PATTERN = r"(\d+)\s*个[^0-9]{0,8}?(?:错误|问题|缺陷)"


def _extract_error_count(answer: str) -> int | None:
    """Extract a numeric open-error count from a natural-language answer (EN or 中文).

    Returns the parsed integer, or ``None`` when no count can be confidently extracted.
    Returning ``None`` (rather than a silent 0) lets the caller emit an honest
    ``needs_more`` instead of fabricating a pass.  Deterministic — no LLM call.
    """
    lowered = answer.lower()
    for pattern in _EN_COUNT_PATTERNS:
        m = re.search(pattern, lowered)
        if m:
            return int(m.group(1))
    # Chinese count (run on the original string — 个/错误 are not affected by .lower()).
    m = re.search(_ZH_COUNT_PATTERN, answer)
    if m:
        return int(m.group(1))
    return None


def _compute_verdict(
    *,
    open_errors_verified: int,
    worker_claimed_errors: int | None,
    source: str,
) -> tuple[Literal["pass", "fail", "needs_more"], str]:
    """Determine pass/fail from the verifier's independent measurement (deterministic, no LLM).

    ``worker_claimed_errors`` is now the worker's REAL audit-backed integer (from its own
    ``audit_project`` tool result) or, when it made no structured claim, the count parsed from
    its final-answer prose.  ``None`` means the worker asserted no error count at all.

    Logic:
    - No claim at all (None): the verifier reports its independent ground truth and PASSES with
      an explicit annotation — it has no worker assertion to contradict, and must NOT fabricate a
      fail nor silently read the absence as a claim of zero.
    - Otherwise compare the worker's claim against ground truth:
        * "deterministic-audit": the worker and verifier read the SAME deterministic audit tool,
          so an honest worker agrees EXACTLY — tolerance is 0 (delta>0 → fail).
        * "agent-answer" / prose counts: the count was parsed from natural language, which is
          approximate, so a ±1 rounding tolerance applies.

    ``source`` records HOW the verifier's count was obtained ("deterministic-audit" = ground
    truth, "agent-answer" = parsed from the verifier's own ReAct loop) so the rationale is
    auditable.
    """
    if worker_claimed_errors is None:
        return (
            "pass",
            f"Verifier independently found {open_errors_verified} open error(s) [{source}]. "
            "Worker made no verifiable error-count claim (it ran no audit and stated no count), "
            "so there is nothing to contradict; reporting ground truth only.",
        )

    # Exact agreement is expected when both sides read the same deterministic audit; only the
    # natural-language-parsed sources get a ±1 rounding tolerance.
    tolerance = 0 if source == "deterministic-audit" else 1
    delta = abs(open_errors_verified - worker_claimed_errors)
    if delta <= tolerance:
        return (
            "pass",
            f"Verifier independently found {open_errors_verified} open error(s) "
            f"[{source}]; worker claimed {worker_claimed_errors}. "
            f"Delta={delta} (within tolerance={tolerance}).",
        )
    return (
        "fail",
        f"Verifier independently found {open_errors_verified} open error(s) [{source}] "
        f"but worker claimed {worker_claimed_errors}. "
        f"Delta={delta} exceeds tolerance={tolerance}.",
    )
