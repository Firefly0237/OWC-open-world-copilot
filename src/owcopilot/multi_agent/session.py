# MultiAgentSession：驱动一整轮 orchestrator→worker→verifier。
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
      [wave] worker.claim_task(blackboard) → reads from SQLite → runs → writes result
      orchestrator.post_verify_request(...) → writes verify_request → returns
      verifier.verify(verify_req, blackboard) → reads task_result → runs → writes verdict
      orchestrator.synthesize(...)  → reads all results → writes synthesize record

    Each agent's ReActAgent transcript is only touched within that agent's own method; no
    two agents share a transcript reference.

Parallel stage dispatch (Anthropic, *How we built our multi-agent research system* and
*Building Effective Agents* — orchestrator-workers + parallelization/sectioning):
    Dispatch is a two-wave fan-out/join.  Wave 1 fans out every diagnosis subtask, JOINs (the
    lead agent "waits for the whole set of subagents to complete before proceeding"), then wave 2
    fans out every repair subtask — each enriched with the joined wave-1 findings — and JOINs.
    Within a wave the subtasks are mutually independent, so a bounded pool of consumer threads
    drains the wave's blackboard mailbox through the SAME optimistic ``claim_task`` lock (now a
    genuinely contended arbiter, not a dead loss-branch).  Each claimed task is executed by a
    FRESH ``WorkerAgent`` instance (its own ReActAgent = its own "context window"), so concurrent
    branches never clobber a shared transcript.  The default single-diagnosis/single-repair
    topology runs one consumer per wave, so its behaviour is unchanged from the serial driver.

Offline / $0 compliance:
    All agents work with MockProvider (via LLMGateway built in tests).  The ReAct loops
    exhaust max_steps and return "max_steps" stop_reason.  All blackboard writes and reads
    are real SQLite operations.  The session completes and a MultiAgentReport is returned.
    Total LLM cost = $0.
"""

from __future__ import annotations

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from ..agent.reflexion import ReflexionError, distill_reflection
from ..llm.gateway import LLMGateway
from ..skills import SkillRegistry
from .blackboard import AgentBlackboard

if TYPE_CHECKING:
    from ..llm.cache import Embedder
    from ..storage.sqlite import SQLiteStore
from .messages import (
    AgentMessage,
    ReflectionPayload,
    TaskAssignPayload,
    TaskResultPayload,
    VerifyResultPayload,
)
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

    # role → concrete WorkerAgent class, used by ``_spawn_worker`` to build a fresh worker
    # instance per claimed task in a parallel wave (no shared transcript between branches).
    _WORKER_CLASSES: dict[str, type[WorkerAgent]] = {
        "diagnosis": DiagWorker,
        "repair_proposal": RepairWorker,
    }

    def __init__(
        self,
        *,
        gateway: LLMGateway,
        registry: SkillRegistry,
        db_path: str = ":memory:",
        # Allow callers to inject a pre-built conn (e.g. shared with SQLiteStore) or let
        # session open its own in-memory conn for isolation.
        conn: sqlite3.Connection | None = None,
        # Upper bound on concurrent consumer threads per fan-out wave (Anthropic's "3-5
        # subagents in parallel"). The actual count is min(max_workers, subtasks-in-wave), so
        # the default single-diagnosis/single-repair topology still runs one consumer per wave.
        max_workers: int = 4,
        # Episodic memory wiring for the Reflexion cross-trial buffer (Shinn et al., 2023). When
        # BOTH are supplied, a retry's distilled reflection is persisted to ``store`` (keyed by its
        # goal embedding from ``embedder``) and a worker dispatched on a similar subtask recalls it
        # into its reflection slot. Default ``None`` ⇒ no persistence and no recall — the session is
        # byte-identical to the pre-episodic behaviour (the runtime store lives in the project DB,
        # separate from the in-memory blackboard, so wiring it never touches blackboard semantics).
        store: SQLiteStore | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self._gateway = gateway
        self._registry = registry
        self._max_workers = max_workers
        self._store = store
        self._embedder = embedder

        # Set up the shared SQLite connection (WAL mode for safe concurrent read).
        if conn is not None:
            self._conn = conn
        else:
            # check_same_thread=False: the parallel wave drains this one connection from a pool
            # of consumer threads. SQLite serialises access internally (threadsafety==3,
            # SERIALIZED), so one shared connection needs no Python-level mutex — the same
            # pattern platform/store.py:19-21 already uses for its threadpool-shared store. The
            # optimistic conditional-UPDATE in blackboard.claim_task remains the sole claim
            # arbiter; a threading.Lock would instead serialise the board and make that lock
            # un-contendable.
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
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
            # Constrain decomposition to the real routing table (mirrors self._workers below)
            # so the model addresses actual workers; near-miss ids (rep_01 for repair_01) are
            # normalised to the role's registered worker instead of being dropped UNROUTED.
            registered_workers={"diagnosis": "diag_01", "repair_proposal": "repair_01"},
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

        # Worker routing table: agent_id → WorkerAgent instance. Still used by the serial
        # reflexion retry (which re-runs the singleton actor) and by tests that class-patch
        # DiagWorker/RepairWorker.run_task (factory instances share the class, so patches apply).
        self._workers: dict[str, WorkerAgent] = {
            "diag_01": self.diag_worker,
            "repair_01": self.repair_worker,
        }
        # agent_id → role, the inverse routing used to (a) partition subtasks into the diagnosis
        # and repair waves and (b) pick the WorkerAgent class when spawning a fresh worker.
        self._role_by_agent: dict[str, str] = {
            agent_id: worker.role for agent_id, worker in self._workers.items()
        }

    def run(self, goal: str, *, session_id: str | None = None) -> MultiAgentReport:
        """Execute one full multi-agent session and return the synthesized report.

        Flow (blackboard-mediated, not a nested call stack):
        1. Orchestrator decomposes goal → posts task_assign messages to blackboard.
        2. Two-wave fan-out/join: wave 1 runs every diagnosis subtask in parallel and JOINs;
           wave 2 runs every repair subtask (enriched with the joined wave-1 findings) in
           parallel and JOINs. Each wave's consumers claim from the blackboard via the optimistic
           lock and run a FRESH worker per claimed task.
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

        # Step 2: two-wave fan-out/join (Anthropic orchestrator-workers + parallelization).
        # Pre-pass (serial, main thread): split routed vs unrouted assignments. Unrouted ones are
        # recorded here exactly as the serial driver did — posted to the blackboard and surfaced
        # in synthesis, but (as before) NOT sent through the verify loop.
        staged: list[AgentMessage] = []
        for task_msg in task_msgs:
            if task_msg.to_agent in self._workers:
                staged.append(task_msg)
                continue
            # ③ Unknown worker_id — NOT silent continue.
            # Warn loudly and record an unrouted_task message to the blackboard.
            logger.warning(
                "session %s: task_msg %s targets unknown worker_id %r — task unrouted",
                sid,
                task_msg.id,
                task_msg.to_agent,
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

        # Wave 1: fan out every diagnosis subtask, then JOIN (barrier inside _dispatch_stage).
        diag_tasks = [t for t in staged if self._role_by_agent[t.to_agent] == "diagnosis"]
        diag_results = self._dispatch_stage(diag_tasks, sid)

        # ④ Wave 2: enrich each repair subtask with the JOINED wave-1 diagnosis findings, then fan
        # out and JOIN. The enriched assign is POSTED and the original superseded, so the repair
        # consumer claims the enriched version — the real diag→repair handoff. Only diagnosis
        # results that actually ran (stop_reason ∉ {error, unrouted}) carry findings to inject;
        # if none did, repair runs un-enriched, matching the serial ``diag_result_msg is None``.
        successful_diag = [
            r
            for r in diag_results
            if TaskResultPayload.from_dict(r.payload).stop_reason not in ("error", "unrouted")
        ]
        combined_diag = _combine_diag_results(successful_diag)
        repair_tasks = [t for t in staged if self._role_by_agent[t.to_agent] == "repair_proposal"]
        repair_stage_tasks: list[AgentMessage] = []
        for repair_task_msg in repair_tasks:
            stage_task = repair_task_msg
            if combined_diag is not None:
                enriched = _enrich_repair_task(repair_task_msg, combined_diag, sid)
                if enriched is not repair_task_msg:
                    # Mark original generic assign as superseded (not claimable)
                    self.blackboard.update_status(repair_task_msg.id, "done")
                    # Post the enriched assign so the worker claims it
                    self.blackboard.post_message(enriched)
                stage_task = enriched
            repair_stage_tasks.append(stage_task)
        repair_results = self._dispatch_stage(repair_stage_tasks, sid)

        # Verify loop consumes the worker results in stable assignment order (wave 1 then wave 2).
        # This matches the serial driver's routed-result order for the default topology and is
        # deterministic despite the racy per-thread ``created_at`` stamps.
        task_result_msgs: list[AgentMessage] = diag_results + repair_results

        # Step 3 + 4: Orchestrator requests verification; verifier runs independently
        for task_result_msg in task_result_msgs:
            verify_req = self.orchestrator.post_verify_request(
                task_result_msg=task_result_msg,
                session_id=sid,
                blackboard=self.blackboard,
            )
            # Verifier claims verify_request and runs its independent audit.
            # T2: keep its verdict — a fail/needs_more signal now DRIVES a single Reflexion
            # retry (Shinn et al., 2023) instead of only being annotated in synthesis.
            verify_result_msg = self.verifier.verify(verify_req, self.blackboard)
            self._maybe_reflexion_retry(
                task_result_msg=task_result_msg,
                verify_result_msg=verify_result_msg,
                session_id=sid,
            )

        # Step 5: Orchestrator synthesizes all results
        # ② Pass decomposition_degraded through so synthesis annotates it — no silent downgrade
        # Both the initial and any retry task_result/verify_result rows are on the blackboard;
        # synthesize reads them all, so a flipped-to-pass retry and a still-[INCOMPLETE] second
        # trial are both surfaced honestly.
        return self.orchestrator.synthesize(
            goal=goal,
            session_id=sid,
            blackboard=self.blackboard,
            decomposition_degraded=decomposition_degraded,
        )

    # ------------------------------------------------------------------
    # Parallel stage dispatch (Anthropic orchestrator-workers + sectioning)
    # ------------------------------------------------------------------

    def _dispatch_stage(self, stage_tasks: list[AgentMessage], sid: str) -> list[AgentMessage]:
        """Run one fan-out wave of mutually independent subtasks and JOIN before returning.

        A bounded pool of consumer threads drains the wave's blackboard mailbox(es) through the
        EXISTING optimistic ``claim_task`` lock (genuinely contended when a wave holds >1 subtask
        for the same worker). Each claimed task is executed by a FRESH ``WorkerAgent`` (its own
        ReActAgent transcript), so concurrent branches never clobber a shared transcript. Exiting
        the ``with`` block is the wave barrier — Anthropic's "wait for the whole set of subagents
        to complete before proceeding". Results are returned in stable *assignment* order, not
        completion order, because ``created_at`` is stamped per-thread and would otherwise be
        nondeterministic.
        """
        if not stage_tasks:
            return []
        n = len(stage_tasks)
        workers = max(1, min(self._max_workers, n))
        # Distinct mailbox addresses (normalised decomposition ⇒ one per wave), first-seen order.
        mailboxes = list(dict.fromkeys(t.to_agent for t in stage_tasks))
        # assign msg id → assignment index, for deterministic post-join ordering.
        order = {t.id: i for i, t in enumerate(stage_tasks)}
        # Recall the Reflexion-buffer lessons for every task HERE, on the main thread, before the
        # pool starts — the runtime store's SQLite connection is single-threaded, so it must not be
        # touched from a consumer thread. This is the "query once when composing the tasks" point.
        reflections_by_task = {t.id: self._reflections_for_task(t) for t in stage_tasks}

        def _consume() -> list[AgentMessage]:
            produced: list[AgentMessage] = []
            while True:
                claimed_any = False
                for addr in mailboxes:
                    claimed = self.blackboard.claim_task(addr, sid)
                    if claimed is None:
                        continue  # another consumer won this one (or the mailbox is drained)
                    claimed_any = True
                    worker = self._spawn_worker(self._role_by_agent[addr], addr)
                    produced.append(
                        self._run_one_claimed(
                            worker, claimed, sid, reflections_by_task.get(claimed.id)
                        )
                    )
                if not claimed_any:
                    return produced

        collected: list[AgentMessage] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for future in [pool.submit(_consume) for _ in range(workers)]:
                collected.extend(future.result())

        collected.sort(
            key=lambda m: order.get(TaskResultPayload.from_dict(m.payload).task_msg_id, len(order))
        )
        return collected

    def _run_one_claimed(
        self,
        worker: WorkerAgent,
        claimed: AgentMessage,
        sid: str,
        reflections: list[str] | None = None,
    ) -> AgentMessage:
        """Run one claimed task; record a crash as a failed task_result instead of swallowing it.

        ① A worker crash is caught and turned into a ``stop_reason='error'`` task_result on the
        blackboard (the claimed assign marked 'failed', not orphaned 'claimed') — never a phantom
        code=0 success. Because this runs inside the consumer, one branch's crash cannot sink the
        rest of the wave.

        ``reflections`` are the recalled Reflexion-buffer lessons for this subtask, retrieved on the
        main thread by the caller (the store connection is single-threaded) and primed into the
        actor's reflection slot — a past failure on a similar subtask conditions the very first
        attempt, not only the single-shot retry. ``None`` when no store/embedder is wired.
        """
        try:
            return worker.run_task(claimed, self.blackboard, reflections=reflections)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "session %s: worker %r raised during run_task: %s",
                sid,
                worker.agent_id,
                exc,
                exc_info=True,
            )
            self.blackboard.update_status(claimed.id, "failed")
            failed_result = AgentMessage(
                session_id=sid,
                from_agent=worker.agent_id,
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
            return failed_result

    def _spawn_worker(self, role: str, agent_id: str) -> WorkerAgent:
        """Build a FRESH worker instance (own gateway/registry/ReActAgent) for one claimed task.

        Factory instances share the concrete class with the ``self.diag_worker`` /
        ``self.repair_worker`` singletons (via ``_WORKER_CLASSES``), so class-level test patches on
        ``DiagWorker.run_task`` / ``RepairWorker.run_task`` apply to spawned workers too.
        """
        worker_cls = self._WORKER_CLASSES[role]
        return worker_cls(agent_id=agent_id, gateway=self._gateway, registry=self._registry)

    # ------------------------------------------------------------------
    # T2: single-shot Reflexion retry (Shinn et al., 2023)
    # ------------------------------------------------------------------

    def _maybe_reflexion_retry(
        self,
        *,
        task_result_msg: AgentMessage,
        verify_result_msg: AgentMessage,
        session_id: str,
    ) -> None:
        """Close the evaluator→optimizer loop for ONE bounded retry when the verifier flags a hit.

        This is the Reflexion (Shinn et al., 2023) / evaluator-optimizer (Anthropic, *Building
        Effective Agents*) pattern, bounded to exactly one iteration:
          - **Evaluator** = the :class:`VerifierAgent` verdict already computed in the main loop;
            ``fail``/``needs_more`` is the coarse failure signal the paper sanctions.
          - **Actor** = the same :class:`WorkerAgent`, re-run with episodic memory.
          - **Self-Reflection** = :func:`~owcopilot.agent.reflexion.distill_reflection`, reused
            verbatim.

        Guards (never retried): a passing verdict (default behaviour stays inert), a result from a
        non-registered sender (session/unrouted placeholders never map to a worker), and a crashed
        or unrouted placeholder (``stop_reason`` in ``{"error", "unrouted"}``) which has no honest
        trajectory to reflect on.
        """
        verdict = VerifyResultPayload.from_dict(verify_result_msg.payload).verdict
        if verdict not in ("fail", "needs_more"):
            return  # pass → the loop is inert (pre-T2 behaviour, proven unchanged by tests)

        worker = self._workers.get(task_result_msg.from_agent)
        if worker is None:
            # A non-registered sender (e.g. the session-posted UNROUTED placeholder from_agent
            # "session") has no worker to re-run — never retry it.
            return

        result_payload = TaskResultPayload.from_dict(task_result_msg.payload)
        if result_payload.stop_reason in ("error", "unrouted"):
            # Crashed / unrouted placeholder — there is no real actor trajectory to reflect on.
            return

        self._reflexion_retry(
            worker=worker,
            task_result_msg=task_result_msg,
            result_payload=result_payload,
            verify_result_msg=verify_result_msg,
            verdict=verdict,
            session_id=session_id,
        )

    def _reflexion_retry(
        self,
        *,
        worker: WorkerAgent,
        task_result_msg: AgentMessage,
        result_payload: TaskResultPayload,
        verify_result_msg: AgentMessage,
        verdict: str,
        session_id: str,
    ) -> None:
        """Run exactly ONE Reflexion retry: distil a reflection, re-run the actor, re-verify once.

        No loop and no recursion — a still-failing retry is NOT retried again; the second verdict
        stands and is surfaced by synthesis (the existing ``[INCOMPLETE]`` / fail annotation).
        Every failure mode (missing task_assign, a blank Self-Reflection, or a retry crash) is
        logged AND recorded as a ``reflection`` note on the blackboard — never silently swallowed.
        """
        feedback = VerifyResultPayload.from_dict(verify_result_msg.payload).rationale

        # Reconstruct the task_assign the worker executed — needed to rebuild the exact goal and
        # to feed the retry a real assignment (not a synthetic one).
        assign_msg = self.blackboard.get_message(result_payload.task_msg_id)
        if assign_msg is None or assign_msg.msg_type != "task_assign":
            logger.warning(
                "session %s: reflexion retry for %s aborted — task_assign %r not found",
                session_id,
                worker.agent_id,
                result_payload.task_msg_id,
            )
            self._post_reflection(
                session_id=session_id,
                worker_id=worker.agent_id,
                target_msg_id=task_result_msg.id,
                trigger_verdict=verdict,
                feedback=feedback,
                reflection="",
                note=(
                    f"retry aborted: task_assign {result_payload.task_msg_id!r} "
                    "not found on blackboard"
                ),
            )
            return

        assign = TaskAssignPayload.from_dict(assign_msg.payload)
        goal = worker.build_goal(assign)

        # Self-Reflection (T1 piece): distil ONE verbal reflection from the evaluator feedback.
        # Offline ($0) this is the gated deterministic template; online it is one LLM call through
        # the worker's own gateway/task. A blank reflection raises ReflexionError — not swallowed.
        try:
            reflection = distill_reflection(
                agent=worker.agent, goal=goal, trial_index=0, feedback=feedback
            )
        except ReflexionError as exc:
            logger.warning(
                "session %s: reflexion retry for %s aborted — self-reflection failed: %s",
                session_id,
                worker.agent_id,
                exc,
            )
            self._post_reflection(
                session_id=session_id,
                worker_id=worker.agent_id,
                target_msg_id=task_result_msg.id,
                trigger_verdict=verdict,
                feedback=feedback,
                reflection="",
                note=f"retry aborted: self-reflection failed ({exc})",
            )
            return

        # Persist the distilled reflection as a cross-trial Reflexion-buffer entry (Shinn et al.,
        # 2023): a future run at a semantically-similar goal recalls it into its reflection slot.
        # Best-effort and no-op when no store/embedder is wired — never blocks the retry below.
        self._persist_reflection(goal=goal, reflection=reflection)

        # Actor (T1 piece): re-run the worker with the reflection as episodic memory. A crash here
        # is recorded, mirroring the main-loop worker guard — a retry must not sink the session.
        try:
            retry_result_msg = worker.run_task(
                assign_msg, self.blackboard, reflections=[reflection]
            )
        except Exception as exc:  # noqa: BLE001 — record, never swallow
            logger.error(
                "session %s: reflexion retry for %s crashed during run_task: %s",
                session_id,
                worker.agent_id,
                exc,
                exc_info=True,
            )
            self._post_reflection(
                session_id=session_id,
                worker_id=worker.agent_id,
                target_msg_id=task_result_msg.id,
                trigger_verdict=verdict,
                feedback=feedback,
                reflection=reflection,
                note=f"retry run crashed: {exc!r}",
            )
            return

        # Evaluator (bounded): verify the retry exactly ONCE. This verdict is NOT fed back into the
        # main verify loop — the retry result is intentionally kept out of task_result_msgs, so no
        # second retry can fire.
        retry_verify_req = self.orchestrator.post_verify_request(
            task_result_msg=retry_result_msg,
            session_id=session_id,
            blackboard=self.blackboard,
        )
        retry_verify_msg = self.verifier.verify(retry_verify_req, self.blackboard)

        # Record the full Reflexion trail on the blackboard (P3-6 attributed, terminal 'done').
        self._post_reflection(
            session_id=session_id,
            worker_id=worker.agent_id,
            target_msg_id=task_result_msg.id,
            trigger_verdict=verdict,
            feedback=feedback,
            reflection=reflection,
            retry_task_result_id=retry_result_msg.id,
            retry_verify_result_id=retry_verify_msg.id,
        )

    def _reflections_for_task(self, task_msg: AgentMessage) -> list[str] | None:
        """Recall the Reflexion-buffer lessons for one task_assign (``None`` when none).

        Runs on the MAIN thread (before any consumer pool), so the single-threaded runtime store is
        never touched cross-thread. Keyed on the SAME goal the worker will run — built via the
        target worker's own :meth:`build_goal` so the recall query matches how reflections are
        persisted (no goal-format drift). No-op (``None``) without a store/embedder, and fail-open
        on any recall error so dispatch is never broken by memory retrieval.
        """
        if self._store is None or self._embedder is None:
            return None
        worker = self._workers.get(task_msg.to_agent)
        if worker is None:
            return None  # unrouted placeholder — no worker, nothing to key a recall on
        try:
            from ..agent.episodic import recall_reflections  # noqa: PLC0415 — lazy, avoid cycle

            goal = worker.build_goal(TaskAssignPayload.from_dict(task_msg.payload))
            hits = recall_reflections(self._store, self._embedder, goal)
        except Exception as exc:  # noqa: BLE001 — recall is best-effort; never break dispatch
            logger.warning(
                "reflection recall failed for %s (%s); dispatching without recalled memory",
                task_msg.to_agent,
                exc,
            )
            return None
        return [hit.reflection for hit in hits] or None

    def _persist_reflection(self, *, goal: str, reflection: str) -> None:
        """Persist one distilled reflection to the cross-trial buffer.

        No-op without a store/embedder. Best-effort: a persistence error is logged, never raised,
        so it cannot sink the retry that produced the reflection."""
        if self._store is None or self._embedder is None:
            return
        try:
            from ..agent.episodic import persist_reflection  # noqa: PLC0415 — lazy, avoid cycle

            persist_reflection(
                self._store,
                self._embedder,
                goal=goal,
                reflection=reflection,
                source="multi_agent_retry",
            )
        except Exception as exc:  # noqa: BLE001 — persistence must not break the Reflexion retry
            logger.warning("failed to persist reflection for goal %r: %s", goal, exc)

    def _post_reflection(
        self,
        *,
        session_id: str,
        worker_id: str,
        target_msg_id: str,
        trigger_verdict: str,
        feedback: str,
        reflection: str,
        retry_task_result_id: str | None = None,
        retry_verify_result_id: str | None = None,
        note: str = "",
    ) -> AgentMessage:
        """Post the ``reflection`` trail record. Attributed worker→worker (the actor reflecting on
        itself and consuming the note), terminal ``done`` — it is never claimed."""
        payload = ReflectionPayload(
            target_msg_id=target_msg_id,
            worker=worker_id,
            trigger_verdict=trigger_verdict,
            feedback=feedback,
            reflection=reflection,
            retry_task_result_id=retry_task_result_id,
            retry_verify_result_id=retry_verify_result_id,
            note=note,
        )
        msg = AgentMessage(
            session_id=session_id,
            from_agent=worker_id,
            to_agent=worker_id,
            msg_type="reflection",
            payload=payload.to_dict(),
            status="done",
        )
        self.blackboard.post_message(msg)
        return msg

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


def _combine_diag_results(results: list[AgentMessage]) -> AgentMessage | None:
    """Fold the wave-1 diagnosis results into the single findings message repair is enriched with.

    - 0 results → ``None`` (repair runs un-enriched, matching the serial ``diag_result_msg`` None).
    - 1 result → that message UNCHANGED, so ``_enrich_repair_task`` builds a byte-identical repair
      subtask for the default single-diagnosis topology (backward-compat hard requirement).
    - >1 results → concatenate every diagnosis final_answer in stable assignment order into one
      transient findings message, so a multi-diagnosis wave hands ALL findings to repair (none
      dropped). This message is used only to enrich the repair subtask; it is not posted to the
      blackboard (the per-worker results already are).
    """
    if not results:
        return None
    if len(results) == 1:
        return results[0]
    payloads = [TaskResultPayload.from_dict(r.payload) for r in results]
    combined_answer = "\n\n".join(
        f"[{p.worker_role} #{i + 1}] {p.final_answer}" for i, p in enumerate(payloads)
    )
    first = results[0]
    combined_payload = TaskResultPayload(
        task_msg_id=payloads[0].task_msg_id,
        worker_role="diagnosis",
        final_answer=combined_answer,
        open_errors=None,
        stop_reason="finished",
        step_count=0,
    )
    return AgentMessage(
        session_id=first.session_id,
        from_agent=first.from_agent,
        to_agent="orchestrator",
        msg_type="task_result",
        payload=combined_payload.to_dict(),
        status="done",
    )


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
