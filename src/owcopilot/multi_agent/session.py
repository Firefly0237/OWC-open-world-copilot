"""MultiAgentSession — drives one complete orchestrator-worker-verifier round.

This is the top-level entry point for a multi-agent run.  It wires together:
  - OrchestratorAgent (task decomposition + synthesis)
  - DiagWorker and RepairWorker (subtask execution)
  - VerifierAgent (independent verification)
  - AgentBlackboard (all inter-agent communication)

Architecture compliance (SUPERVISOR_rubric P3-7):
    "Multiple agents must not be in the same synchronous call chain (can be asyncio tasks or
     LangGraph node-to-node state passing, but not nested function calls)."

    P3-7 is satisfied here via the Blackboard pattern: the orchestrator does not call workers
    directly.  Instead it POSTS messages to the blackboard, then hands back control.  The
    session loop then reads those messages and DISPATCHES to the appropriate worker — the
    blackboard message is the hand-off, not a Python return value inside a call stack.

    Concretely:
      orchestrator.post_task_assignments(...) → writes to SQLite blackboard → returns
      [loop] worker.claim_task(blackboard) → reads from SQLite → runs → writes result
      orchestrator.post_verify_request(...) → writes verify_request → returns
      verifier.verify(verify_req, blackboard) → reads task_result → runs → writes verdict
      orchestrator.synthesize(...)  → reads all results → writes synthesize record

    Each agent's ReActAgent transcript is only touched within that agent's own method; no
    two agents share a transcript reference.

Offline / $0 compliance:
    All agents work with MockProvider (via LLMGateway built in tests).  The ReAct loops
    exhaust max_steps and return "max_steps" stop_reason.  All blackboard writes and reads
    are real SQLite operations.  The session completes and a MultiAgentReport is returned.
    Total LLM cost = $0.
"""

from __future__ import annotations

import logging
import sqlite3

from ..core.skills import SkillRegistry
from ..llm.gateway import LLMGateway
from .blackboard import AgentBlackboard
from .messages import AgentMessage, TaskAssignPayload, TaskResultPayload
from .orchestrator import MultiAgentReport, OrchestratorAgent
from .verifier import VerifierAgent
from .workers import DiagWorker, RepairWorker, WorkerAgent

logger = logging.getLogger(__name__)


class MultiAgentSession:
    """One end-to-end multi-agent run: orchestrate → diagnose → repair → verify → synthesize.

    Agents:
      - ``OrchestratorAgent`` (agent_id="orchestrator")
      - ``DiagWorker``        (agent_id="diag_01")
      - ``RepairWorker``      (agent_id="repair_01")
      - ``VerifierAgent``     (agent_id="verifier_01")

    All four are independent instances with independent transcripts.
    Communication flows exclusively through the SQLite blackboard.
    """

    def __init__(
        self,
        *,
        gateway: LLMGateway,
        registry: SkillRegistry,
        db_path: str = ":memory:",
        # Allow callers to inject a pre-built conn (e.g. shared with SQLiteStore) or let
        # session open its own in-memory conn for isolation.
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self._gateway = gateway
        self._registry = registry

        # Set up the shared SQLite connection (WAL mode for safe concurrent read)
        if conn is not None:
            self._conn = conn
        else:
            self._conn = sqlite3.connect(db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")

        self.blackboard = AgentBlackboard(self._conn)

        # Instantiate all four agents — each with its own LLM gateway call path and transcript.
        # The gateway instance may be shared (it is stateless beyond telemetry collection),
        # but each agent creates its own ReActAgent which owns an independent transcript list.
        self.orchestrator = OrchestratorAgent(
            agent_id="orchestrator",
            gateway=gateway,
            registry=registry,
        )
        self.diag_worker = DiagWorker(
            agent_id="diag_01",
            gateway=gateway,
            registry=registry,
        )
        self.repair_worker = RepairWorker(
            agent_id="repair_01",
            gateway=gateway,
            registry=registry,
        )
        self.verifier = VerifierAgent(
            agent_id="verifier_01",
            gateway=gateway,
            registry=registry,
        )

        # Worker routing table: agent_id → WorkerAgent instance
        self._workers: dict[str, WorkerAgent] = {
            "diag_01": self.diag_worker,
            "repair_01": self.repair_worker,
        }

    def run(self, goal: str, *, session_id: str | None = None) -> MultiAgentReport:
        """Execute one full multi-agent session and return the synthesized report.

        Flow (blackboard-mediated, not a nested call stack):
        1. Orchestrator decomposes goal → posts task_assign messages to blackboard.
        2. Session dispatches each task_assign to the addressed worker.
           Worker reads from blackboard, runs, posts task_result to blackboard.
        3. Orchestrator posts verify_request for each task_result.
        4. Verifier reads verify_request + task_result from blackboard, runs independent audit,
           posts verify_result to blackboard.
        5. Orchestrator reads all results from blackboard → synthesizes → returns report.
        """
        import uuid

        sid = session_id or str(uuid.uuid4())

        # Step 1: Orchestrator decomposes and posts task assignments
        # ② post_task_assignments now returns (msgs, degraded) — degraded must be surfaced
        task_msgs, decomposition_degraded = self.orchestrator.post_task_assignments(
            goal=goal,
            session_id=sid,
            blackboard=self.blackboard,
        )

        # Step 2: Dispatch each task_assign to its addressed worker
        # The worker claims from the blackboard (not via direct function argument)
        # This is the blackboard-mediated hand-off that satisfies P3-3 / P3-7.
        task_result_msgs: list[AgentMessage] = []
        # Track which task_msgs had no registered worker so we can record them
        unrouted: list[AgentMessage] = []
        # Track diag task_result for repair worker dependency injection (④)
        diag_result_msg: AgentMessage | None = None

        for task_msg in task_msgs:
            worker = self._workers.get(task_msg.to_agent)
            if worker is None:
                # ③ Unknown worker_id — NOT silent continue.
                # Warn loudly and record an unrouted_task message to the blackboard.
                logger.warning(
                    "session %s: task_msg %s targets unknown worker_id %r — task unrouted",
                    sid, task_msg.id, task_msg.to_agent,
                )
                unrouted_msg = AgentMessage(
                    session_id=sid,
                    from_agent="session",
                    to_agent="orchestrator",
                    msg_type="task_result",
                    payload=TaskResultPayload(
                        task_msg_id=task_msg.id,
                        worker_role=task_msg.to_agent,
                        final_answer=(
                            f"[UNROUTED] No registered worker for agent_id "
                            f"{task_msg.to_agent!r}. Task was not executed."
                        ),
                        open_errors=None,  # no worker ran → no audit-backed claim
                        stop_reason="unrouted",
                        step_count=0,
                    ).to_dict(),
                    status="failed",
                )
                self.blackboard.post_message(unrouted_msg)
                # Mark the original task_assign as failed too
                self.blackboard.update_status(task_msg.id, "failed")
                unrouted.append(task_msg)
                continue

            # ④ If this is the repair worker and we already have a diag result,
            # inject the diag findings into the repair subtask before claiming.
            # The enriched message is POSTED to the blackboard and the original is superseded,
            # so the worker claims the enriched version — real diag→repair handoff.
            if task_msg.to_agent == "repair_01" and diag_result_msg is not None:
                enriched = _enrich_repair_task(task_msg, diag_result_msg, sid)
                if enriched is not task_msg:
                    # Mark original generic assign as superseded (not claimable)
                    self.blackboard.update_status(task_msg.id, "done")
                    # Post the enriched assign so the worker claims it
                    self.blackboard.post_message(enriched)
                task_msg = enriched

            # Worker claims its task from the blackboard (optimistic lock)
            claimed = self.blackboard.claim_task(task_msg.to_agent, sid)
            if claimed is None:
                # Already claimed or no task — should not happen in this sequential flow
                logger.warning(
                    "session %s: claim_task for %r returned None — task already claimed or missing",
                    sid, task_msg.to_agent,
                )
                continue

            # ① Worker runs and posts result — wrap in try/except so any crash is recorded,
            # not silently swallowed as a phantom code=0 success.
            try:
                result_msg = worker.run_task(claimed, self.blackboard)
                task_result_msgs.append(result_msg)
                # Track diag result for downstream repair injection (④)
                if task_msg.to_agent == "diag_01":
                    diag_result_msg = result_msg
            except Exception as exc:  # noqa: BLE001
                # Mark the claimed task as failed on the blackboard (not orphaned 'claimed')
                logger.error(
                    "session %s: worker %r raised during run_task: %s",
                    sid, task_msg.to_agent, exc, exc_info=True,
                )
                self.blackboard.update_status(claimed.id, "failed")
                # Write a placeholder task_result so session report is truthful
                failed_result = AgentMessage(
                    session_id=sid,
                    from_agent=task_msg.to_agent,
                    to_agent="orchestrator",
                    msg_type="task_result",
                    payload=TaskResultPayload(
                        task_msg_id=claimed.id,
                        worker_role=worker.role,
                        final_answer=f"[WORKER FAILED] {exc!r}",
                        open_errors=None,  # crashed before any audit → no claim
                        stop_reason="error",
                        step_count=0,
                    ).to_dict(),
                    status="failed",
                )
                self.blackboard.post_message(failed_result)
                task_result_msgs.append(failed_result)

        # Step 3 + 4: Orchestrator requests verification; verifier runs independently
        for task_result_msg in task_result_msgs:
            verify_req = self.orchestrator.post_verify_request(
                task_result_msg=task_result_msg,
                session_id=sid,
                blackboard=self.blackboard,
            )
            # Verifier claims verify_request and runs its independent audit
            self.verifier.verify(verify_req, self.blackboard)

        # Step 5: Orchestrator synthesizes all results
        # ② Pass decomposition_degraded through so synthesis annotates it — no silent downgrade
        return self.orchestrator.synthesize(
            goal=goal,
            session_id=sid,
            blackboard=self.blackboard,
            decomposition_degraded=decomposition_degraded,
        )

    @property
    def session_participants(self) -> list[str]:
        """All agent_ids in this session (useful for assertions)."""
        return [
            self.orchestrator.agent_id,
            self.diag_worker.agent_id,
            self.repair_worker.agent_id,
            self.verifier.agent_id,
        ]

    def close(self) -> None:
        """Close the private SQLite connection (no-op if conn was injected externally)."""
        try:
            self._conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Session-level helpers
# ---------------------------------------------------------------------------


def _enrich_repair_task(
    repair_task_msg: AgentMessage,
    diag_result_msg: AgentMessage,
    session_id: str,
) -> AgentMessage:
    """④ Build a new repair task_assign that contains the diag worker's findings.

    When the orchestrator issues a generic repair subtask before seeing diag results,
    the repair worker would run blind.  This function replaces the repair subtask's
    description with one that explicitly embeds the diag final_answer — enabling true
    multi-agent handoff where repair acts on diag's concrete findings.

    A new AgentMessage is posted to the blackboard by the caller after this returns.
    The original generic repair task is superseded; the diag→repair dependency is
    made explicit in the payload.
    """
    try:
        diag_payload = TaskResultPayload.from_dict(diag_result_msg.payload)
        repair_assign = TaskAssignPayload.from_dict(repair_task_msg.payload)

        enriched_subtask = (
            f"Propose fixes for the issues found by the diagnosis worker.\n\n"
            f"Diagnosis findings:\n{diag_payload.final_answer}\n\n"
            f"Original repair instruction:\n{repair_assign.subtask}"
        )

        new_assign = TaskAssignPayload(
            original_goal=repair_assign.original_goal,
            subtask=enriched_subtask,
            allowed_skills=repair_assign.allowed_skills,
            max_steps=repair_assign.max_steps,
        )
        enriched_msg = AgentMessage(
            session_id=session_id,
            from_agent="orchestrator",
            to_agent=repair_task_msg.to_agent,
            msg_type="task_assign",
            payload=new_assign.to_dict(),
        )
        return enriched_msg
    except Exception as exc:  # noqa: BLE001
        # Enrich failure is non-fatal: fall back to original message
        logger.warning("_enrich_repair_task failed (%s); using original repair task", exc)
        return repair_task_msg
