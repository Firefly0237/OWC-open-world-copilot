# MultiAgentSession：驱动一整轮 orchestrator→worker→verifier。
"""MultiAgentSession — drives one complete orchestrator-worker-verifier round.

This is the top-level entry point for a multi-agent run.  It wires together:
  - OrchestratorAgent (task decomposition + synthesis)
  - DiagWorker and RepairWorker (subtask execution)
  - VerifierAgent (independent verification)
  - AgentBlackboard (all inter-agent communication)

Execution structure:
    "Multiple agents must not be in the same synchronous call chain (can be asyncio tasks or
     LangGraph node-to-node state passing, but not nested function calls)."

    Messages are handed off through the blackboard: the orchestrator does not call workers
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

Parallel stage dispatch (orchestrator-workers with a complete-wave join):
    Dispatch is a LangGraph ``StateGraph`` supervisor:
        plan → Send("diag_worker") × N → join_diag → Send("repair_worker") × M → verify → synthesize
    ``Send`` fans out each subtask (one packet per assignment id) and a superstep JOIN barrier at
    the downstream node is a complete-wave join barrier: ``join_diag`` runs once after ALL
    diagnosis workers, so repair is enriched with the
    complete wave-1 findings before it fans out. ``config={"max_concurrency": …}`` bounds the
    per-wave concurrency (replacing the old ``ThreadPoolExecutor(max_workers=…)``), and the reducer
    on ``diag_results``/``repair_results`` merges each worker's result id in Send-index order =
    assignment order (so no post-hoc sort is needed).

    The blackboard is retained as the append-only audit + business channel. Its optimistic claim
    primitive changes ROLE here: a ``Send`` routes each ``assign_id`` directly, so the worker node
    claims that SPECIFIC id via :meth:`AgentBlackboard.claim_assignment` — a single-claim
    idempotency gate (a duplicate in-process Send for an already-run id is refused), not a
    mailbox-drain arbiter. This prevents duplicate execution after a successful claim; it does not
    provide at-least-once delivery or safe cross-process replay after a crash. Each claimed task is
    executed by a FRESH ``WorkerAgent`` (its own
    ReActAgent = its own "context window"), so concurrent branches never clobber a shared
    transcript, and the crash guard lives INSIDE the worker node (an unhandled exception there would
    otherwise abort the whole supervisor ``invoke``). A single-Send wave (the default
    single-diagnosis/single-repair topology) runs the node inline on the caller thread — LangGraph's
    single-task fast path — which is correct: there is no parallelism to gain from one task. Real
    parallelism (a wave of ≥2 subtasks) runs the workers concurrently on pool threads.

Offline / $0 compliance:
    Worker/verifier reasoning-and-tool loop loops run against deterministic chat-model fakes in
    tests, and the
    orchestrator's decompose calls run against a non-JSON chat-model fake (so decomposition
    degrades to the static fallback).  All blackboard writes and reads are real SQLite
    operations.  The session completes and a MultiAgentReport is returned.  Total LLM cost = $0.
"""

from __future__ import annotations

import logging
import operator
import sqlite3
import uuid
from typing import TYPE_CHECKING, Annotated, Any, TypedDict, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ..agent.middleware import SettledCostBudget, TotalModelCallBudget, TotalToolCallBudget
from ..agent.reflexion import ReflexionError, distill_reflection
from ..llm.telemetry import TelemetryCollector
from ..skills import SkillRegistry
from ..storage.checkpointer import resolve_thread_id
from .blackboard import AgentBlackboard

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

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

# Hard aggregate default across decomposition, every worker/verifier reasoning-and-tool loop path,
# context
# compression and feedback-memory. Per-agent limits still shape individual work; this prevents
# fan-out
# from multiplying model dispatches without a session ceiling.
DEFAULT_SESSION_MAX_MODEL_CALLS = 128
DEFAULT_SESSION_MAX_TOOL_CALLS = 256


class _OrchestrationState(TypedDict):
    """LangGraph channels for one multi-agent run.

    Deliberately thin: it carries only REFERENCES (message ids) and FLAGS, never business content.
    Every payload / transcript / verdict / reflection lives on the SQLite blackboard — the graph
    state just threads the ids that let each node read/write those rows in the right order.

    * ``diag_results`` / ``repair_results`` use the ``operator.add`` reducer, so each ``Send``
      worker's one-element ``[result_id]`` write is merged in *Send-index order* = assignment order
      (LangGraph merges reducer writes by the emitted Send list index, not completion time — see
      scratchpad ``verify_t3_send.py``). That is what lets the verify loop consume results in stable
      assignment order WITHOUT the old ``collected.sort`` — the ordering is now a property of the
      fan-out, not a post-hoc re-sort.
    """

    goal: str
    session_id: str
    decomposition_degraded: bool
    diag_assign_ids: list[str]
    repair_assign_ids: list[str]
    diag_results: Annotated[list[str], operator.add]
    repair_results: Annotated[list[str], operator.add]
    report: MultiAgentReport | None


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
        # The orchestrator's decompose calls run on their own LangChain chat model (a distinct
        # constructor param so a studio may route decomposition to a different/cheaper model);
        # the worker/verifier reasoning-and-tool loop loops run on ``model``. Production passes
        # one instance to both.
        orchestrator_model: BaseChatModel,
        model: BaseChatModel,
        registry: SkillRegistry,
        db_path: str = ":memory:",
        # Allow callers to inject a pre-built conn (e.g. shared with SQLiteStore) or let
        # session open its own in-memory conn for isolation.
        conn: sqlite3.Connection | None = None,
        # Upper bound on concurrent consumer threads per fan-out wave.
        # The actual count is min(max_workers, subtasks-in-wave), so
        # the default single-diagnosis/single-repair topology still runs one consumer per wave.
        max_workers: int = 4,
        # Episodic memory wiring for the feedback-memory cross-trial buffer. When
        # BOTH are supplied, a retry's distilled reflection is persisted to ``store`` (keyed by its
        # goal embedding from ``embedder``) and a worker dispatched on a similar subtask recalls it
        # into its reflection slot. Default ``None`` ⇒ no persistence and no recall — the session is
        # byte-identical to the pre-episodic behaviour (the runtime store lives in the project DB,
        # separate from the in-memory blackboard, so wiring it never touches blackboard semantics).
        store: SQLiteStore | None = None,
        embedder: Embedder | None = None,
        # Per-call accounting for the worker/verifier chat-model calls (one shared collector).
        telemetry: TelemetryCollector | None = None,
        # Independent tool-call cap applied to EACH worker/verifier reasoning-and-tool loop loop.
        per_agent_max_tool_calls: int | None = None,
        # Backward-compatible alias. New callers should use ``per_agent_max_tool_calls`` so the
        # scope cannot be mistaken for a session-wide budget.
        max_tool_calls: int | None = None,
        # One hard model-dispatch ledger shared by decomposition, all parallel workers, verifier
        # and feedback-memory. The cost gate uses settled telemetry: in-flight calls may cross the
        # threshold, but every later dispatch is blocked.
        session_max_model_calls: int | None = DEFAULT_SESSION_MAX_MODEL_CALLS,
        session_max_tool_calls: int | None = DEFAULT_SESSION_MAX_TOOL_CALLS,
        session_max_cost_usd: float | None = None,
        # T4 (scheme Y): a LangGraph checkpointer compiled onto the supervisor graph. Default None
        # ⇒ NO checkpointer, the report is byte-identical to the pre-T4 run. When attached it serves
        # IN-PROCESS resume — a clean interrupt suspend/continue (the T6 HITL substrate). Cross-
        # process (kill-9) resume of a multi-agent run is deliberately NOT promised: the blackboard
        # (default :memory:) and the checkpoint are two independently-committed stores, so a crash
        # between a claim commit and its result post can drop a task on cross-process replay (see
        # the design's §3.2). In-process suspend stops cleanly at a node boundary with the pending
        # write stored and the blackboard connection still live, so no such window opens.
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> None:
        self._orchestrator_model = orchestrator_model
        self._model = model
        self._registry = registry
        self._max_workers = max_workers
        self._store = store
        self._embedder = embedder
        if session_max_model_calls is not None and session_max_model_calls < 1:
            raise ValueError("session_max_model_calls must be >= 1")
        if session_max_tool_calls is not None and session_max_tool_calls < 1:
            raise ValueError("session_max_tool_calls must be >= 1")
        if session_max_cost_usd is not None and session_max_cost_usd < 0:
            raise ValueError("session_max_cost_usd must be >= 0")
        self._telemetry = telemetry or (
            TelemetryCollector() if session_max_cost_usd is not None else None
        )
        self._session_model_budget = (
            TotalModelCallBudget(session_max_model_calls)
            if session_max_model_calls is not None
            else None
        )
        self._session_tool_budget = (
            TotalToolCallBudget(session_max_tool_calls)
            if session_max_tool_calls is not None
            else None
        )
        self._session_cost_budget = (
            SettledCostBudget(session_max_cost_usd, collector=self._telemetry)
            if session_max_cost_usd is not None
            else None
        )
        if (
            per_agent_max_tool_calls is not None
            and max_tool_calls is not None
            and per_agent_max_tool_calls != max_tool_calls
        ):
            raise ValueError(
                "per_agent_max_tool_calls and legacy max_tool_calls disagree; provide one value"
            )
        self._per_agent_max_tool_calls = (
            per_agent_max_tool_calls if per_agent_max_tool_calls is not None else max_tool_calls
        )
        self._checkpointer = checkpointer
        # The thread_id the LAST run used (auto-minted when a checkpointer is attached and none was
        # passed); the assembly layer surfaces it.
        self.last_thread_id: str | None = None

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

        # Instantiate all four agents — each with its own LLM call path and transcript.
        # The chat model instance may be shared (it is stateless per call), but each agent
        # creates its own ReActAgent which owns an independent transcript.
        self.orchestrator = OrchestratorAgent(
            agent_id="orchestrator",
            model=orchestrator_model,
            registry=registry,
            # Constrain decomposition to the real routing table (mirrors self._workers below)
            # so the model addresses actual workers; near-miss ids (rep_01 for repair_01) are
            # normalised to the role's registered worker instead of being dropped UNROUTED.
            registered_workers={"diagnosis": "diag_01", "repair_proposal": "repair_01"},
            # Bill the decompose calls into the shared collector (§1.3) — parity with the retired
            # gateway telemetry; None ⇒ decompose cost is simply not recorded.
            telemetry=self._telemetry,
            shared_model_budget=self._session_model_budget,
            shared_cost_budget=self._session_cost_budget,
        )
        self.diag_worker = DiagWorker(
            agent_id="diag_01",
            model=model,
            registry=registry,
            max_tool_calls=self._per_agent_max_tool_calls,
            telemetry=self._telemetry,
            shared_model_budget=self._session_model_budget,
            shared_cost_budget=self._session_cost_budget,
            shared_tool_budget=self._session_tool_budget,
        )
        self.repair_worker = RepairWorker(
            agent_id="repair_01",
            model=model,
            registry=registry,
            max_tool_calls=self._per_agent_max_tool_calls,
            telemetry=self._telemetry,
            shared_model_budget=self._session_model_budget,
            shared_cost_budget=self._session_cost_budget,
            shared_tool_budget=self._session_tool_budget,
        )
        self.verifier = VerifierAgent(
            agent_id="verifier_01",
            model=model,
            registry=registry,
            max_tool_calls=self._per_agent_max_tool_calls,
            telemetry=self._telemetry,
            shared_model_budget=self._session_model_budget,
            shared_cost_budget=self._session_cost_budget,
            shared_tool_budget=self._session_tool_budget,
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

        # Per-run recall cache: assign_id → recalled feedback-memory lessons (or None). Populated
        # on the
        # SOLO plan/join supersteps (serial store access — never concurrent, so the store's shared
        # connection is safe) and READ by the parallel worker nodes. Reset at the top of ``run`` so
        # a prior run's recall never leaks; inert (stays empty) unless a store+embedder are wired.
        self._reflections_by_assign: dict[str, list[str] | None] = {}

        # Compile the supervisor graph once; it reads per-run state from the graph channels and the
        # recall cache above, so a single compiled instance is reused across runs.
        self._graph = self._build_graph()

    def run(
        self,
        goal: str = "",
        *,
        session_id: str | None = None,
        thread_id: str | None = None,
        resume: bool = False,
    ) -> MultiAgentReport:
        """Execute one full multi-agent session and return the synthesized report.

        Drives the LangGraph supervisor graph (plan → Send diag workers → join_diag → Send repair
        workers → verify → synthesize). Communication is blackboard-mediated throughout — the graph
        state threads only ids/flags between nodes, never a nested Python call chain (P3-7).

        Resume (T4, superset signature — IN-PROCESS only, scheme Y): with a checkpointer attached,
        ``resume=True`` + ``thread_id`` continues a suspended graph (``invoke(None)``) from its last
        superstep. This is the substrate for T6 in-process HITL (a clean ``interrupt`` suspend, a
        human decision, then continue). It is NOT a cross-process crash-resume: the recall cache and
        blackboard connection are live only within this instance, so a fresh process cannot resume a
        killed multi-agent run — that boundary is deliberate (design §3). The default call (no
        checkpointer, no thread_id) is byte-identical to the pre-T4 run.
        """
        config: RunnableConfig = {
            # Bounds the per-wave concurrency (LangGraph caps the executor at this many workers) —
            # the declarative replacement for the old ``ThreadPoolExecutor(max_workers=…)``.
            "max_concurrency": max(1, self._max_workers)
        }
        effective_thread_id = resolve_thread_id(
            self._checkpointer, thread_id=thread_id, resume=resume
        )
        self.last_thread_id = effective_thread_id
        if effective_thread_id is not None:
            config["configurable"] = {"thread_id": effective_thread_id}

        if resume:
            # Continue the suspended graph; its channels (session_id, ids, recall) are in the
            # checkpoint / this live instance. The recall cache is NOT reset — a resume mid-run
            # must keep the memory the plan/join supersteps populated.
            final_state = self._graph.invoke(None, config=config)
        else:
            sid = session_id or str(uuid.uuid4())
            # Reset the recall cache for this run (see the attribute's declaration in __init__).
            self._reflections_by_assign = {}
            final_state = self._graph.invoke(
                {
                    "goal": goal,
                    "session_id": sid,
                    "decomposition_degraded": False,
                    "diag_assign_ids": [],
                    "repair_assign_ids": [],
                    "diag_results": [],
                    "repair_results": [],
                    "report": None,
                },
                config=config,
            )
        report = final_state["report"]
        # ``synthesize`` is the terminal node and always populates ``report``; the guard keeps the
        # public return type honest (never a silent None) if the graph is ever short-circuited.
        assert report is not None, "synthesize node must populate the report"
        return cast(MultiAgentReport, report)

    # ------------------------------------------------------------------
    # Supervisor graph (LangGraph StateGraph — orchestrator-workers + sectioning)
    # ------------------------------------------------------------------

    def _build_graph(
        self,
    ) -> CompiledStateGraph[_OrchestrationState, None, _OrchestrationState, _OrchestrationState]:
        """Wire the two-wave supervisor: plan → diag Send/JOIN → repair Send/JOIN → verify → synth.

        The conditional edges fan each wave out as one ``Send`` per assignment id, and the plain
        ``worker → join`` edges are the superstep JOIN barriers. An empty wave routes straight to
        the downstream node (returning ``[]`` from a conditional edge would route NOWHERE —
        LangGraph writes no edge for an empty list — so a fallback node NAME is returned instead).
        """
        g = StateGraph(_OrchestrationState)
        g.add_node("plan", self._plan_node)
        # The two worker nodes receive a Send PACKET (``{"assign_id", "session_id"}``), not the full
        # state — a contract LangGraph's static ``add_node`` types cannot express, so the arg-type
        # mismatch is ignored here; the other four nodes take the state and type-check.
        g.add_node("diag_worker", self._diag_worker_node)  # type: ignore[arg-type]
        g.add_node("join_diag", self._join_diag_node)
        g.add_node("repair_worker", self._repair_worker_node)  # type: ignore[arg-type]
        g.add_node("verify", self._verify_node)
        g.add_node("synthesize", self._synthesize_node)

        g.add_edge(START, "plan")
        g.add_conditional_edges("plan", self._fan_diag, ["diag_worker", "join_diag"])
        g.add_edge("diag_worker", "join_diag")  # wave-1 JOIN barrier
        g.add_conditional_edges("join_diag", self._fan_repair, ["repair_worker", "verify"])
        g.add_edge("repair_worker", "verify")  # wave-2 JOIN barrier
        g.add_edge("verify", "synthesize")
        g.add_edge("synthesize", END)
        # None ⇒ the compile default (no checkpointer): the run is byte-identical to the pre-T4
        # graph. A checkpointer persists the supervisor's channels for in-process resume (scheme Y).
        return g.compile(checkpointer=self._checkpointer)

    def _plan_node(self, state: _OrchestrationState) -> dict[str, object]:
        """Decompose the goal, post assignments, split routed vs unrouted, recall diag reflections.

        Runs as a solo superstep (no concurrent siblings), so the single-threaded-safe recall of
        feedback-memory lessons for the diagnosis wave happens here — before any parallel Send.
        """
        goal = state["goal"]
        sid = state["session_id"]

        # ② post_task_assignments returns (msgs, degraded) — degraded must be surfaced.
        task_msgs, degraded = self.orchestrator.post_task_assignments(
            goal=goal, session_id=sid, blackboard=self.blackboard
        )

        diag_assign_ids: list[str] = []
        repair_assign_ids: list[str] = []
        for task_msg in task_msgs:
            if task_msg.to_agent in self._workers:
                role = self._role_by_agent[task_msg.to_agent]
                if role == "diagnosis":
                    diag_assign_ids.append(task_msg.id)
                elif role == "repair_proposal":
                    repair_assign_ids.append(task_msg.id)
                continue
            # ③ Unknown worker_id — NOT a silent continue. Record an unrouted task_result on the
            # blackboard (surfaced in synthesis, but NOT sent through the verify loop, as before).
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
            self.blackboard.update_status(task_msg.id, "failed")

        self._recall_into_cache(diag_assign_ids)

        return {
            "decomposition_degraded": degraded,
            "diag_assign_ids": diag_assign_ids,
            "repair_assign_ids": repair_assign_ids,
        }

    def _fan_diag(self, state: _OrchestrationState) -> list[Send] | list[str]:
        """Fan out one ``Send`` per diagnosis assignment; route straight to join_diag if none."""
        assign_ids = state["diag_assign_ids"]
        if not assign_ids:
            return ["join_diag"]
        return [
            Send("diag_worker", {"assign_id": aid, "session_id": state["session_id"]})
            for aid in assign_ids
        ]

    def _diag_worker_node(self, arg: dict[str, str]) -> dict[str, list[str]]:
        """Execute one diagnosis assignment routed by ``Send`` (see ``_execute_send``)."""
        result_id = self._execute_send(arg)
        return {"diag_results": [result_id] if result_id is not None else []}

    def _join_diag_node(self, state: _OrchestrationState) -> dict[str, list[str]]:
        """Wave-1 JOIN: fold ALL diagnosis findings into the repair assigns, recall repair memory.

        ④ Runs once after every diagnosis worker (superstep barrier), so repair sees the COMPLETE
        wave-1 findings. Each repair assign is enriched with the joined diagnosis final_answer (the
        original generic assign superseded to 'done', the enriched one posted so the worker claims
        it) — the real diag→repair handoff. Only diagnosis results that actually ran
        (stop_reason ∉ {error, unrouted}) carry findings; if none did, repair runs un-enriched.
        This is also a solo superstep, so the repair-wave reflexion recall (keyed on the ENRICHED
        goal) is done here, off the parallel path.
        """
        sid = state["session_id"]

        diag_msgs = [self.blackboard.get_message(rid) for rid in state["diag_results"]]
        successful_diag = [
            m
            for m in diag_msgs
            if m is not None
            and TaskResultPayload.from_dict(m.payload).stop_reason not in ("error", "unrouted")
        ]
        combined_diag = _combine_diag_results(successful_diag)

        staged_repair_ids: list[str] = []
        for rid in state["repair_assign_ids"]:
            repair_task_msg = self.blackboard.get_message(rid)
            if repair_task_msg is None:
                continue  # defensive — an assign we just posted must exist
            stage_task = repair_task_msg
            if combined_diag is not None:
                enriched = _enrich_repair_task(repair_task_msg, combined_diag, sid)
                if enriched is not repair_task_msg:
                    self.blackboard.update_status(repair_task_msg.id, "done")  # supersede original
                    self.blackboard.post_message(enriched)  # post enriched so the worker claims it
                stage_task = enriched
            staged_repair_ids.append(stage_task.id)

        self._recall_into_cache(staged_repair_ids)

        return {"repair_assign_ids": staged_repair_ids}

    def _fan_repair(self, state: _OrchestrationState) -> list[Send] | list[str]:
        """Fan out one ``Send`` per (enriched) repair assignment; route to verify if none."""
        assign_ids = state["repair_assign_ids"]
        if not assign_ids:
            return ["verify"]
        return [
            Send("repair_worker", {"assign_id": aid, "session_id": state["session_id"]})
            for aid in assign_ids
        ]

    def _repair_worker_node(self, arg: dict[str, str]) -> dict[str, list[str]]:
        """Execute one repair assignment routed by ``Send`` (see ``_execute_send``)."""
        result_id = self._execute_send(arg)
        return {"repair_results": [result_id] if result_id is not None else []}

    def _verify_node(self, state: _OrchestrationState) -> dict[str, object]:
        """Serial verify loop over every worker result (wave 1 then wave 2, assignment order).

        The verifier runs its independent audit for each result, and a fail/needs_more verdict
        DRIVES a single bounded feedback-memory retry . This whole step is a solo
        superstep — the single-shot retry re-runs the actor and re-verifies IN PLACE and never emits
        a new Send, so no retry can cascade into a fresh wave.
        """
        sid = state["session_id"]
        result_ids = list(state["diag_results"]) + list(state["repair_results"])
        for rid in result_ids:
            task_result_msg = self.blackboard.get_message(rid)
            if task_result_msg is None:
                continue  # defensive
            verify_req = self.orchestrator.post_verify_request(
                task_result_msg=task_result_msg,
                session_id=sid,
                blackboard=self.blackboard,
            )
            verify_result_msg = self.verifier.verify(verify_req, self.blackboard)
            self._maybe_reflexion_retry(
                task_result_msg=task_result_msg,
                verify_result_msg=verify_result_msg,
                session_id=sid,
            )
        return {}

    def _synthesize_node(self, state: _OrchestrationState) -> dict[str, MultiAgentReport]:
        """Terminal node: read all blackboard results → synthesized report (deterministic, no LLM).

        ② decomposition_degraded is threaded through so synthesis annotates it. Both the initial and
        any retry task_result/verify_result rows are on the blackboard, so a flipped-to-pass retry
        and a still-[INCOMPLETE] second trial are both surfaced honestly.
        """
        report = self.orchestrator.synthesize(
            goal=state["goal"],
            session_id=state["session_id"],
            blackboard=self.blackboard,
            decomposition_degraded=state["decomposition_degraded"],
        )
        return {"report": report}

    def _execute_send(self, arg: dict[str, str]) -> str | None:
        """Run ONE ``Send``-routed assignment: idempotent-claim → fresh worker → guarded run.

        Returns the posted task_result msg id, or ``None`` when the claim gate refused the assign
        (already executed — a re-dispatched / checkpoint-recovered Send), so the wave records no
        duplicate result. The crash guard (:meth:`_run_one_claimed`) is node-internal by design: an
        unhandled exception raised inside a ``Send`` node aborts the ENTIRE supervisor ``invoke`` (a
        sibling's crash would sink the whole wave), whereas the serial driver's contract is "one
        worker crash ⇒ one failed task_result, the rest of the wave still runs".
        """
        assign_id = arg["assign_id"]
        sid = arg["session_id"]
        claimed = self.blackboard.claim_assignment(assign_id)
        if claimed is None:
            # Idempotent gate: this assignment was already claimed/run (a re-dispatched or
            # checkpoint-recovered Send) — do not execute it twice.
            return None
        role = self._role_by_agent[claimed.to_agent]
        worker = self._spawn_worker(role, claimed.to_agent)
        result_msg = self._run_one_claimed(
            worker, claimed, sid, self._reflections_by_assign.get(assign_id)
        )
        return result_msg.id

    def _recall_into_cache(self, assign_ids: list[str]) -> None:
        """Recall the feedback-memory lessons for each assignment into
        ``self._reflections_by_assign``.

        Called only from the solo plan/join supersteps (never a parallel Send), so the runtime
        store's shared connection is accessed serially — the constraint is non-concurrent use, not
        a single owning thread (the store opens ``check_same_thread=False``). Inert without a
        store/embedder (:meth:`_reflections_for_task` returns None), so the default path is a no-op.
        """
        for assign_id in assign_ids:
            task_msg = self.blackboard.get_message(assign_id)
            if task_msg is not None:
                self._reflections_by_assign[assign_id] = self._reflections_for_task(task_msg)

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

        ``reflections`` are the recalled feedback-memory-buffer lessons for this subtask,
        retrieved on the
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
        """Build a FRESH worker instance (own registry scope/ReActAgent) for one claimed task.

        Factory instances share the concrete class with the ``self.diag_worker`` /
        ``self.repair_worker`` singletons (via ``_WORKER_CLASSES``), so class-level test patches on
        ``DiagWorker.run_task`` / ``RepairWorker.run_task`` apply to spawned workers too.
        """
        worker_cls = self._WORKER_CLASSES[role]
        return worker_cls(
            agent_id=agent_id,
            model=self._model,
            registry=self._registry,
            max_tool_calls=self._per_agent_max_tool_calls,
            telemetry=self._telemetry,
            shared_model_budget=self._session_model_budget,
            shared_cost_budget=self._session_cost_budget,
            shared_tool_budget=self._session_tool_budget,
        )

    # ------------------------------------------------------------------
    # T2: single-shot feedback-memory retry
    # ------------------------------------------------------------------

    def _maybe_reflexion_retry(
        self,
        *,
        task_result_msg: AgentMessage,
        verify_result_msg: AgentMessage,
        session_id: str,
    ) -> None:
        """Close the evaluator→optimizer loop for ONE bounded retry when the verifier flags a hit.

        The existing evaluator result drives exactly one iteration:
          - **Evaluator** = the :class:`VerifierAgent` verdict already computed in the main loop;
            ``fail``/``needs_more`` is the coarse failure signal the evaluator provides.
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
        """Run exactly ONE feedback-memory retry: distil a reflection, re-run the actor, re-verify
        once.

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
        # Offline ($0) this is the gated deterministic template; online it is one call through
        # the worker's own chat model/task. A blank reflection raises ReflexionError, never
        # swallowed.
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

        # Persist the distilled reflection as a correction note for later attempts:
        # A future run at a semantically-similar goal recalls it into its reflection slot.
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

        # Record the full feedback-memory trail on the blackboard (invariant attributed, terminal
        # 'done').
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
        """Recall the feedback-memory-buffer lessons for one task_assign (``None`` when none).

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
        except Exception as exc:  # noqa: BLE001 — persistence must not break the feedback-memory retry
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

    @property
    def session_budget(self) -> dict[str, Any]:
        """Machine-readable aggregate budget state for the product/UI and eval artifacts."""
        model = self._session_model_budget
        tool = self._session_tool_budget
        cost = self._session_cost_budget
        return {
            "model_calls_limit": model.limit if model is not None else None,
            "model_calls_used": model.used if model is not None else None,
            "model_calls_remaining": (
                max(0, model.limit - model.used) if model is not None else None
            ),
            "model_calls_by_kind": dict(model.by_kind) if model is not None else {},
            "tool_calls_limit": tool.limit if tool is not None else None,
            "tool_calls_used": tool.used if tool is not None else None,
            "tool_calls_remaining": (max(0, tool.limit - tool.used) if tool is not None else None),
            "tool_calls_by_name": dict(tool.by_tool) if tool is not None else {},
            "settled_cost_limit_usd": cost.limit_usd if cost is not None else None,
            "settled_cost_usd": cost.settled_cost_usd if cost is not None else 0.0,
            "cost_is_estimate": cost.cost_is_estimate if cost is not None else False,
            "cost_gate_exhausted": cost.is_exhausted if cost is not None else False,
            "cost_gate_semantics": "settled_cost_blocks_next_dispatch",
            "hard_dollar_cap": False,
            "concurrent_in_flight_cost_can_overshoot": True,
        }

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
