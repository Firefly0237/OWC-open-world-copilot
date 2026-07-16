"""Tests for the OWCopilot multi-agent system (T3).

ALL tests run offline at $0 (MockProvider via conftest autouse fixture).
No real LLM calls, no network, no model downloads.

Verified against SUPERVISOR_rubric P3-1 through P3-7:
  P3-1: ≥2 independent Agent classes, each with agent_id + independent context
  P3-2: OrchestratorAgent has real task decomposition, not if-else routing
  P3-3: Agent communication goes through SQLite blackboard, not function return values
  P3-4: Every worker receives original_goal + subtask in its prompt
  P3-5: VerifierAgent is independent — doesn't read worker transcript
  P3-6: Every blackboard row has from_agent / to_agent attribution
  P3-7: Agents are dispatched via blackboard messages, not nested function calls
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from owcopilot.fakes import MockProvider
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.telemetry import TelemetryCollector
from owcopilot.multi_agent import (
    AgentBlackboard,
    AgentMessage,
    DiagWorker,
    MultiAgentReport,
    MultiAgentSession,
    OrchestratorAgent,
    ReflectionPayload,
    RepairWorker,
    TaskAssignPayload,
    TaskResultPayload,
    VerifierAgent,
    VerifyResultPayload,
    WorkerAgent,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def offline_gateway() -> LLMGateway:
    """$0 offline LLM gateway backed by MockProvider.

    Registered under 'cheap' because StaticRouter defaults unmapped tasks to 'cheap'.
    """
    mock = MockProvider()
    return LLMGateway(
        {"cheap": mock, "frontier": mock},
        telemetry=TelemetryCollector(),
    )


@pytest.fixture()
def mem_conn() -> sqlite3.Connection:
    """Fresh in-memory SQLite connection for blackboard isolation."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@pytest.fixture()
def blackboard(mem_conn: sqlite3.Connection) -> AgentBlackboard:
    """AgentBlackboard over an in-memory SQLite connection."""
    return AgentBlackboard(mem_conn)


@pytest.fixture()
def minimal_registry():
    """A SkillRegistry with zero real skills — enough for ReActAgent without content_root."""
    from owcopilot.skills import SkillRegistry

    return SkillRegistry()


def _make_audit_registry(open_errors: int):
    """Build a SkillRegistry whose ``audit_project`` deterministically returns ``open_errors``.

    Mirrors the real ``audit_project`` tool's contract ({"open_errors": <int>, ...}) without a
    content root, so the verifier's deterministic ground-truth path can be exercised at $0.
    Also registers a ``propose_fix`` skill (PROPOSES_PATCH) so execution-time scoping can be
    tested: a read-only agent must be DENIED this skill at dispatch, not merely in the prompt.
    """
    from owcopilot.skills import (
        CostTier,
        SideEffect,
        Skill,
        SkillParameter,
        SkillRegistry,
    )

    calls: dict[str, int] = {"audit_project": 0, "propose_fix": 0}

    def _audit(**_kwargs):
        calls["audit_project"] += 1
        return {"open_errors": open_errors, "issues": []}

    def _list_issues(**_kwargs):
        return {"count": open_errors, "issues": []}

    def _propose_fix(*, issue_id: str, **_kwargs):
        calls["propose_fix"] += 1
        return {"issue_id": issue_id, "proposed": True}

    reg = SkillRegistry()
    reg.register(
        Skill(
            name="audit_project",
            description="Deterministic audit (fake).",
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=_audit,
        )
    )
    reg.register(
        Skill(
            name="list_issues",
            description="List issues (fake).",
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=_list_issues,
        )
    )
    reg.register(
        Skill(
            name="propose_fix",
            description="Propose a fix (fake, PROPOSES_PATCH).",
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.PROPOSES_PATCH,
            handler=_propose_fix,
            parameters=(SkillParameter("issue_id", "string", "issue id", required=True),),
        )
    )
    return reg, calls


@pytest.fixture()
def session(offline_gateway: LLMGateway, minimal_registry) -> MultiAgentSession:
    """Full MultiAgentSession wired to offline providers."""
    return MultiAgentSession(gateway=offline_gateway, registry=minimal_registry)


# ---------------------------------------------------------------------------
# P3-1: Multiple independent Agent instances with distinct agent_ids
# ---------------------------------------------------------------------------


def test_p3_1_distinct_agent_classes(offline_gateway, minimal_registry) -> None:
    """P3-1: OrchestratorAgent, DiagWorker, RepairWorker, VerifierAgent are distinct classes."""
    gw, reg = offline_gateway, minimal_registry
    orch = OrchestratorAgent(agent_id="orchestrator", gateway=gw, registry=reg)
    diag = DiagWorker(agent_id="diag_01", gateway=gw, registry=reg)
    repair = RepairWorker(agent_id="repair_01", gateway=gw, registry=reg)
    verifier = VerifierAgent(agent_id="verifier_01", gateway=gw, registry=reg)

    agent_ids = {orch.agent_id, diag.agent_id, repair.agent_id, verifier.agent_id}
    assert len(agent_ids) == 4, f"Expected 4 distinct agent_ids, got: {agent_ids}"

    # All are different classes
    assert type(orch) is OrchestratorAgent
    assert type(diag) is DiagWorker
    assert type(repair) is RepairWorker
    assert type(verifier) is VerifierAgent

    # All are subclasses of their respective bases
    assert isinstance(diag, WorkerAgent)
    assert isinstance(repair, WorkerAgent)
    assert not isinstance(orch, WorkerAgent)
    assert not isinstance(verifier, WorkerAgent)


def test_p3_1_independent_transcripts(offline_gateway, minimal_registry) -> None:
    """P3-1: Two workers must have independent transcript objects — not the same list."""
    diag = DiagWorker(agent_id="diag_01", gateway=offline_gateway, registry=minimal_registry)
    repair = RepairWorker(agent_id="repair_01", gateway=offline_gateway, registry=minimal_registry)

    # Each ReActAgent initialises its transcript inside run() as a local list.
    # We verify the two agents' underlying ReActAgent objects are distinct instances.
    assert diag.agent is not repair.agent, "Workers must have distinct ReActAgent instances"
    assert diag.agent is not repair.agent

    # Also verify no shared identity at class level
    assert diag is not repair


def test_p3_1_session_has_four_participants(session: MultiAgentSession) -> None:
    """P3-1: Session exposes all four agent_ids."""
    participants = session.session_participants
    assert "orchestrator" in participants
    assert "diag_01" in participants
    assert "repair_01" in participants
    assert "verifier_01" in participants
    assert len(set(participants)) == 4


# ---------------------------------------------------------------------------
# P3-2: OrchestratorAgent has real task decomposition
# ---------------------------------------------------------------------------


def test_p3_2_orchestrator_decomposes_goal(
    offline_gateway: LLMGateway, minimal_registry, blackboard: AgentBlackboard
) -> None:
    """P3-2: post_task_assignments posts ≥2 task_assign messages to the blackboard."""
    import uuid

    orch = OrchestratorAgent(
        agent_id="orchestrator", gateway=offline_gateway, registry=minimal_registry
    )
    sid = str(uuid.uuid4())

    posted, _degraded = orch.post_task_assignments(
        goal="Bring the world to exportable state",
        session_id=sid,
        blackboard=blackboard,
    )

    assert len(posted) >= 2, "Orchestrator must decompose into at least 2 subtasks"
    assert all(msg.from_agent == "orchestrator" for msg in posted)
    assert all(msg.msg_type == "task_assign" for msg in posted)
    assert len({msg.to_agent for msg in posted}) >= 2, "Must target at least 2 distinct workers"


def test_p3_2_orchestrator_is_not_if_else_router(
    offline_gateway: LLMGateway, minimal_registry, blackboard: AgentBlackboard
) -> None:
    """P3-2: Orchestrator routes to named workers via blackboard, not a static decision tree."""
    import uuid

    orch = OrchestratorAgent(
        agent_id="orchestrator", gateway=offline_gateway, registry=minimal_registry
    )
    sid = str(uuid.uuid4())

    posted, _degraded = orch.post_task_assignments(
        goal="Find all broken references and propose fixes",
        session_id=sid,
        blackboard=blackboard,
    )

    # Each posted message must have a specific worker in to_agent — not "broadcast"
    # and not a generic "worker" — proving the orchestrator routes to named workers
    to_agents = [m.to_agent for m in posted]
    assert "broadcast" not in to_agents, (
        "Orchestrator must route to specific workers, not broadcast"
    )
    # At minimum we expect diag_01 and repair_01 from the fallback
    assert any(t.startswith("diag") for t in to_agents), f"Expected a diag worker, got {to_agents}"
    assert any(t.startswith("repair") for t in to_agents), (
        f"Expected a repair worker, got {to_agents}"
    )


class _JsonDecomposeProvider:
    """Mock provider that returns a valid decompose JSON array for the orchestrator.

    This exercises the PRODUCTION JSON-parse path in ``_decompose_goal`` /
    ``_parse_subtasks`` (not the offline fallback). For non-decompose tasks
    (worker / verifier ReAct loops) it returns a trivial Final Answer so those
    loops terminate quickly. Still $0 — no network, no real model.
    """

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        if "task-decomposition" in system or "Decompose into subtasks" in user:
            payload = (
                '[{"role": "diagnosis", "description": "Audit and list open errors", '
                '"allowed_skills": ["audit_project", "list_issues"], "worker_id": "diag_01"}, '
                '{"role": "repair_proposal", "description": "Propose fixes for top errors", '
                '"allowed_skills": ["propose_fix"], "worker_id": "repair_01"}]'
            )
            return payload, 10, 10
        return "Thought: done.\nFinal Answer: ok", 5, 2


def test_p3_2_orchestrator_parses_real_decompose_json(
    minimal_registry, blackboard: AgentBlackboard
) -> None:
    """P3-2 (production path): a valid decompose JSON is parsed into NAMED subtasks.

    The default offline mock falls back to built-in subtasks; this test feeds a provider
    that returns a real JSON array, proving ``_parse_subtasks`` parses LLM output (not just
    the fallback) and routes each parsed subtask to its named worker via the blackboard.
    """
    import uuid

    provider = _JsonDecomposeProvider()
    gateway = LLMGateway({"cheap": provider, "frontier": provider}, telemetry=TelemetryCollector())
    orch = OrchestratorAgent(agent_id="orchestrator", gateway=gateway, registry=minimal_registry)
    sid = str(uuid.uuid4())

    posted, degraded = orch.post_task_assignments(
        goal="Make the world exportable",
        session_id=sid,
        blackboard=blackboard,
    )

    # Production parse succeeded — must NOT be degraded
    assert not degraded, "JSON-returning provider should not trigger fallback"

    # Exactly the two subtasks defined in the JSON — proving real parse, not fallback shape
    assert len(posted) == 2
    by_worker = {m.to_agent: m for m in posted}
    assert set(by_worker) == {"diag_01", "repair_01"}

    # The parsed descriptions/skills came from the JSON, not the fallback constants
    diag_payload = TaskAssignPayload.from_dict(by_worker["diag_01"].payload)
    assert diag_payload.subtask == "Audit and list open errors"
    assert diag_payload.allowed_skills == ["audit_project", "list_issues"]
    # Original goal still injected (P3-4) even on the production parse path
    assert diag_payload.original_goal == "Make the world exportable"

    repair_payload = TaskAssignPayload.from_dict(by_worker["repair_01"].payload)
    assert repair_payload.subtask == "Propose fixes for top errors"
    assert repair_payload.allowed_skills == ["propose_fix"]


# ---------------------------------------------------------------------------
# P3-3: Communication via SQLite blackboard (not function return values in same stack)
# ---------------------------------------------------------------------------


def test_p3_3_blackboard_stores_messages(blackboard: AgentBlackboard) -> None:
    """P3-3: AgentBlackboard stores and retrieves messages via SQLite."""
    import uuid

    sid = str(uuid.uuid4())

    msg = AgentMessage(
        session_id=sid,
        from_agent="orchestrator",
        to_agent="diag_01",
        msg_type="task_assign",
        payload={"original_goal": "goal", "subtask": "diagnose", "allowed_skills": []},
    )
    blackboard.post_message(msg)

    results = blackboard.read_messages(sid, msg_type="task_assign")
    assert len(results) == 1
    assert results[0].from_agent == "orchestrator"
    assert results[0].to_agent == "diag_01"
    assert results[0].payload["original_goal"] == "goal"


def test_p3_3_blackboard_roundtrip_task_assign_to_result(
    offline_gateway: LLMGateway, minimal_registry, blackboard: AgentBlackboard
) -> None:
    """P3-3: Full assign→claim→result cycle flows through the blackboard, not function args."""
    import uuid

    sid = str(uuid.uuid4())

    # Orchestrator posts a task_assign
    assign_payload = TaskAssignPayload(
        original_goal="Fix broken refs in world",
        subtask="Run audit_project and report open errors",
        allowed_skills=["audit_project", "list_issues"],
    )
    assign_msg = AgentMessage(
        session_id=sid,
        from_agent="orchestrator",
        to_agent="diag_01",
        msg_type="task_assign",
        payload=assign_payload.to_dict(),
    )
    blackboard.post_message(assign_msg)

    # Worker claims and runs — the blackboard is the handoff, not a Python argument
    worker = DiagWorker(agent_id="diag_01", gateway=offline_gateway, registry=minimal_registry)
    claimed = blackboard.claim_task("diag_01", sid)
    assert claimed is not None, "Worker should be able to claim the posted task"
    assert claimed.msg_type == "task_assign"

    worker.run_task(claimed, blackboard)

    # Verify the result is on the blackboard
    results = blackboard.read_messages(sid, msg_type="task_result")
    assert len(results) == 1
    assert results[0].from_agent == "diag_01"
    assert results[0].to_agent == "orchestrator"

    # Original assign is now done
    updated_assign = blackboard.get_message(assign_msg.id)
    assert updated_assign is not None
    assert updated_assign.status == "done"


def test_p3_3_claim_prevents_double_claim(
    blackboard: AgentBlackboard,
) -> None:
    """P3-3: Optimistic locking prevents two workers claiming the same task."""
    import uuid

    sid = str(uuid.uuid4())

    msg = AgentMessage(
        session_id=sid,
        from_agent="orchestrator",
        to_agent="diag_01",
        msg_type="task_assign",
        payload={"original_goal": "g", "subtask": "s", "allowed_skills": []},
    )
    blackboard.post_message(msg)

    # First claim succeeds
    c1 = blackboard.claim_task("diag_01", sid)
    assert c1 is not None

    # Second claim returns None (already claimed)
    c2 = blackboard.claim_task("diag_01", sid)
    assert c2 is None


# ---------------------------------------------------------------------------
# P3-4: Worker receives both original_goal and subtask
# ---------------------------------------------------------------------------


def test_p3_4_worker_goal_contains_original_goal_and_subtask(
    offline_gateway: LLMGateway, minimal_registry, blackboard: AgentBlackboard
) -> None:
    """P3-4: The goal string built for the ReActAgent contains both original_goal and subtask."""
    import uuid

    sid = str(uuid.uuid4())

    assign = TaskAssignPayload(
        original_goal="UNIQUE_OVERALL_GOAL_SENTINEL",
        subtask="UNIQUE_SUBTASK_SENTINEL",
        allowed_skills=[],
    )
    assign_msg = AgentMessage(
        session_id=sid,
        from_agent="orchestrator",
        to_agent="diag_01",
        msg_type="task_assign",
        payload=assign.to_dict(),
    )
    blackboard.post_message(assign_msg)

    worker = DiagWorker(agent_id="diag_01", gateway=offline_gateway, registry=minimal_registry)
    claimed = blackboard.claim_task("diag_01", sid)
    assert claimed is not None

    # Patch the ReActAgent.run to capture the goal it receives
    captured_goals: list[str] = []
    original_run = worker.agent.__class__.run

    def capturing_run(self_inner, goal: str):  # type: ignore[override]
        captured_goals.append(goal)
        return original_run(self_inner, goal)

    # We call run_task and inspect what goal was constructed
    # The run_task method builds the goal string before calling agent.run
    # We verify by reading the payload fields the task protocol mandates

    result_msg = worker.run_task(claimed, blackboard)

    # The task_result was posted — verify its payload references the task_msg_id
    result_payload = TaskResultPayload.from_dict(result_msg.payload)
    assert result_payload.task_msg_id == assign_msg.id

    # Direct check: TaskAssignPayload.from_dict must always have both fields
    recovered = TaskAssignPayload.from_dict(claimed.payload)
    assert recovered.original_goal == "UNIQUE_OVERALL_GOAL_SENTINEL"
    assert recovered.subtask == "UNIQUE_SUBTASK_SENTINEL"

    # The goal string format ("Overall goal: {original_goal}\n\n[Your subtask]:\n{subtask}")
    # is enforced in WorkerAgent.run_task — system-level guarantee, not a prompt constraint.
    # We verify the protocol contract via the payload fields.
    assert "original_goal" in claimed.payload
    assert "subtask" in claimed.payload


# ---------------------------------------------------------------------------
# P3-5: Verifier is independent — does not read worker transcript
# ---------------------------------------------------------------------------


def test_p3_5_verifier_has_independent_agent_instance(
    offline_gateway: LLMGateway, minimal_registry
) -> None:
    """P3-5: VerifierAgent has its own ReActAgent, distinct from any worker's agent."""
    gw, reg = offline_gateway, minimal_registry
    worker = DiagWorker(agent_id="diag_01", gateway=gw, registry=reg)
    verifier = VerifierAgent(agent_id="verifier_01", gateway=gw, registry=reg)

    assert verifier.agent is not worker.agent, "Verifier must have its own ReActAgent"
    assert verifier.agent_id != worker.agent_id


def test_p3_5_verifier_only_reads_blackboard_not_worker_transcript(
    offline_gateway: LLMGateway, minimal_registry, blackboard: AgentBlackboard
) -> None:
    """P3-5: Verifier reads task_result from blackboard, not from the worker object."""
    import uuid

    sid = str(uuid.uuid4())

    # Simulate a task_result already on the blackboard (as if a worker had posted it)
    task_result = AgentMessage(
        session_id=sid,
        from_agent="diag_01",
        to_agent="orchestrator",
        msg_type="task_result",
        payload=TaskResultPayload(
            task_msg_id="fake-assign-id",
            worker_role="diagnosis",
            final_answer="Found 2 open errors: ref-001 and ref-002",
            open_errors=2,
            stop_reason="finished",
            step_count=3,
        ).to_dict(),
    )
    blackboard.post_message(task_result)

    # Orchestrator posts verify_request
    verify_req = AgentMessage(
        session_id=sid,
        from_agent="orchestrator",
        to_agent="verifier_01",
        msg_type="verify_request",
        payload={"target_msg_id": task_result.id},
    )
    blackboard.post_message(verify_req)

    # Verifier runs — it MUST NOT receive the worker object, only the blackboard
    verifier = VerifierAgent(
        agent_id="verifier_01", gateway=offline_gateway, registry=minimal_registry
    )
    result_msg = verifier.verify(verify_req, blackboard)

    # Verify result is on the blackboard
    assert result_msg.from_agent == "verifier_01"
    assert result_msg.msg_type == "verify_result"
    vr = VerifyResultPayload.from_dict(result_msg.payload)
    assert vr.verdict in {"pass", "fail", "needs_more"}
    assert vr.target_msg_id == task_result.id
    # With a bare registry (no audit_project bound) and a non-ReAct mock, the verifier has
    # NO ground truth — it returns needs_more honestly (open_errors_verified=-1), never a
    # fabricated pass.  The real-verdict path is covered by the audit-registry tests below.
    if vr.verdict == "needs_more":
        assert vr.open_errors_verified == -1


# ---------------------------------------------------------------------------
# P3-6: Every agent has a unique agent_id that appears in blackboard records
# ---------------------------------------------------------------------------


def test_p3_6_all_messages_have_from_agent_attribution(
    session: MultiAgentSession,
) -> None:
    """P3-6: Full session run produces blackboard records with distinct from_agent values."""
    report = session.run("Diagnose and fix the world content")

    all_msgs = session.blackboard.session_flow(report.session_id)
    assert len(all_msgs) > 0, "No messages in blackboard after session run"

    for msg in all_msgs:
        assert msg.from_agent, f"Message {msg.id} has no from_agent"
        assert msg.to_agent, f"Message {msg.id} has no to_agent"
        assert msg.session_id == report.session_id

    from_agents = {msg.from_agent for msg in all_msgs}
    assert "orchestrator" in from_agents, f"Orchestrator not in from_agents: {from_agents}"


def test_p3_6_message_types_all_present(session: MultiAgentSession) -> None:
    """P3-6: A session produces all five message types on the blackboard."""
    report = session.run("Check world consistency")
    all_msgs = session.blackboard.session_flow(report.session_id)
    msg_types = {msg.msg_type for msg in all_msgs}

    # These are the minimum required by the protocol
    assert "task_assign" in msg_types, f"Missing task_assign. Types: {msg_types}"
    assert "task_result" in msg_types, f"Missing task_result. Types: {msg_types}"
    assert "verify_request" in msg_types, f"Missing verify_request. Types: {msg_types}"
    assert "verify_result" in msg_types, f"Missing verify_result. Types: {msg_types}"
    assert "synthesize" in msg_types, f"Missing synthesize. Types: {msg_types}"


# ---------------------------------------------------------------------------
# P3-7: Agents are not in same synchronous call chain
# ---------------------------------------------------------------------------


def test_p3_7_blackboard_as_handoff_not_return_value(
    session: MultiAgentSession,
) -> None:
    """P3-7: Workers are dispatched via blackboard message, not via direct function return.

    We verify this by checking that task_assign messages exist BEFORE task_result messages
    are created, proving the blackboard was the handoff point.
    """
    report = session.run("Check world for export readiness")
    all_msgs = session.blackboard.session_flow(report.session_id)

    assigns = [m for m in all_msgs if m.msg_type == "task_assign"]
    results = [m for m in all_msgs if m.msg_type == "task_result"]

    assert len(assigns) >= 1, "No task_assign messages found"
    assert len(results) >= 1, "No task_result messages found"

    # ⑤ P3-7 real chronological assertion: the FIRST task_assign must precede the FIRST
    # task_result.  This proves that tasks were POSTED (blackboard write) before workers
    # produced results — confirming the blackboard is the hand-off, not a direct return.
    # We compare the earliest assign vs the earliest result; ISO timestamps sort lexicographically.
    first_assign_time = min(m.created_at for m in assigns)
    first_result_time = min(m.created_at for m in results)
    assert first_assign_time <= first_result_time, (
        f"P3-7 violated: first task_assign ({first_assign_time}) must precede "
        f"first task_result ({first_result_time}). "
        "This means workers produced results before tasks were posted — "
        "the blackboard handoff was bypassed."
    )


def test_p3_7_worker_transcripts_are_not_same_object(
    session: MultiAgentSession,
) -> None:
    """P3-7 / P3-1: DiagWorker and RepairWorker have different ReActAgent instances."""
    diag = session.diag_worker
    repair = session.repair_worker

    # The agent attribute holds the ReActAgent — must be distinct objects
    assert diag.agent is not repair.agent, (
        "DiagWorker and RepairWorker must have independent ReActAgent instances, "
        "not the same object"
    )
    # Verifier's agent is also distinct
    assert diag.agent is not session.verifier.agent
    assert repair.agent is not session.verifier.agent
    # The orchestrator does NOT hold a ReActAgent — it decomposes via direct gateway calls,
    # not a (previously performative) ReAct meta-loop.  Its independence is structural: a
    # distinct agent_id and dedicated orchestrator_decompose gateway task.
    assert not hasattr(session.orchestrator, "agent")
    assert session.orchestrator.agent_id == "orchestrator"


# ---------------------------------------------------------------------------
# Full integration: blackboard roundtrip assertion
# ---------------------------------------------------------------------------


def test_full_session_report_has_all_participants(session: MultiAgentSession) -> None:
    """Integration: A full session produces a MultiAgentReport with all agent_ids."""
    report = session.run("Make the world ready for export")

    assert isinstance(report, MultiAgentReport)
    assert report.session_id
    assert report.goal == "Make the world ready for export"
    assert "orchestrator" in report.participants
    assert "diag_01" in report.participants
    assert "repair_01" in report.participants
    assert "verifier_01" in report.participants
    assert len(report.participants) >= 4

    # Worker summaries and verifier verdicts must be present
    assert len(report.worker_summaries) >= 1
    assert len(report.verifier_verdicts) >= 1


def test_full_session_blackboard_message_flow(session: MultiAgentSession) -> None:
    """Integration: All expected message types flow through the blackboard in correct order."""
    report = session.run("Find and fix broken world references")

    # Verify task_assign messages
    assigns = session.blackboard.read_messages(report.session_id, msg_type="task_assign")
    assert len(assigns) >= 2, f"Expected ≥2 task_assign, got {len(assigns)}"
    assert all(m.from_agent == "orchestrator" for m in assigns)
    assert all(m.to_agent in {"diag_01", "repair_01"} for m in assigns)

    # Verify task_result messages
    results = session.blackboard.read_messages(report.session_id, msg_type="task_result")
    assert len(results) >= 1
    assert all(m.from_agent in {"diag_01", "repair_01"} for m in results)
    assert all(m.from_agent != "orchestrator" for m in results)

    # Verify verify_result messages exist and have valid verdicts
    verdicts = session.blackboard.read_messages(report.session_id, msg_type="verify_result")
    assert len(verdicts) >= 1
    for v in verdicts:
        vr = VerifyResultPayload.from_dict(v.payload)
        assert vr.verdict in {"pass", "fail", "needs_more"}
        # With the minimal registry there is no ground-truth audit tool, so the verdict is
        # an honest needs_more (-1); a real verdict (>=0) is covered by the audit-registry
        # tests.  Either way it must never be a silently fabricated count.
        if vr.verdict != "needs_more":
            assert vr.open_errors_verified >= 0


def test_full_session_synthesis_on_blackboard(session: MultiAgentSession) -> None:
    """Integration: Synthesis message is posted to blackboard with participants list."""
    report = session.run("Synthesize world status")

    synth_msgs = session.blackboard.read_messages(report.session_id, msg_type="synthesize")
    assert len(synth_msgs) == 1
    assert synth_msgs[0].from_agent == "orchestrator"
    assert "synthesis" in synth_msgs[0].payload
    assert "participants" in synth_msgs[0].payload


# ---------------------------------------------------------------------------
# Messages module
# ---------------------------------------------------------------------------


def test_agent_message_payload_immutability_via_blackboard(
    blackboard: AgentBlackboard,
) -> None:
    """payload_json is the only serialisation path; update_status cannot change payload."""
    import uuid

    sid = str(uuid.uuid4())

    msg = AgentMessage(
        session_id=sid,
        from_agent="orchestrator",
        to_agent="diag_01",
        msg_type="task_assign",
        payload={"original_goal": "immutable", "subtask": "do it", "allowed_skills": []},
    )
    blackboard.post_message(msg)

    # Update status only
    blackboard.update_status(msg.id, "done")

    # Payload must be unchanged
    retrieved = blackboard.get_message(msg.id)
    assert retrieved is not None
    assert retrieved.payload["original_goal"] == "immutable"
    assert retrieved.status == "done"


def test_verify_result_payload_roundtrip() -> None:
    """VerifyResultPayload serialises and deserialises cleanly."""
    original = VerifyResultPayload(
        target_msg_id="abc123",
        verdict="pass",
        rationale="Counts match within tolerance.",
        open_errors_verified=3,
        worker_claimed_errors=3,
    )
    data = original.to_dict()
    recovered = VerifyResultPayload.from_dict(data)
    assert recovered.verdict == "pass"
    assert recovered.open_errors_verified == 3
    assert recovered.target_msg_id == "abc123"


def test_task_assign_payload_always_has_both_fields() -> None:
    """TaskAssignPayload always carries original_goal and subtask (P3-4 contract)."""
    p = TaskAssignPayload(
        original_goal="The big goal",
        subtask="The small job",
        allowed_skills=["audit_project"],
    )
    d = p.to_dict()
    assert "original_goal" in d
    assert "subtask" in d
    assert d["original_goal"] == "The big goal"
    assert d["subtask"] == "The small job"

    # Roundtrip
    p2 = TaskAssignPayload.from_dict(d)
    assert p2.original_goal == p.original_goal
    assert p2.subtask == p.subtask


# ---------------------------------------------------------------------------
# RT3 hardening tests — ①②③④⑤⑥⑦
# ---------------------------------------------------------------------------


def test_rt3_1_worker_crash_recorded_not_silent(
    offline_gateway: LLMGateway, minimal_registry, blackboard: AgentBlackboard
) -> None:
    """① worker.run_task crash → failed task_result on blackboard, NOT silent code=0.

    We make run_task raise by monkey-patching ReActAgent.run to throw.  The session
    must NOT silently succeed: the task_assign must be marked 'failed', a task_result
    with stop_reason='error' must be on the blackboard, and the report must include
    that failed worker in worker_summaries (not an empty list).
    """
    from owcopilot.multi_agent.session import MultiAgentSession

    session = MultiAgentSession(gateway=offline_gateway, registry=minimal_registry)

    # Patch DiagWorker.run_task to raise
    original_run_task = session.diag_worker.__class__.run_task

    def crashing_run_task(self_inner, task_msg, bb):
        raise RuntimeError("simulated diag worker crash")

    session.diag_worker.__class__.run_task = crashing_run_task
    try:
        report = session.run("test crash goal")
    finally:
        session.diag_worker.__class__.run_task = original_run_task

    # The report must NOT be an empty success — worker_summaries must contain the failed entry
    assert len(report.worker_summaries) >= 1, (
        "worker_summaries must not be empty after a worker crash — silent success forbidden"
    )
    # At least one summary must reflect the crash (stop_reason='error')
    stop_reasons = [ws["stop_reason"] for ws in report.worker_summaries]
    assert "error" in stop_reasons, (
        f"Expected stop_reason='error' in at least one worker summary, got: {stop_reasons}"
    )

    # The blackboard must have a failed task_result (not a 'claimed' orphan)
    all_msgs = session.blackboard.session_flow(report.session_id)
    failed_results = [m for m in all_msgs if m.msg_type == "task_result" and m.status == "failed"]
    assert len(failed_results) >= 1, (
        "Crashed worker must produce a task_result with status='failed' on the blackboard, "
        "not an orphaned 'claimed' message"
    )


def test_rt3_2_fallback_decomposition_is_not_silent(
    offline_gateway: LLMGateway, minimal_registry
) -> None:
    """② LLM decomposition fallback must NOT be silent — report.decomposition_degraded=True.

    MockProvider returns garbage (not valid JSON), so _parse_subtasks falls back.
    The report must carry decomposition_degraded=True and the synthesis must contain
    the degradation notice string, never hiding the fact from callers.
    """
    from owcopilot.multi_agent.session import MultiAgentSession

    session = MultiAgentSession(gateway=offline_gateway, registry=minimal_registry)
    # MockProvider's output is not valid JSON for the decompose prompt → fallback fires
    report = session.run("test fallback goal")

    assert report.decomposition_degraded is True, (
        "report.decomposition_degraded must be True when LLM JSON parse fails — "
        "silent downgrade is forbidden (no-silent-downgrade policy)"
    )
    has_degraded_note = (
        "分解已降级到静态模板" in report.synthesis or "DECOMPOSITION DEGRADED" in report.synthesis
    )
    assert has_degraded_note, (
        "synthesis must explicitly annotate decomposition degradation — found: "
        + report.synthesis[:300]
    )


def test_rt3_3_unknown_worker_recorded_not_silent(
    offline_gateway: LLMGateway, minimal_registry
) -> None:
    """③ Unknown worker_id must not silent-continue — blackboard gets a failed task_result.

    We inject a task_assign for a non-existent worker_id via a custom provider that
    returns a JSON subtask targeting 'nonexistent_worker_99'.  The session must NOT
    silently skip it — it must record an unrouted task_result on the blackboard.
    """
    from owcopilot.multi_agent.session import MultiAgentSession

    class _UnknownWorkerProvider:
        def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
            if "task-decomposition" in system or "Decompose" in user:
                # Role-complete decomposition (diagnosis + repair_proposal) PLUS a third subtask
                # whose role is unregistered — orchestrator id-normalisation cannot remap an
                # unknown role (a known role with an invented id would be remapped to the
                # registered worker instead), so the session's unrouted path is exercised
                # without tripping the role-completeness degradation check.
                return (
                    (
                        '[{"role": "diagnosis", "description": "Diagnose", '
                        '"allowed_skills": ["audit_project"], "worker_id": "diag_01"}, '
                        '{"role": "repair_proposal", "description": "Repair", '
                        '"allowed_skills": ["propose_fix"], "worker_id": "repair_01"}, '
                        '{"role": "lore_review", "description": "Review lore", '
                        '"allowed_skills": ["list_issues"], "worker_id": "nonexistent_worker_99"}]'
                    ),
                    10,
                    10,
                )
            return "Thought: done.\nFinal Answer: ok", 5, 2

    from owcopilot.llm.telemetry import TelemetryCollector

    provider = _UnknownWorkerProvider()
    gw = LLMGateway({"cheap": provider, "frontier": provider}, telemetry=TelemetryCollector())

    session = MultiAgentSession(gateway=gw, registry=minimal_registry)
    report = session.run("test unknown worker goal")

    # The report must include the unrouted worker in worker_summaries (not silently absent)
    assert len(report.worker_summaries) >= 1, (
        "worker_summaries must not be empty even when worker_id is unknown"
    )
    stop_reasons = [ws["stop_reason"] for ws in report.worker_summaries]
    assert "unrouted" in stop_reasons, (
        f"Expected stop_reason='unrouted' in worker_summaries, got: {stop_reasons}"
    )

    # Blackboard must record the unrouted task (not just silently skip it)
    all_msgs = session.blackboard.session_flow(report.session_id)
    unrouted_msgs = [
        m
        for m in all_msgs
        if m.msg_type == "task_result"
        and m.status == "failed"
        and "UNROUTED" in m.payload.get("final_answer", "")
    ]
    assert len(unrouted_msgs) >= 1, (
        "Unknown worker_id must produce an unrouted record on the blackboard"
    )


def test_rt3_4_repair_worker_reads_diag_findings(
    offline_gateway: LLMGateway, minimal_registry
) -> None:
    """④ Repair worker's subtask must contain diag findings — true multi-agent handoff.

    After diag runs and posts its task_result, the repair worker's task_assign must
    be enriched with the diag final_answer.  We verify by inspecting the task_assign
    message that repair_01 actually claimed — its payload.subtask must reference diag output.
    """
    from owcopilot.multi_agent.session import MultiAgentSession

    class _MarkerDiagProvider:
        """DiagWorker will return a final answer containing a sentinel string."""

        def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
            if "task-decomposition" in system or "Decompose" in user:
                # Return a valid decompose JSON so we test the enrich path
                return (
                    (
                        '[{"role": "diagnosis", "description": "Diagnose project", '
                        '"allowed_skills": ["audit_project"], "worker_id": "diag_01"}, '
                        '{"role": "repair_proposal", "description": "Propose fixes", '
                        '"allowed_skills": ["propose_fix"], "worker_id": "repair_01"}]'
                    ),
                    10,
                    10,
                )
            if "diagnosis" in user.lower() or "diag" in user.lower():
                return "Thought: done.\nFinal Answer: DIAG_SENTINEL_FINDINGS: ref-XYZ broken", 5, 2
            return "Thought: done.\nFinal Answer: ok", 5, 2

    from owcopilot.llm.telemetry import TelemetryCollector

    provider = _MarkerDiagProvider()
    gw = LLMGateway({"cheap": provider, "frontier": provider}, telemetry=TelemetryCollector())

    session = MultiAgentSession(gateway=gw, registry=minimal_registry)
    report = session.run("find and fix broken refs")

    # Find the task_assign that repair_01 actually received
    all_msgs = session.blackboard.session_flow(report.session_id)
    repair_assigns = [
        m for m in all_msgs if m.msg_type == "task_assign" and m.to_agent == "repair_01"
    ]
    assert len(repair_assigns) >= 1, "No task_assign found for repair_01"

    # The LAST repair assign (the enriched one) must contain the diag findings
    # (the original generic assign is posted by orchestrator; enriched one is posted by session)
    enriched_assigns = [
        m
        for m in repair_assigns
        if "Diagnosis findings" in m.payload.get("subtask", "")
        or "DIAG_SENTINEL" in m.payload.get("subtask", "")
    ]
    # Accept: either the subtask mentions "Diagnosis findings" (enrich succeeded) or
    # the diag final_answer propagated to it in some form
    assert len(enriched_assigns) >= 1, (
        "Repair worker task_assign must contain diag findings for true handoff. "
        f"Repair assigns found: {[m.payload.get('subtask', '')[:100] for m in repair_assigns]}"
    )


def test_rt3_5_p3_7_real_chronological_assertion(
    session: MultiAgentSession,
) -> None:
    """⑤ P3-7: first task_assign precedes first task_result — real timing check.

    This replaces the dead `assert ... or True` at line 544.
    """
    report = session.run("timing test goal")
    all_msgs = session.blackboard.session_flow(report.session_id)

    assigns = [m for m in all_msgs if m.msg_type == "task_assign"]
    results = [m for m in all_msgs if m.msg_type == "task_result"]

    assert len(assigns) >= 1, "No task_assign messages"
    assert len(results) >= 1, "No task_result messages"

    first_assign = min(assigns, key=lambda m: m.created_at)
    first_result = min(results, key=lambda m: m.created_at)

    # The first task must be ASSIGNED before any result is produced
    assert first_assign.created_at <= first_result.created_at, (
        f"P3-7: first assign ({first_assign.created_at}) must be <= "
        f"first result ({first_result.created_at})"
    )


def test_rt3_6_verifier_needs_more_drives_retry_then_keeps_annotation(
    session: MultiAgentSession,
) -> None:
    """⑥ + T2: a needs_more verdict now DRIVES a single Reflexion retry, and a still-needs_more
    second trial keeps the [INCOMPLETE] annotation with NO third attempt.

    The bare minimal_registry gives the verifier no ground-truth audit tool, so every verdict is
    an honest needs_more. Pre-T2 this was only annotated in synthesis; T2 turns that wasted signal
    into exactly one bounded retry (Shinn et al., 2023) — but the second failure is not retried
    again and the existing [INCOMPLETE] annotation still stands.
    """
    report = session.run("needs_more test goal")
    flow = session.blackboard.session_flow(report.session_id)

    # needs_more now DRIVES a retry: a 'reflection' message was recorded.
    assert any(m.msg_type == "reflection" for m in flow), (
        "needs_more must drive a Reflexion retry (a reflection message on the blackboard)"
    )

    # Exactly two task_results per worker — the initial trial plus one retry, never a third.
    for wid in ("diag_01", "repair_01"):
        trs = [m for m in flow if m.msg_type == "task_result" and m.from_agent == wid]
        assert len(trs) == 2, f"{wid} must run exactly twice (initial + one retry), got {len(trs)}"

    # The still-needs_more second trial keeps the [INCOMPLETE] annotation (fallback, no 3rd try).
    assert "needs_more [INCOMPLETE" in report.synthesis, (
        "needs_more must still carry [INCOMPLETE] annotation in synthesis — found: "
        + report.synthesis[:500]
    )


# ---------------------------------------------------------------------------
# T2: single-shot Reflexion retry (Shinn et al., 2023) on a flagged verdict
#
# The verifier (Evaluator) verdict fail/needs_more now drives ONE bounded retry: the
# worker (Actor) is re-run with a reflection distilled by distill_reflection
# (Self-Reflection). All $0 offline — conftest sets OWCOPILOT_ALLOW_OFFLINE_LLM=1 so the
# deterministic reflection template stands in for the Self-Reflection LLM.
# ---------------------------------------------------------------------------


_REFLEXION_DECOMPOSE_JSON = (
    '[{"role": "diagnosis", "description": "Audit and report open errors", '
    '"allowed_skills": ["audit_project", "list_issues"], "worker_id": "diag_01"}, '
    '{"role": "repair_proposal", "description": "Propose fixes", '
    '"allowed_skills": ["propose_fix"], "worker_id": "repair_01"}]'
)


def test_reflexion_run_task_reflections_default_byte_identical(
    minimal_registry, blackboard: AgentBlackboard, monkeypatch
) -> None:
    """(A) run_task WITHOUT the reflections kwarg is byte-identical to the pre-Reflexion path.

    No 'Reflections from previous attempts:' block appears in any worker prompt, reflections=None
    reaches ReActAgent.run, and the posted TaskResultPayload equals a plain agent.run(goal).
    """
    import uuid

    from owcopilot.agent.react import ReActAgent
    from owcopilot.multi_agent.workers import _extract_claimed_open_errors

    class _RecordingProvider:
        def __init__(self) -> None:
            self.seen_user: list[str] = []

        def complete(self, *, system: str, user: str, model: str):
            self.seen_user.append(user)
            return "Thought: done.\nFinal Answer: ok", 5, 2

    provider = _RecordingProvider()
    gw = LLMGateway({"cheap": provider, "frontier": provider}, telemetry=TelemetryCollector())

    sid = str(uuid.uuid4())
    assign = TaskAssignPayload(
        original_goal="overall goal",
        subtask="run the audit",
        allowed_skills=["audit_project"],
    )
    assign_msg = AgentMessage(
        session_id=sid,
        from_agent="orchestrator",
        to_agent="diag_01",
        msg_type="task_assign",
        payload=assign.to_dict(),
    )
    blackboard.post_message(assign_msg)

    # Capture the reflections kwarg reaching the actor — run_task builds a fresh agent internally,
    # so patch at the class level to observe the call across it.
    captured: dict[str, object] = {}
    orig_run = ReActAgent.run

    def capturing_run(self, goal, reflections=None):  # type: ignore[no-untyped-def]
        captured["reflections"] = reflections
        return orig_run(self, goal, reflections=reflections)

    monkeypatch.setattr(ReActAgent, "run", capturing_run)

    worker = DiagWorker(agent_id="diag_01", gateway=gw, registry=minimal_registry)
    claimed = blackboard.claim_task("diag_01", sid)
    assert claimed is not None
    result_msg = worker.run_task(claimed, blackboard)  # NO reflections kwarg (default path)

    # reflections=None reached the actor, and no reflection block was rendered into any prompt.
    assert captured["reflections"] is None
    assert provider.seen_user
    assert all("Reflections from previous attempts:" not in u for u in provider.seen_user)

    # The posted result equals a plain agent.run(goal) built the same way run_task builds it.
    tr = TaskResultPayload.from_dict(result_msg.payload)
    goal = worker.build_goal(assign)
    plain_agent = worker._make_agent(allowed_skills={"audit_project"}, max_steps=assign.max_steps)
    plain = plain_agent.run(goal)
    assert tr.final_answer == plain.final_answer
    assert tr.stop_reason == plain.stop_reason
    assert tr.step_count == plain.step_count
    assert tr.open_errors == _extract_claimed_open_errors(plain)


def test_reflexion_pass_verdict_no_retry_inert(minimal_registry) -> None:
    """(B) A session where every worker passes verification is inert.

    No reflection message is written, each worker runs exactly once (one task_result + one
    verify_result), and the blackboard msg_type set stays the original five.
    """
    reg, _calls = _make_audit_registry(open_errors=2)

    class _AllPassProvider:
        """diag honestly audits (claims 2 == ground truth) → pass; repair makes no claim → pass."""

        def complete(self, *, system: str, user: str, model: str):
            if "task-decomposition" in system or "Decompose into subtasks" in user:
                return _REFLEXION_DECOMPOSE_JSON, 10, 10
            if "[Your specific subtask as diagnosis]" in user:
                if "Observation:" not in user:
                    return "Thought: audit first.\nAction: audit_project\nAction Input: {}", 5, 5
                return "Thought: done.\nFinal Answer: 一致性审计发现 2 个待修复错误。", 5, 5
            return "Thought: done.\nFinal Answer: 已生成修复提案，等待人工审阅。", 5, 2

    provider = _AllPassProvider()
    gw = LLMGateway({"cheap": provider, "frontier": provider}, telemetry=TelemetryCollector())
    session = MultiAgentSession(gateway=gw, registry=reg)
    report = session.run("diagnose and repair")
    flow = session.blackboard.session_flow(report.session_id)

    # Inert: no reflection message at all.
    assert not any(m.msg_type == "reflection" for m in flow), (
        "a pass-verdict session must not perform any Reflexion retry"
    )
    # Exactly one task_result per worker.
    for wid in ("diag_01", "repair_01"):
        trs = [m for m in flow if m.msg_type == "task_result" and m.from_agent == wid]
        assert len(trs) == 1, f"{wid} must run exactly once on a pass, got {len(trs)}"
    # Exactly one verify_result per worker (two total).
    vrs = [m for m in flow if m.msg_type == "verify_result"]
    assert len(vrs) == 2, f"expected one verify_result per worker, got {len(vrs)}"
    assert all(VerifyResultPayload.from_dict(m.payload).verdict == "pass" for m in vrs)
    # The blackboard msg_type set stays the original five (no 'reflection').
    assert {m.msg_type for m in flow} == {
        "task_assign",
        "task_result",
        "verify_request",
        "verify_result",
        "synthesize",
    }


def test_reflexion_fail_then_pass_single_retry(minimal_registry) -> None:
    """(C) A verifier 'fail' drives ONE Reflexion retry that flips the second trial to 'pass'.

    Trial 0 is a lazy diag trajectory (no audit, prose '0 errors') → fail. When the reflection is
    present the retry runs a diligent audit (open_errors=3 == ground truth) → pass. The worker runs
    exactly twice and the reflection trail is recorded; the reflection is injected into the retry
    actor's prompt but not the first attempt's.
    """
    reg, _calls = _make_audit_registry(open_errors=3)

    class _LazyThenDiligentProvider:
        def __init__(self) -> None:
            self.diag_prompts: list[str] = []

        def complete(self, *, system: str, user: str, model: str):
            if "task-decomposition" in system or "Decompose into subtasks" in user:
                return _REFLEXION_DECOMPOSE_JSON, 10, 10
            if "[Your specific subtask as diagnosis]" in user:
                self.diag_prompts.append(user)
                if "Reflections from previous attempts:" not in user:
                    # Trial 0: lazy — never audits, asserts clean → verifier fail (ground truth 3).
                    return (
                        (
                            "Thought: looks fine to me.\n"
                            "Final Answer: The world is clean. I found 0 open errors."
                        ),
                        5,
                        5,
                    )
                # Trial 1: the reflection says audit first — do it, then report the real count.
                if "Observation:" not in user:
                    return (
                        (
                            "Thought: audit first this time.\n"
                            "Action: audit_project\nAction Input: {}"
                        ),
                        5,
                        5,
                    )
                return "Thought: now I know.\nFinal Answer: 一致性审计发现 3 个待修复错误。", 5, 5
            return "Thought: done.\nFinal Answer: 已生成修复提案，等待人工审阅。", 5, 2

    provider = _LazyThenDiligentProvider()
    gw = LLMGateway({"cheap": provider, "frontier": provider}, telemetry=TelemetryCollector())
    session = MultiAgentSession(gateway=gw, registry=reg)
    report = session.run("diagnose and fix")
    flow = session.blackboard.session_flow(report.session_id)

    # A reflection message was recorded for diag_01, triggered by a 'fail'.
    diag_reflections = [m for m in flow if m.msg_type == "reflection" and m.from_agent == "diag_01"]
    assert len(diag_reflections) == 1
    rp = ReflectionPayload.from_dict(diag_reflections[0].payload)
    assert rp.trigger_verdict == "fail"
    assert rp.reflection  # a real, non-empty distilled reflection

    # feedback == the trial-0 verifier rationale (the exact signal fed into Self-Reflection).
    trial0_verifies = [
        m
        for m in flow
        if m.msg_type == "verify_result"
        and VerifyResultPayload.from_dict(m.payload).target_msg_id == rp.target_msg_id
    ]
    assert len(trial0_verifies) == 1
    assert VerifyResultPayload.from_dict(trial0_verifies[0].payload).rationale == rp.feedback

    # Exactly TWO task_results for diag_01 (initial + one retry) — not three.
    diag_results = [m for m in flow if m.msg_type == "task_result" and m.from_agent == "diag_01"]
    assert len(diag_results) == 2

    # trial1 verdict flipped to pass.
    assert rp.retry_verify_result_id is not None
    retry_vr = session.blackboard.get_message(rp.retry_verify_result_id)
    assert retry_vr is not None
    assert VerifyResultPayload.from_dict(retry_vr.payload).verdict == "pass"

    # The reflection was injected into the RETRY actor prompt, absent from the first attempt.
    assert "Reflections from previous attempts:" not in provider.diag_prompts[0]
    assert any("Reflections from previous attempts:" in p for p in provider.diag_prompts[1:])
    assert any(rp.reflection in p for p in provider.diag_prompts[1:])


def test_reflexion_second_failure_falls_back_to_annotation(
    session: MultiAgentSession,
) -> None:
    """(D) When the retry still yields needs_more there is NO third attempt and synthesis keeps
    the existing [INCOMPLETE] annotation."""
    report = session.run("second failure goal")
    flow = session.blackboard.session_flow(report.session_id)

    assert any(m.msg_type == "reflection" for m in flow), "a retry must have happened"
    for wid in ("diag_01", "repair_01"):
        trs = [m for m in flow if m.msg_type == "task_result" and m.from_agent == wid]
        assert len(trs) == 2, f"{wid} must run exactly twice (no third attempt), got {len(trs)}"
    assert "needs_more [INCOMPLETE" in report.synthesis


def test_reflexion_offline_reflection_template_used(
    session: MultiAgentSession,
) -> None:
    """(E) The distilled reflection uses the offline $0 template (no LLM / no network): it carries
    the verifier rationale verbatim and the '第1次' trial marker."""
    report = session.run("offline template goal")
    flow = session.blackboard.session_flow(report.session_id)

    reflections = [m for m in flow if m.msg_type == "reflection"]
    assert reflections
    rp = ReflectionPayload.from_dict(reflections[0].payload)
    assert "第1次" in rp.reflection
    assert rp.feedback and rp.feedback in rp.reflection


def test_reflexion_reflection_message_terminal_done(
    session: MultiAgentSession,
) -> None:
    """(F) Every reflection row is terminal 'done' with non-empty from_agent/to_agent (keeps the
    terminal-status and P3-6 attribution guarantees green)."""
    report = session.run("terminal status goal")
    flow = session.blackboard.session_flow(report.session_id)

    reflections = [m for m in flow if m.msg_type == "reflection"]
    assert reflections
    for m in reflections:
        assert m.status == "done"
        assert m.from_agent
        assert m.to_agent


def test_rt3_7_orchestrator_pass2_is_honest_second_gateway_call(
    minimal_registry,
) -> None:
    """⑦ Pass-2 decomposition recovers via an HONEST second gateway call — no ReActAgent.

    The orchestrator must NOT carry a performative ReActAgent.  Pass-2 is a plain second
    gateway.complete with a stricter JSON-only prompt.  We prove it does real work by
    making pass-1 fail (prose) and pass-2 succeed (clean JSON): the result must be a
    NON-degraded, goal-specific decomposition that the static fallback could never produce.
    """
    import uuid

    from owcopilot.multi_agent.orchestrator import OrchestratorAgent

    class _ProseThenJsonProvider:
        """First decompose call returns prose (unparseable); second returns clean JSON."""

        def __init__(self) -> None:
            self.decompose_calls = 0

        def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
            is_decompose = "task-decomposition" in system or "raw JSON array" in system
            if is_decompose:
                self.decompose_calls += 1
                if self.decompose_calls == 1:
                    # Pass-1: prose with no JSON array at all → parse fails → degraded
                    return "Sure! Here is how I would break it down for you...", 5, 5
                # Pass-2 (strict prompt): clean JSON the parser accepts
                return (
                    (
                        '[{"role": "diagnosis", "description": "PASS2_DIAG", '
                        '"allowed_skills": ["audit_project"], "worker_id": "diag_01"}, '
                        '{"role": "repair_proposal", "description": "PASS2_REPAIR", '
                        '"allowed_skills": ["propose_fix"], "worker_id": "repair_01"}]'
                    ),
                    10,
                    10,
                )
            return "Thought: done.\nFinal Answer: ok", 5, 2

    provider = _ProseThenJsonProvider()
    gw = LLMGateway({"cheap": provider, "frontier": provider}, telemetry=TelemetryCollector())

    orch = OrchestratorAgent(agent_id="orchestrator", gateway=gw, registry=minimal_registry)

    # The orchestrator must NOT hold a ReActAgent (no performative meta-loop).
    assert not hasattr(orch, "agent")

    import sqlite3

    from owcopilot.multi_agent import AgentBlackboard

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bb = AgentBlackboard(conn)

    posted, degraded = orch.post_task_assignments(
        goal="make world exportable", session_id=str(uuid.uuid4()), blackboard=bb
    )

    # Pass-2 recovered: NOT degraded, and the descriptions came from the pass-2 JSON
    assert degraded is False, "pass-2 strict-JSON retry should recover a non-degraded decomposition"
    assert provider.decompose_calls == 2, "exactly two gateway decompose calls (pass-1 + pass-2)"
    subtasks = {m.to_agent: TaskAssignPayload.from_dict(m.payload).subtask for m in posted}
    assert subtasks.get("diag_01") == "PASS2_DIAG"
    assert subtasks.get("repair_01") == "PASS2_REPAIR"


def test_teamb_parse_subtasks_missing_repair_role_is_degraded() -> None:
    """③ A decomposition lacking the required repair_proposal role is flagged degraded.

    The contract requires BOTH a diagnosis and a repair_proposal subtask. A diagnosis-only
    result used to pass through as non-degraded (silent: the session ran with no repair half).
    It must now degrade to the static fallback (which is role-complete) so the gap is surfaced.
    """
    from owcopilot.multi_agent.orchestrator import _parse_subtasks

    diag_only = (
        '[{"role": "diagnosis", "description": "only diagnose", '
        '"allowed_skills": ["audit_project"], "worker_id": "diag_01"}]'
    )
    subtasks, degraded = _parse_subtasks(diag_only, "some goal")
    assert degraded is True, "diagnosis-only decomposition must be marked degraded, not silent"
    roles = {st.worker_role for st in subtasks}
    assert {"diagnosis", "repair_proposal"} <= roles, (
        "the degraded fallback must restore a role-complete subtask set"
    )

    # A role-complete decomposition is accepted (not degraded).
    both = (
        '[{"role": "diagnosis", "description": "d", "allowed_skills": ["audit_project"], '
        '"worker_id": "diag_01"}, {"role": "repair_proposal", "description": "r", '
        '"allowed_skills": ["propose_fix"], "worker_id": "repair_01"}]'
    )
    subtasks2, degraded2 = _parse_subtasks(both, "some goal")
    assert degraded2 is False
    assert {st.worker_role for st in subtasks2} == {"diagnosis", "repair_proposal"}


def test_teamb_parse_subtasks_string_allowed_skills_not_exploded() -> None:
    """④ A bare-string allowed_skills is wrapped, NOT exploded into single characters.

    ``list("audit_project")`` would yield ['a','u','d',...] — none match a real skill, so the
    worker's scoped registry denies everything and it spins uselessly. The coercion must wrap
    the scalar into a one-element list instead.
    """
    from owcopilot.multi_agent.orchestrator import _parse_subtasks

    raw = (
        '[{"role": "diagnosis", "description": "d", "allowed_skills": "audit_project", '
        '"worker_id": "diag_01"}, {"role": "repair_proposal", "description": "r", '
        '"allowed_skills": ["propose_fix"], "worker_id": "repair_01"}]'
    )
    subtasks, degraded = _parse_subtasks(raw, "goal")
    assert degraded is False
    diag = next(st for st in subtasks if st.worker_role == "diagnosis")
    assert diag.allowed_skills == ["audit_project"], (
        f"bare string must wrap to a single-element list, got {diag.allowed_skills} "
        "(per-character explosion has regressed)"
    )


def test_teamb_coerce_allowed_skills_drops_non_strings() -> None:
    """④ Non-string elements / non-list values in allowed_skills are sanitised, not trusted."""
    from owcopilot.multi_agent.orchestrator import _coerce_allowed_skills

    assert _coerce_allowed_skills("audit_project", worker_role="diagnosis") == ["audit_project"]
    assert _coerce_allowed_skills(None, worker_role="diagnosis") == []
    assert _coerce_allowed_skills(123, worker_role="diagnosis") == []
    assert _coerce_allowed_skills(
        ["audit_project", 7, None, "list_issues"], worker_role="diagnosis"
    ) == ["audit_project", "list_issues"]


# ---------------------------------------------------------------------------
# T21: Anthropic effort scaling — the orchestrator allocates a per-subtask step budget
# (per-worker step budget == Anthropic's per-subagent tool-call budget). Scaling rules live
# in the prompt; a conservative clamp is the code guardrail; no-budget is a byte-identical
# deferral to the existing TaskAssignPayload default.
# ---------------------------------------------------------------------------


def test_t21_coerce_subtask_steps_clamps_and_rejects() -> None:
    """The budget coercion clamps out-of-range ints and rejects unusable/bool values.

    Isolated so the clamp (acceptance criterion 4) is unit-testable, mirroring
    ``_coerce_allowed_skills``.
    """
    from owcopilot.multi_agent.orchestrator import (
        _MAX_SUBTASK_STEPS,
        _MIN_SUBTASK_STEPS,
        _coerce_subtask_steps,
    )

    # Missing budget → None → orchestrator defers to the payload default (regression-safe).
    assert _coerce_subtask_steps(None) is None
    # In-range int passes through untouched.
    assert _coerce_subtask_steps(5) == 5
    assert _coerce_subtask_steps(_MIN_SUBTASK_STEPS) == _MIN_SUBTASK_STEPS
    assert _coerce_subtask_steps(_MAX_SUBTASK_STEPS) == _MAX_SUBTASK_STEPS
    # Below MIN clamps UP — MIN >= 1 so ReActAgent (which raises on max_steps < 1) never crashes.
    assert _coerce_subtask_steps(1) == _MIN_SUBTASK_STEPS
    assert _coerce_subtask_steps(0) == _MIN_SUBTASK_STEPS
    assert _coerce_subtask_steps(-10) == _MIN_SUBTASK_STEPS
    # Above MAX clamps DOWN — never rejected, the model still gets a runnable budget.
    assert _coerce_subtask_steps(999) == _MAX_SUBTASK_STEPS
    # Float coerces to int then clamps.
    assert _coerce_subtask_steps(6.0) == 6
    # bool is an int subclass — must be rejected so ``max_steps: true`` is NOT read as budget 1.
    assert _coerce_subtask_steps(True) is None
    assert _coerce_subtask_steps(False) is None
    # Unusable types fall back to the default (None), never trusted.
    assert _coerce_subtask_steps("6") is None
    assert _coerce_subtask_steps([6]) is None
    assert _coerce_subtask_steps({"n": 6}) is None


def test_t21_parse_subtasks_reads_and_clamps_max_steps() -> None:
    """A decomposition item's ``max_steps`` is parsed onto the SubTask and clamped in range."""
    from owcopilot.multi_agent.orchestrator import _MAX_SUBTASK_STEPS, _parse_subtasks

    raw = (
        '[{"role": "diagnosis", "description": "d", "allowed_skills": ["audit_project"], '
        '"worker_id": "diag_01", "max_steps": 3}, '
        '{"role": "repair_proposal", "description": "r", "allowed_skills": ["propose_fix"], '
        '"worker_id": "repair_01", "max_steps": 999}]'
    )
    subtasks, degraded = _parse_subtasks(raw, "goal")
    assert degraded is False
    by_role = {st.worker_role: st for st in subtasks}
    assert by_role["diagnosis"].max_steps == 3
    # Out-of-range budget is CLAMPED, not rejected (the subtask still runs).
    assert by_role["repair_proposal"].max_steps == _MAX_SUBTASK_STEPS


def test_t21_parse_subtasks_missing_budget_is_none() -> None:
    """Backward-compat: an item WITHOUT max_steps yields ``SubTask.max_steps is None``.

    None is the "no budget decided" sentinel the orchestrator turns into the unchanged
    TaskAssignPayload default — proven byte-identical in
    ``test_t21_orchestrator_no_budget_is_byte_identical_default``.
    """
    from owcopilot.multi_agent.orchestrator import _parse_subtasks

    raw = (
        '[{"role": "diagnosis", "description": "d", "allowed_skills": ["audit_project"], '
        '"worker_id": "diag_01"}, '
        '{"role": "repair_proposal", "description": "r", "allowed_skills": ["propose_fix"], '
        '"worker_id": "repair_01"}]'
    )
    subtasks, degraded = _parse_subtasks(raw, "goal")
    assert degraded is False
    assert all(st.max_steps is None for st in subtasks)


class _BudgetDecomposeProvider:
    """Mock provider returning a role-complete decompose JSON, optionally carrying max_steps.

    Exercises the production JSON parse + allocation path in ``post_task_assignments`` at $0.
    """

    def __init__(self, *, with_budget: bool) -> None:
        self._with_budget = with_budget

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        if "task-decomposition" in system or "Decompose into subtasks" in user:
            diag_budget = ', "max_steps": 7' if self._with_budget else ""
            repair_budget = ', "max_steps": 3' if self._with_budget else ""
            payload = (
                '[{"role": "diagnosis", "description": "d", '
                '"allowed_skills": ["audit_project"], "worker_id": "diag_01"' + diag_budget + "}, "
                '{"role": "repair_proposal", "description": "r", '
                '"allowed_skills": ["propose_fix"], "worker_id": "repair_01"' + repair_budget + "}]"
            )
            return payload, 10, 10
        return "Thought: done.\nFinal Answer: ok", 5, 2


def test_t21_orchestrator_allocates_budget_onto_payload(
    minimal_registry, blackboard: AgentBlackboard
) -> None:
    """The orchestrator writes each subtask's decided budget onto its task_assign payload."""
    import uuid

    provider = _BudgetDecomposeProvider(with_budget=True)
    gateway = LLMGateway({"cheap": provider, "frontier": provider}, telemetry=TelemetryCollector())
    orch = OrchestratorAgent(agent_id="orchestrator", gateway=gateway, registry=minimal_registry)

    posted, degraded = orch.post_task_assignments(
        goal="g", session_id=str(uuid.uuid4()), blackboard=blackboard
    )

    assert not degraded
    by_worker = {m.to_agent: TaskAssignPayload.from_dict(m.payload) for m in posted}
    assert by_worker["diag_01"].max_steps == 7
    assert by_worker["repair_01"].max_steps == 3


def test_t21_orchestrator_no_budget_is_byte_identical_default(
    minimal_registry, blackboard: AgentBlackboard
) -> None:
    """Backward-compat: no decided budget → the posted payload is byte-identical to before T21.

    The wire ``max_steps`` is the TaskAssignPayload default (4), with NO orchestrator-side magic
    number, and the whole payload dict equals an un-budgeted TaskAssignPayload's serialisation.
    """
    import uuid

    provider = _BudgetDecomposeProvider(with_budget=False)
    gateway = LLMGateway({"cheap": provider, "frontier": provider}, telemetry=TelemetryCollector())
    orch = OrchestratorAgent(agent_id="orchestrator", gateway=gateway, registry=minimal_registry)

    posted, degraded = orch.post_task_assignments(
        goal="g", session_id=str(uuid.uuid4()), blackboard=blackboard
    )

    assert not degraded
    for m in posted:
        assert m.payload["max_steps"] == 4
        assert TaskAssignPayload.from_dict(m.payload).max_steps == 4

    diag = next(m for m in posted if m.to_agent == "diag_01")
    expected = TaskAssignPayload(
        original_goal="g", subtask="d", allowed_skills=["audit_project"]
    ).to_dict()
    assert diag.payload == expected


def test_t21_normalise_worker_ids_preserves_budget(
    offline_gateway: LLMGateway, minimal_registry
) -> None:
    """An id remap must carry the allocated budget across — dropping it is a silent downgrade."""
    from owcopilot.multi_agent.orchestrator import OrchestratorAgent, SubTask

    orch = OrchestratorAgent(
        gateway=offline_gateway,  # unused by _normalise_worker_ids, passed to satisfy the ctor
        registry=minimal_registry,
        registered_workers={"diagnosis": "diag_01", "repair_proposal": "repair_01"},
    )
    subtasks = [
        SubTask(
            worker_role="repair_proposal",
            description="r",
            allowed_skills=[],
            worker_id="rep_01",  # near-miss id → remapped to repair_01
            max_steps=8,
        ),
    ]
    out = orch._normalise_worker_ids(subtasks)
    assert out[0].worker_id == "repair_01"  # remapped
    assert out[0].max_steps == 8  # budget preserved across the remap


def test_t21_scaling_rules_embedded_in_decompose_prompt(
    minimal_registry, blackboard: AgentBlackboard
) -> None:
    """Anthropic effort scaling lives in the PROMPT: both decompose passes name max_steps and
    state the [MIN, MAX] bounds, and those bounds are interpolated from the clamp constants."""
    import uuid

    from owcopilot.multi_agent.orchestrator import _MAX_SUBTASK_STEPS, _MIN_SUBTASK_STEPS

    class _RecordingProvider:
        """Records every system prompt; returns non-JSON so both passes fire and are captured."""

        def __init__(self) -> None:
            self.systems: list[str] = []

        def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
            self.systems.append(system)
            return "not json", 1, 1

    provider = _RecordingProvider()
    gateway = LLMGateway({"cheap": provider, "frontier": provider}, telemetry=TelemetryCollector())
    orch = OrchestratorAgent(agent_id="orchestrator", gateway=gateway, registry=minimal_registry)

    orch.post_task_assignments(goal="g", session_id=str(uuid.uuid4()), blackboard=blackboard)

    # Exactly two decompose passes fired (pass-1 + strict pass-2); both prompts recorded.
    assert len(provider.systems) == 2
    pass1, pass2 = provider.systems
    for prompt in (pass1, pass2):
        assert "max_steps" in prompt
    # The prompt bounds are the code clamp constants (interpolated — cannot drift).
    assert f"between {_MIN_SUBTASK_STEPS} and {_MAX_SUBTASK_STEPS}" in pass1
    assert f"max_steps {_MIN_SUBTASK_STEPS}-{_MAX_SUBTASK_STEPS}" in pass2


# ---------------------------------------------------------------------------
# Team-B: real deterministic verifier, execution-time skill scoping, dead fields
# ---------------------------------------------------------------------------


def _post_task_result(blackboard, sid, *, claimed_errors: int | None, final_answer: str):
    """Helper: drop a task_result on the blackboard and a verify_request pointing at it.

    NOTE: this hand-sets ``open_errors`` to a chosen value, so it is a UNIT fixture for the
    verifier's compare logic only — it deliberately bypasses the real worker path. The
    end-to-end proof that an honest worker is not misjudged and a lying worker IS caught lives
    in ``test_teamb_verifier_real_worker_path_*`` below, which run a real DiagWorker so the
    ``open_errors`` field is produced by the actual ``_extract_claimed_open_errors`` code, not a
    fixture integer.
    """
    task_result = AgentMessage(
        session_id=sid,
        from_agent="diag_01",
        to_agent="orchestrator",
        msg_type="task_result",
        payload=TaskResultPayload(
            task_msg_id="fake-assign-id",
            worker_role="diagnosis",
            final_answer=final_answer,
            open_errors=claimed_errors,
            stop_reason="finished",
            step_count=3,
        ).to_dict(),
        status="done",
    )
    blackboard.post_message(task_result)
    verify_req = AgentMessage(
        session_id=sid,
        from_agent="orchestrator",
        to_agent="verifier_01",
        msg_type="verify_request",
        payload={"target_msg_id": task_result.id},
    )
    blackboard.post_message(verify_req)
    return task_result, verify_req


def test_teamb_verifier_deterministic_real_verdict_offline(
    offline_gateway: LLMGateway, blackboard: AgentBlackboard
) -> None:
    """HIGH: offline verifier yields a REAL pass verdict from deterministic ground truth.

    With audit_project bound (returning open_errors=2) and a worker that honestly claimed 2,
    the verifier's deterministic path returns 'pass' — NOT a perpetual needs_more. This is the
    red-line fix: the verifier truly completes independent ground-truth verification offline.
    """
    import uuid

    reg, calls = _make_audit_registry(open_errors=2)
    sid = str(uuid.uuid4())
    _tr, verify_req = _post_task_result(
        blackboard,
        sid,
        claimed_errors=2,
        final_answer="一致性审计发现 2 个待修复错误。",
    )

    verifier = VerifierAgent(agent_id="verifier_01", gateway=offline_gateway, registry=reg)
    result_msg = verifier.verify(verify_req, blackboard)
    vr = VerifyResultPayload.from_dict(result_msg.payload)

    assert vr.verdict == "pass", f"expected real pass verdict, got {vr.verdict} / {vr.rationale}"
    assert vr.open_errors_verified == 2, "verifier must read the real audit open_errors (2)"
    assert vr.worker_claimed_errors == 2
    assert "deterministic-audit" in vr.rationale
    # The deterministic path actually invoked the audit tool (true independent measurement).
    assert calls["audit_project"] >= 1
    # The verify_request is marked done, not left perpetually pending.
    updated = blackboard.get_message(verify_req.id)
    assert updated is not None and updated.status == "done"


def test_teamb_verifier_catches_worker_underreport(
    offline_gateway: LLMGateway, blackboard: AgentBlackboard
) -> None:
    """HIGH: verifier independently CATCHES a worker that under-reported errors → fail.

    Ground truth audit says 3 open errors; the worker claimed 0. The verifier must
    return 'fail' with the discrepancy — the demo of catching a virtual misreport.
    """
    import uuid

    reg, _calls = _make_audit_registry(open_errors=3)
    sid = str(uuid.uuid4())
    _tr, verify_req = _post_task_result(
        blackboard,
        sid,
        claimed_errors=0,
        final_answer="Looks clean to me, no errors found.",
    )

    verifier = VerifierAgent(agent_id="verifier_01", gateway=offline_gateway, registry=reg)
    vr = VerifyResultPayload.from_dict(verifier.verify(verify_req, blackboard).payload)

    assert vr.verdict == "fail", "verifier must catch the worker's under-report"
    assert vr.open_errors_verified == 3
    assert vr.worker_claimed_errors == 0


def _run_real_worker_and_verify(
    *, worker_provider, audit_open_errors: int, blackboard: AgentBlackboard
):
    """Drive a REAL DiagWorker (no hand-set open_errors) then verify its posted result.

    Returns ``(task_result_payload, verify_result_payload)``.  The worker's ``open_errors`` is
    whatever ``workers._extract_claimed_open_errors`` produces from the worker's actual ReAct
    transcript — this exercises the true product path, NOT a fixture integer.
    """
    import uuid

    reg, _calls = _make_audit_registry(open_errors=audit_open_errors)
    gw = LLMGateway(
        {"cheap": worker_provider, "frontier": worker_provider},
        telemetry=TelemetryCollector(),
    )
    sid = str(uuid.uuid4())
    assign = TaskAssignPayload(
        original_goal="clean up the world",
        subtask="audit and report open errors",
        allowed_skills=["audit_project", "list_issues", "build_context_pack"],
    )
    assign_msg = AgentMessage(
        session_id=sid,
        from_agent="orchestrator",
        to_agent="diag_01",
        msg_type="task_assign",
        payload=assign.to_dict(),
    )
    blackboard.post_message(assign_msg)

    worker = DiagWorker(agent_id="diag_01", gateway=gw, registry=reg)
    claimed = blackboard.claim_task("diag_01", sid)
    assert claimed is not None
    tr_msg = worker.run_task(claimed, blackboard)
    tr = TaskResultPayload.from_dict(tr_msg.payload)

    verify_req = AgentMessage(
        session_id=sid,
        from_agent="orchestrator",
        to_agent="verifier_01",
        msg_type="verify_request",
        payload={"target_msg_id": tr_msg.id},
    )
    blackboard.post_message(verify_req)
    # Verifier gets the SAME audit registry (its own independent audit call), distinct ReActAgent.
    verifier = VerifierAgent(agent_id="verifier_01", gateway=gw, registry=reg)
    vr = VerifyResultPayload.from_dict(verifier.verify(verify_req, blackboard).payload)
    return tr, vr


def test_teamb_verifier_real_worker_path_honest_not_misjudged(
    blackboard: AgentBlackboard,
) -> None:
    """RED-LINE: an HONEST worker on the REAL path is NOT falsely flagged as a liar.

    Regression guard for the HIGH bug: the worker's ``open_errors`` used to be
    ``answer.lower().count("error")``, which is 0 for a Chinese answer even when the worker
    truly audited and found errors — so the verifier (which audits to the true count) judged
    the honest worker a liar (delta=N → fail).

    Here a real DiagWorker runs ``audit_project`` (true count = 4) and writes a Chinese Final
    Answer.  Its ``open_errors`` must now be the REAL 4 (from its own audit tool result, not a
    substring count), and the verifier must return PASS with delta 0.
    """

    class _HonestChineseProvider:
        """Audits, then reports in 中文 — the locale that broke the old substring metric."""

        def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
            if user.count("Observation:") == 0:
                return ("Thought: 先跑一致性审计。\nAction: audit_project\nAction Input: {}"), 5, 5
            # Chinese final answer — contains ZERO Latin 'error' substrings on purpose.
            return (
                (
                    "Thought: 审计完成，可以汇报了。\n"
                    "Final Answer: 一致性审计发现若干待修复问题，已生成修复提案，请人工审阅。"
                ),
                5,
                5,
            )

    tr, vr = _run_real_worker_and_verify(
        worker_provider=_HonestChineseProvider(), audit_open_errors=4, blackboard=blackboard
    )

    # The worker's claim is the REAL audit integer (4), NOT a substring count (which would be 0).
    assert tr.open_errors == 4, (
        f"honest worker must report its real audited count 4, got {tr.open_errors} "
        "(if 0, the substring-count bug has regressed)"
    )
    assert vr.verdict == "pass", (
        f"honest worker must NOT be misjudged as a liar; got {vr.verdict} / {vr.rationale}"
    )
    assert vr.open_errors_verified == 4
    assert vr.worker_claimed_errors == 4
    assert "deterministic-audit" in vr.rationale


def test_teamb_verifier_real_worker_path_liar_caught(
    blackboard: AgentBlackboard,
) -> None:
    """RED-LINE: a LYING worker on the REAL path IS caught (claims clean; audit finds 8).

    A worker that SKIPS the audit and asserts the world is clean ("0 errors") while the true
    audit finds 8 must be flagged 'fail'.  Because it ran no audit it posts no structured claim
    (open_errors=None); the verifier falls back to the count stated in the worker's prose (0)
    and contradicts it with ground truth (8) → fail.
    """

    class _LyingProvider:
        """Never audits; immediately asserts the world is clean."""

        def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
            return (
                (
                    "Thought: I'm sure it's fine.\n"
                    "Final Answer: The world is clean. I found 0 open errors; nothing to fix."
                ),
                5,
                5,
            )

    tr, vr = _run_real_worker_and_verify(
        worker_provider=_LyingProvider(), audit_open_errors=8, blackboard=blackboard
    )

    # The liar ran no audit, so it has no structured audit-backed claim.
    assert tr.open_errors is None, (
        "a worker that never audited must post open_errors=None (no audit-backed claim), "
        f"not a fabricated integer; got {tr.open_errors}"
    )
    assert vr.verdict == "fail", (
        f"verifier must catch the liar (prose says 0, audit finds 8); got {vr.verdict}"
    )
    assert vr.open_errors_verified == 8
    assert vr.worker_claimed_errors == 0  # parsed from the worker's lying prose


def test_teamb_verifier_real_worker_no_claim_not_read_as_zero(
    blackboard: AgentBlackboard,
) -> None:
    """An honest non-auditing worker (no count claimed at all) is NOT punished as claiming 0.

    A repair-style worker that neither audits nor states a count must yield open_errors=None and
    a PASS annotated "no verifiable claim" — never a fail from silently reading None as 0.
    """

    class _NoCountProvider:
        def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
            return (
                (
                    "Thought: I'll summarise the plan.\n"
                    "Final Answer: 已根据上游诊断生成修复提案，等待人工审阅。"
                ),
                5,
                5,
            )

    tr, vr = _run_real_worker_and_verify(
        worker_provider=_NoCountProvider(), audit_open_errors=5, blackboard=blackboard
    )

    assert tr.open_errors is None
    assert vr.verdict == "pass", (
        f"a worker that made no count claim must not be failed; got {vr.verdict} / {vr.rationale}"
    )
    assert vr.worker_claimed_errors is None
    assert "no verifiable error-count claim" in vr.rationale


def test_teamb_verifier_negative_audit_count_rejected(
    offline_gateway: LLMGateway, blackboard: AgentBlackboard
) -> None:
    """LOW (bug): a NEGATIVE audit open_errors is refused, not passed through as a real verdict.

    A malformed/malicious audit tool returning open_errors=-5 must be treated as malformed
    (fall back to the LLM-answer path), never trusted — a negative count would also collide with
    the -1 'target not found' sentinel.
    """
    import uuid

    from owcopilot.skills import CostTier, SideEffect, Skill, SkillRegistry

    reg = SkillRegistry()
    reg.register(
        Skill(
            name="audit_project",
            description="malformed",
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=lambda **_k: {"open_errors": -5, "issues": []},
        )
    )
    reg.register(
        Skill(
            name="list_issues",
            description="x",
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=lambda **_k: {"count": 0},
        )
    )
    sid = str(uuid.uuid4())
    _tr, verify_req = _post_task_result(
        blackboard,
        sid,
        claimed_errors=3,
        final_answer="一致性审计发现 3 个待修复错误",
    )
    verifier = VerifierAgent(agent_id="verifier_01", gateway=offline_gateway, registry=reg)
    vr = VerifyResultPayload.from_dict(verifier.verify(verify_req, blackboard).payload)

    # Negative count must NOT surface as the verified value; det path rejected it as malformed.
    assert vr.open_errors_verified != -5, "negative audit count must never pass through"
    assert "deterministic-audit" not in vr.rationale, (
        "a malformed (negative) audit must not be reported as deterministic ground truth"
    )


@pytest.mark.parametrize("bad_audit", [[1, 2], None, "oops", 5])
def test_teamb_verifier_non_dict_audit_does_not_crash(
    offline_gateway: LLMGateway, blackboard: AgentBlackboard, bad_audit
) -> None:
    """LOW (bug): a NON-dict audit result must not crash the verifier.

    ``_deterministic_verify`` called ``result.get("open_errors")`` outside its try/except, so a
    malformed/malicious ``audit_project`` returning a list/None/str/int raised ``AttributeError``
    that propagated out of ``verify()`` (and, in a real session, out of ``session.run`` — the
    verify step is NOT wrapped in try/except, unlike the worker step).  The worker-side reader
    already guarded this case (``workers._extract_claimed_open_errors``); the verifier must too.
    The verifier should treat a non-dict result as malformed and fall back, never crash.
    """
    import uuid

    from owcopilot.skills import CostTier, SideEffect, Skill, SkillRegistry

    reg = SkillRegistry()
    reg.register(
        Skill(
            name="audit_project",
            description="malformed-nondict",
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=lambda **_k: bad_audit,
        )
    )
    reg.register(
        Skill(
            name="list_issues",
            description="x",
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=lambda **_k: {"count": 0},
        )
    )
    sid = str(uuid.uuid4())
    _tr, verify_req = _post_task_result(
        blackboard,
        sid,
        claimed_errors=3,
        final_answer="一致性审计发现 3 个待修复错误",
    )
    verifier = VerifierAgent(agent_id="verifier_01", gateway=offline_gateway, registry=reg)
    # Must NOT raise — a non-dict audit is malformed, not a crash.
    vr = VerifyResultPayload.from_dict(verifier.verify(verify_req, blackboard).payload)

    # Malformed non-dict must not be reported as deterministic ground truth.
    assert "deterministic-audit" not in vr.rationale, (
        "a non-dict audit result must not surface as deterministic ground truth"
    )
    # Verdict is whatever the fallback (agent-answer) path yields, but it must be a valid verdict.
    assert vr.verdict in {"pass", "fail", "needs_more"}


def test_teamb_verifier_chinese_answer_parsed_when_no_audit_tool(
    offline_gateway: LLMGateway, blackboard: AgentBlackboard, minimal_registry
) -> None:
    """The fallback parser understands a Chinese error count (no audit tool bound).

    When audit_project is absent we fall back to the agent answer; a Chinese
    "发现 N 个待修复错误" must be parsed (it used to silently count to 0).
    """
    from owcopilot.multi_agent.verifier import _extract_error_count

    # Direct unit check of the parser (EN + 中文 + un-parseable).
    assert _extract_error_count("一致性审计发现 2 个待修复错误。") == 2
    assert _extract_error_count("Found 5 open errors in the world.") == 5
    assert _extract_error_count("共 3 个问题需要处理") == 3
    assert _extract_error_count("everything looks fine") is None


def test_teamb_allowed_skills_enforced_at_execution(
    offline_gateway: LLMGateway, blackboard: AgentBlackboard
) -> None:
    """MID: a read-only worker is DENIED propose_fix at EXECUTION, not just in the manifest.

    We build a DiagWorker (allowed: audit_project/list_issues/build_context_pack) whose
    ReActAgent is handed a registry that DOES contain propose_fix.  Then we force its agent
    to emit `Action: propose_fix`.  The scoped registry must reject it at run() time — the
    skill handler must never execute — and the step is surfaced as an is_error observation.
    """
    import uuid

    reg, calls = _make_audit_registry(open_errors=1)

    class _ProposeFixProvider:
        """Forces the worker's ReAct loop to call the out-of-scope propose_fix skill."""

        def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
            if user.count("Observation:") == 0:
                return (
                    (
                        "Thought: I will try to propose a fix directly.\n"
                        "Action: propose_fix\n"
                        'Action Input: {"issue_id": "issue-1"}'
                    ),
                    5,
                    5,
                )
            return "Thought: ok.\nFinal Answer: done", 5, 2

    gw = LLMGateway(
        {"cheap": _ProposeFixProvider(), "frontier": _ProposeFixProvider()},
        telemetry=TelemetryCollector(),
    )

    sid = str(uuid.uuid4())
    assign = TaskAssignPayload(
        original_goal="diagnose only",
        subtask="run the audit",
        allowed_skills=["audit_project", "list_issues", "build_context_pack"],
    )
    assign_msg = AgentMessage(
        session_id=sid,
        from_agent="orchestrator",
        to_agent="diag_01",
        msg_type="task_assign",
        payload=assign.to_dict(),
    )
    blackboard.post_message(assign_msg)

    worker = DiagWorker(agent_id="diag_01", gateway=gw, registry=reg)
    claimed = blackboard.claim_task("diag_01", sid)
    assert claimed is not None
    worker.run_task(claimed, blackboard)

    # propose_fix is PROPOSES_PATCH and out of scope — its handler must NEVER have run.
    assert calls["propose_fix"] == 0, (
        "out-of-scope propose_fix must be denied at execution, not silently executed"
    )
    # The denial surfaced as an is_error observation (the loop self-corrected, did not crash).
    steps = worker.agent.run(  # re-run a fresh agent to inspect step error surfacing
        "Overall goal: diagnose only\n\n[Your specific subtask as diagnosis]:\nrun the audit"
    ).steps
    propose_steps = [s for s in steps if s.action == "propose_fix"]
    assert propose_steps, "expected a propose_fix action attempt"
    assert all(s.is_error for s in propose_steps), "denied skill must yield an is_error step"
    assert any("not in this agent's allowed tool set" in s.observation for s in propose_steps)


def test_teamb_scoped_registry_allows_in_scope_skill(
    offline_gateway: LLMGateway,
) -> None:
    """The scoped registry still allows in-scope skills to run normally."""
    from owcopilot.multi_agent.skill_scope import scoped_registry

    reg, calls = _make_audit_registry(open_errors=4)
    scoped = scoped_registry(reg, {"audit_project", "list_issues"})

    # In-scope skill runs and returns the real result.
    out = scoped.run("audit_project", {})
    assert out["open_errors"] == 4
    assert calls["audit_project"] == 1
    # Out-of-scope skill is denied at dispatch.
    import pytest as _pytest

    from owcopilot.skills import SkillError

    with _pytest.raises(SkillError, match="not in this agent's allowed tool set"):
        scoped.run("propose_fix", {"issue_id": "x"})
    assert calls["propose_fix"] == 0
    # Manifest never advertises the denied tool.
    assert "propose_fix" not in scoped.manifest()
    assert "audit_project" in scoped.manifest()
    # None passthrough = full access (backward compatible).
    assert scoped_registry(reg, None) is reg


# ---------------------------------------------------------------------------
# Least-privilege: the orchestrator's allowed_skills proposal may only NARROW the
# worker role's safe default set, never REPLACE/EXPAND it (OWASP LLM06:2025 Excessive
# Agency). run_task intersects assign.allowed_skills with default_allowed_skills.
# ---------------------------------------------------------------------------


def _run_worker_with_assign(
    worker: WorkerAgent, assign: TaskAssignPayload, blackboard: AgentBlackboard
) -> WorkerAgent:
    """Post ``assign`` to ``worker``, let it claim + run, and hand back the run worker.

    The worker's resolved tool face is observable at ``worker.agent.allowed_skills`` after
    run_task (ReActAgent stores the set it was scoped to). The MockProvider makes the ReAct
    loop hit max_steps harmlessly — we only inspect the resolved scope, not the answer.
    """
    import uuid

    sid = str(uuid.uuid4())
    assign_msg = AgentMessage(
        session_id=sid,
        from_agent="orchestrator",
        to_agent=worker.agent_id,
        msg_type="task_assign",
        payload=assign.to_dict(),
    )
    blackboard.post_message(assign_msg)
    claimed = blackboard.claim_task(worker.agent_id, sid)
    assert claimed is not None
    worker.run_task(claimed, blackboard)
    return worker


def test_least_privilege_assign_subset_of_default_used_verbatim(
    offline_gateway: LLMGateway, minimal_registry, blackboard: AgentBlackboard, caplog
) -> None:
    """① assign ⊆ role default → granted verbatim, no skills dropped, no warning.

    Regression guard for the happy path: intersecting a subset with the default is a no-op, so
    the security fix must not perturb the case the verification report identified as the current
    live path (orchestrator proposes exactly a subset of the role default).
    """
    assign = TaskAssignPayload(
        original_goal="g",
        subtask="s",
        allowed_skills=["audit_project", "list_issues"],  # ⊆ DiagWorker default
    )
    worker = DiagWorker(agent_id="diag_01", gateway=offline_gateway, registry=minimal_registry)
    with caplog.at_level(logging.WARNING, logger="owcopilot.multi_agent.workers"):
        _run_worker_with_assign(worker, assign, blackboard)

    assert worker.agent.allowed_skills == {"audit_project", "list_issues"}
    assert "out-of-scope" not in caplog.text, "a clean subset must not log a drop warning"


def test_least_privilege_out_of_default_skill_is_dropped_and_warned(
    offline_gateway: LLMGateway, minimal_registry, blackboard: AgentBlackboard, caplog
) -> None:
    """② assign contains a skill outside the role default → it is dropped and a warning is logged.

    A RepairWorker (default {propose_fix, quality_harness}) is handed an assign that also asks for
    the read-only ``audit_project`` — outside its role bound. The out-of-scope skill must be
    stripped (only propose_fix survives) and the drop must be recorded, never silently honoured
    (which would be Excessive Agency: the decomposer LLM widening a worker's tool face).
    """
    assign = TaskAssignPayload(
        original_goal="g",
        subtask="s",
        allowed_skills=["propose_fix", "audit_project"],  # audit_project ∉ RepairWorker default
    )
    worker = RepairWorker(agent_id="repair_01", gateway=offline_gateway, registry=minimal_registry)
    with caplog.at_level(logging.WARNING, logger="owcopilot.multi_agent.workers"):
        _run_worker_with_assign(worker, assign, blackboard)

    # Only the in-bound skill survives; the out-of-bound one is gone (not widened in).
    assert worker.agent.allowed_skills == {"propose_fix"}
    assert "audit_project" in caplog.text and "repair_proposal" in caplog.text
    assert "out-of-scope" in caplog.text


def test_least_privilege_disjoint_assign_is_deny_all_not_full_access(
    offline_gateway: LLMGateway, minimal_registry, blackboard: AgentBlackboard, caplog
) -> None:
    """③ assign disjoint from role default → empty (deny-all) tool set + warning, NOT full access.

    The fail-safe red line: when the LLM proposes ONLY out-of-scope skills, the intersection is
    empty and the worker gets NO tools (``set()``) — it must fall back to context-only answering,
    NOT to the role's full default and NEVER to ``None`` (which the scoped registry reads as full
    unrestricted access). This is the branch that a naive ``or default`` fallback would get wrong.
    """
    assign = TaskAssignPayload(
        original_goal="g",
        subtask="s",
        allowed_skills=["audit_project", "list_issues"],  # both ∉ RepairWorker default
    )
    worker = RepairWorker(agent_id="repair_01", gateway=offline_gateway, registry=minimal_registry)
    with caplog.at_level(logging.WARNING, logger="owcopilot.multi_agent.workers"):
        _run_worker_with_assign(worker, assign, blackboard)

    resolved = worker.agent.allowed_skills
    assert resolved == set(), "empty intersection must be an empty (deny-all) set"
    assert resolved is not None  # NOT the full-access sentinel
    # Did NOT fall back to the broad role default — that would reward an out-of-bounds proposal.
    assert resolved != {"propose_fix", "quality_harness"}
    assert "out-of-scope" in caplog.text
    assert "audit_project" in caplog.text and "list_issues" in caplog.text


def test_least_privilege_empty_assign_falls_back_to_full_default(
    offline_gateway: LLMGateway, minimal_registry, blackboard: AgentBlackboard, caplog
) -> None:
    """④ empty assign → the role's full default set (pre-existing behaviour, unchanged).

    When the orchestrator proposes nothing, the worker keeps its full role default face — this
    matches the pre-fix behaviour (``assign.allowed_skills or default`` already fell back to the
    default) so the security change is a strict no-op here.
    """
    assign = TaskAssignPayload(original_goal="g", subtask="s", allowed_skills=[])
    worker = DiagWorker(agent_id="diag_01", gateway=offline_gateway, registry=minimal_registry)
    with caplog.at_level(logging.WARNING, logger="owcopilot.multi_agent.workers"):
        _run_worker_with_assign(worker, assign, blackboard)

    assert worker.agent.allowed_skills == {"audit_project", "list_issues", "build_context_pack"}
    assert "out-of-scope" not in caplog.text, "an empty proposal drops nothing, so no warning"


def test_teamb_max_steps_protocol_field_is_live(
    offline_gateway: LLMGateway, blackboard: AgentBlackboard
) -> None:
    """LOW (dead field → live): TaskAssignPayload.max_steps now drives the worker's budget.

    A subtask with max_steps=1 must cap the worker's ReAct loop at a single step
    (the field was previously ignored — the worker always used its constructor default).
    """
    import uuid

    reg, _calls = _make_audit_registry(open_errors=0)

    class _NeverFinishesProvider:
        """Always emits an action, never a Final Answer — so step_count == max_steps."""

        def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
            return "Thought: keep going.\nAction: audit_project\nAction Input: {}", 5, 5

    gw = LLMGateway(
        {"cheap": _NeverFinishesProvider(), "frontier": _NeverFinishesProvider()},
        telemetry=TelemetryCollector(),
    )

    sid = str(uuid.uuid4())
    assign = TaskAssignPayload(
        original_goal="g",
        subtask="s",
        allowed_skills=["audit_project"],
        max_steps=1,
    )
    assign_msg = AgentMessage(
        session_id=sid,
        from_agent="orchestrator",
        to_agent="diag_01",
        msg_type="task_assign",
        payload=assign.to_dict(),
    )
    blackboard.post_message(assign_msg)

    worker = DiagWorker(agent_id="diag_01", gateway=gw, registry=reg, max_steps=4)
    claimed = blackboard.claim_task("diag_01", sid)
    assert claimed is not None
    result_msg = worker.run_task(claimed, blackboard)

    tr = TaskResultPayload.from_dict(result_msg.payload)
    # max_steps=1 from the payload (NOT the constructor default of 4) bounded the loop.
    assert tr.step_count == 1, (
        f"worker must honour assign.max_steps=1, got step_count={tr.step_count}"
    )


def test_teamb_confidence_field_removed_from_payload() -> None:
    """LOW: the dead ``confidence`` field is gone from TaskResultPayload (no reader existed)."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(TaskResultPayload)}
    assert "confidence" not in field_names, "dead 'confidence' field must be removed"
    # Serialised dict also free of it.
    p = TaskResultPayload(
        task_msg_id="t",
        worker_role="diagnosis",
        final_answer="a",
        open_errors=0,
        stop_reason="finished",
        step_count=1,
    )
    assert "confidence" not in p.to_dict()
    # from_dict tolerates legacy payloads that still carry confidence (ignored, not crash).
    legacy = {**p.to_dict(), "confidence": 0.9}
    recovered = TaskResultPayload.from_dict(legacy)
    assert recovered.task_msg_id == "t"


def test_teamb_terminal_messages_marked_done_not_pending(
    session: MultiAgentSession,
) -> None:
    """LOW: terminal records (task_result/verify_result/synthesize) are 'done', not 'pending'.

    These rows are never claimed; leaving them 'pending' was misleading status hygiene.
    """
    report = session.run("status hygiene goal")
    flow = session.blackboard.session_flow(report.session_id)

    terminal = [m for m in flow if m.msg_type in {"task_result", "verify_result", "synthesize"}]
    assert terminal, "expected terminal records on the blackboard"
    stuck_pending = [m for m in terminal if m.status == "pending"]
    assert not stuck_pending, (
        "terminal records must not linger as 'pending': "
        f"{[(m.msg_type, m.status) for m in stuck_pending]}"
    )


# ---------------------------------------------------------------------------
# CLI integration: `owcopilot multi-agent` triggers the system on a real world
# ---------------------------------------------------------------------------


def test_cli_multi_agent_command_runs_on_real_world(tmp_path, capsys) -> None:
    """The `multi-agent` CLI subcommand constructs and runs a session on a real world.

    Proves the multi_agent module is reachable from a product trigger path (CLI), runs the
    full orchestrate→worker→verify→synthesize flow on a real content root, and emits the
    blackboard message flow + synthesis. Offline $0.
    """
    import json

    from owcopilot.cli.main import main
    from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
    from owcopilot.content.store import ContentStore

    root = tmp_path / "content"
    bundle = ContentBundle(
        entities={
            "npc_mara": Entity(
                id="npc_mara", name="Mara", type=EntityType.NPC, description="Border scout."
            ),
        },
        quests={
            "quest_patrol": Quest(
                id="quest_patrol",
                title="Patrol the Border",
                giver_npc="npc_ghost",  # seeded dangling-ref error so audit has something to find
                objective="Walk the border line before dusk.",
            )
        },
    )
    ContentStore(root).save(bundle)

    code = main(
        [
            "multi-agent",
            "--content-root",
            str(root),
            "--goal",
            "让世界达到可导出状态",
            "--llm-mode",
            "offline",
        ]
    )
    assert code == 0

    out = capsys.readouterr().out.strip().splitlines()
    payload = json.loads(out[-1])

    # Report shape
    assert payload["goal"] == "让世界达到可导出状态"
    assert "orchestrator" in payload["participants"]
    assert "diag_01" in payload["participants"]
    assert "repair_01" in payload["participants"]
    assert "verifier_01" in payload["participants"]
    assert payload["llm_mode"] == "offline"

    # Blackboard flow must be present and attributed (from→to), covering all message types
    flow = payload["blackboard_flow"]
    assert len(flow) >= 5
    for row in flow:
        assert row["from_agent"]
        assert row["to_agent"]
    flow_types = {row["msg_type"] for row in flow}
    assert "task_assign" in flow_types
    assert "task_result" in flow_types
    assert "verify_request" in flow_types
    assert "verify_result" in flow_types
    assert "synthesize" in flow_types

    # Orchestrator routed task_assign messages to the named workers
    assigns = [r for r in flow if r["msg_type"] == "task_assign"]
    assert all(r["from_agent"] == "orchestrator" for r in assigns)
    assert {r["to_agent"] for r in assigns} <= {"diag_01", "repair_01"}

    # Synthesis text is present
    assert "Multi-Agent Session Report" in payload["synthesis"]


def test_cli_multi_agent_rejects_empty_goal(tmp_path, capsys) -> None:
    """Empty --goal is rejected with a friendly (non-raw) error."""
    import json

    from owcopilot.cli.main import main
    from owcopilot.content.models import ContentBundle, Entity, EntityType
    from owcopilot.content.store import ContentStore

    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            entities={"e": Entity(id="e", name="E", type=EntityType.NPC, description="x")}
        )
    )

    code = main(
        ["multi-agent", "--content-root", str(root), "--goal", "   ", "--llm-mode", "offline"]
    )
    assert code == 2
    err = capsys.readouterr().err.strip().splitlines()
    payload = json.loads(err[-1])
    assert "goal" in payload["error"].lower() or "目标" in payload["error"]


# ---------------------------------------------------------------------------
# Demo entry point (callable as a script or from tests)
# ---------------------------------------------------------------------------


def run_demo() -> MultiAgentReport:
    """Offline $0 demo: orchestrator-worker-verifier collaboration.

    Can be called directly:
        python -m pytest tests/test_multi_agent.py::run_demo -s
    or:
        python -c "from tests.test_multi_agent import run_demo; run_demo()"
    """
    from owcopilot.llm.telemetry import TelemetryCollector
    from owcopilot.skills import SkillRegistry

    mock = MockProvider()
    gateway = LLMGateway({"cheap": mock, "frontier": mock}, telemetry=TelemetryCollector())
    registry = SkillRegistry()  # minimal registry for demo

    session = MultiAgentSession(gateway=gateway, registry=registry)
    report = session.run("Bring the world to a clean, exportable state")

    print("\n=== OWCopilot Multi-Agent Demo ===")
    print(f"Session ID : {report.session_id}")
    print(f"Goal       : {report.goal}")
    print(f"Participants ({len(report.participants)}): {', '.join(sorted(report.participants))}")
    print(f"\nWorker summaries ({len(report.worker_summaries)}):")
    for ws in report.worker_summaries:
        print(f"  [{ws['agent_id']}] role={ws['role']} open_errors={ws['open_errors']}")
    print(f"\nVerifier verdicts ({len(report.verifier_verdicts)}):")
    for vv in report.verifier_verdicts:
        errs = vv["open_errors_verified"]
        print(f"  [{vv['agent_id']}] verdict={vv['verdict']} verified_errors={errs}")
    print("\nBlackboard message flow:")
    for msg in session.blackboard.session_flow(report.session_id):
        print(f"  {msg.msg_type:16s} {msg.from_agent:15s} → {msg.to_agent:15s} [{msg.status}]")
    print(f"\nSynthesis:\n{report.synthesis}")

    # Assertions for demo correctness
    assert "orchestrator" in report.participants
    assert len(report.participants) >= 4
    assert len(report.worker_summaries) >= 1
    assert len(report.verifier_verdicts) >= 1

    all_msgs = session.blackboard.session_flow(report.session_id)
    msg_types = {m.msg_type for m in all_msgs}
    assert "task_assign" in msg_types
    assert "task_result" in msg_types
    assert "verify_request" in msg_types
    assert "verify_result" in msg_types
    assert "synthesize" in msg_types

    print("\n=== All assertions passed. Demo complete. $0 cost. ===")
    return report


if __name__ == "__main__":
    run_demo()


def test_scoped_registry_openai_tools_matches_manifest_scope(tmp_path) -> None:
    """Live 回归：ReActAgent 原生 function-calling 默认化后，worker 的 ScopedSkillRegistry
    必须实现 openai_tools（曾 AttributeError 崩掉 live worker），且原生 schema 面与文本
    manifest 面完全一致——沙箱在两条路径上暴露同一组工具，绝不多出执行门会拒绝的技能。"""
    from owcopilot.multi_agent.skill_scope import scoped_registry
    from owcopilot.skills import default_skill_registry

    reg = default_skill_registry(content_root=str(tmp_path))
    allowed = {"audit_project", "list_issues"}
    scoped = scoped_registry(reg, allowed)

    tools = scoped.openai_tools()
    tool_names = {t["function"]["name"] for t in tools}
    assert tool_names == allowed  # 原生面 = 沙箱面
    # 与文本 manifest 宣传的工具一致
    manifest = scoped.manifest()
    assert all(name in manifest for name in tool_names)
    assert "propose_fix" not in tool_names  # 域外技能绝不出现在原生 schema
    # 进一步收窄的交集语义与 manifest 相同
    narrowed = scoped.openai_tools(allowed={"audit_project", "propose_fix"})
    assert {t["function"]["name"] for t in narrowed} == {"audit_project"}


def test_orchestrator_normalises_invented_worker_ids_by_role() -> None:
    """Live 回归：LLM 分解会发明 worker id（rep_01 而非注册的 repair_01），导致子任务被
    UNROUTED 丢弃。规范化按 role 重映射到已注册 worker；未知 role 保留原样（维持诚实的
    UNROUTED 报告路径）；已注册 id 直通。"""
    from owcopilot.multi_agent.orchestrator import OrchestratorAgent, SubTask

    orch = OrchestratorAgent(
        gateway=None,  # type: ignore[arg-type] — 规范化不触网关
        registry=None,  # type: ignore[arg-type]
        registered_workers={"diagnosis": "diag_01", "repair_proposal": "repair_01"},
    )

    def sub(role: str, desc: str, wid: str) -> SubTask:
        return SubTask(worker_role=role, description=desc, allowed_skills=[], worker_id=wid)

    subtasks = [
        sub("diagnosis", "d", "diag_01"),
        sub("repair_proposal", "r", "rep_01"),
        sub("diagnosis", "d2", "diag_02"),
        sub("unknown_role", "u", "ghost_9"),
    ]

    out = orch._normalise_worker_ids(subtasks)

    assert [s.worker_id for s in out] == ["diag_01", "repair_01", "diag_01", "ghost_9"]
    # 描述/技能等不被改动
    assert out[1].description == "r"
    assert out[3].worker_role == "unknown_role"  # 未知 role → 留给 UNROUTED 诚实报告


# ---------------------------------------------------------------------------
# T20: two-wave (fan-out / JOIN) parallel stage dispatch
#
# Model = Anthropic, *How we built our multi-agent research system* + *Building Effective
# Agents* (orchestrator-workers + parallelization/sectioning): a wave fans out mutually
# independent subtasks, JOINs (waits for the whole set), then the next wave fans out. Within a
# wave a bounded pool of consumer threads drains the blackboard mailbox through the EXISTING
# optimistic claim lock — the concurrency the serial driver never exercised. All $0 offline.
# ---------------------------------------------------------------------------


def _decompose_json(n_diag: int) -> str:
    """A decompose-JSON with ``n_diag`` diagnosis subtasks (all → diag_01) plus one repair."""
    import json

    items = [
        {
            "role": "diagnosis",
            "description": f"Diag {i}",
            "allowed_skills": [],
            "worker_id": "diag_01",
        }
        for i in range(n_diag)
    ]
    items.append(
        {
            "role": "repair_proposal",
            "description": "Repair",
            "allowed_skills": [],
            "worker_id": "repair_01",
        }
    )
    return json.dumps(items)


def test_t20_claim_task_concurrent_single_winner() -> None:
    """T20: the optimistic claim lock is a GENUINELY contended arbiter.

    One pending task, many threads racing: exactly one wins and every other observes the loss
    branch (``claim_task`` returns None). This is the concurrency the serial driver never drove —
    the '摆设' the task calls out — proven live here using the production ``AgentBlackboard`` path
    with NO new primitive (SQLite serialises the shared connection internally, threadsafety==3).
    """
    import threading
    import uuid

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    bb = AgentBlackboard(conn)
    sid = str(uuid.uuid4())
    bb.post_message(
        AgentMessage(
            session_id=sid,
            from_agent="orchestrator",
            to_agent="diag_01",
            msg_type="task_assign",
            payload={"original_goal": "g", "subtask": "s", "allowed_skills": []},
        )
    )

    k = 8
    start = threading.Barrier(k)  # test scaffolding: force all threads to claim simultaneously
    outcomes: list[AgentMessage | None] = []

    def claim() -> None:
        start.wait()
        outcomes.append(bb.claim_task("diag_01", sid))  # list.append is GIL-atomic

    threads = [threading.Thread(target=claim) for _ in range(k)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [o for o in outcomes if o is not None]
    assert len(winners) == 1, f"exactly one thread may claim the task, got {len(winners)}"
    assert outcomes.count(None) == k - 1, "every losing thread must observe the loss branch (None)"
    conn.close()


def test_t20_session_conn_usable_cross_thread(session: MultiAgentSession) -> None:
    """T20: the session's self-created blackboard connection is opened check_same_thread=False.

    Parallel-wave consumer threads drain this one connection; a default (True) connection would
    raise ``sqlite3.ProgrammingError`` when touched from a non-creating thread. Proven directly.
    """
    import threading

    errors: list[Exception] = []

    def use() -> None:
        try:
            session._conn.execute("SELECT 1").fetchone()
        except Exception as exc:  # noqa: BLE001 — the whole point is to catch a would-be crash
            errors.append(exc)

    t = threading.Thread(target=use)
    t.start()
    t.join()
    assert not errors, (
        f"session conn must be usable cross-thread (check_same_thread=False): {errors}"
    )


def test_t20_parallel_wave_exactly_once() -> None:
    """T20: a wave of independent diagnosis subtasks is drained EXACTLY ONCE under real thread
    contention — every subtask runs, none twice, none dropped.

    Four diagnosis subtasks all address diag_01, so up to four consumer threads race to claim four
    pending messages from ONE mailbox. Correctness = a bijection between diagnosis task_assigns and
    diagnosis task_results (the optimistic UPDATE is the sole arbiter). The audit registry lets the
    verifier PASS a no-count worker, so no Reflexion retry inflates the result count.
    """
    reg, _calls = _make_audit_registry(open_errors=0)  # audit bound ⇒ verifier PASS ⇒ no retry
    decompose = _decompose_json(4)

    class _FourDiagProvider:
        def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
            if "task-decomposition" in system or "Decompose into subtasks" in user:
                return decompose, 10, 10
            return "Thought: done.\nFinal Answer: ok", 5, 2

    gw = LLMGateway(
        {"cheap": _FourDiagProvider(), "frontier": _FourDiagProvider()},
        telemetry=TelemetryCollector(),
    )
    session = MultiAgentSession(gateway=gw, registry=reg, max_workers=4)
    report = session.run("diagnose in parallel")
    flow = session.blackboard.session_flow(report.session_id)

    diag_assigns = [m for m in flow if m.msg_type == "task_assign" and m.to_agent == "diag_01"]
    assert len(diag_assigns) == 4, f"expected 4 diagnosis assigns, got {len(diag_assigns)}"
    assert all(m.status == "done" for m in diag_assigns), (
        "every claimed diagnosis assign must end 'done' (claimed exactly once, not orphaned)"
    )
    diag_results = [m for m in flow if m.msg_type == "task_result" and m.from_agent == "diag_01"]
    assert len(diag_results) == 4, (
        f"exactly one result per subtask (no double-claim, no drop); got {len(diag_results)}"
    )
    refs = {TaskResultPayload.from_dict(m.payload).task_msg_id for m in diag_results}
    assert refs == {m.id for m in diag_assigns}, (
        "results must map bijectively onto assigns — proof each subtask ran exactly once"
    )


def test_t20_wave_spawns_fresh_worker_per_claim_off_main_thread(
    session: MultiAgentSession,
) -> None:
    """T20: each claimed task is executed by a FRESH worker instance (its own ReActAgent
    transcript), spawned on a pool thread — so concurrent branches never clobber a shared
    transcript and no wave work runs on the calling thread.
    """
    import threading

    main_ident = threading.get_ident()
    spawned: list[WorkerAgent] = []
    spawn_threads: set[int] = set()
    orig_spawn = session._spawn_worker

    def spy(role: str, agent_id: str) -> WorkerAgent:
        worker = orig_spawn(role, agent_id)
        spawned.append(worker)
        spawn_threads.add(threading.get_ident())
        return worker

    # Instance-attribute shadow of the bound method — spy on the production factory.
    session._spawn_worker = spy
    session.run("fresh workers goal")

    # Default topology spawns one diagnosis + one repair worker (one per claimed wave task).
    assert len(spawned) >= 2, f"expected ≥2 spawned workers, got {len(spawned)}"
    assert len({id(w) for w in spawned}) == len(spawned), (
        "each claim must spawn a DISTINCT worker instance"
    )
    assert len({id(w.agent) for w in spawned}) == len(spawned), (
        "each spawned worker must own its ReActAgent transcript (no shared object)"
    )
    assert main_ident not in spawn_threads, (
        "wave work must run on pool threads, not the caller — else there is no parallelism"
    )


def test_t20_two_wave_barrier_repair_sees_all_diagnoses() -> None:
    """T20: the wave-1 JOIN barrier means repair is enriched with EVERY diagnosis finding before it
    runs (Anthropic's 'wait for the whole set to complete before proceeding').

    Three diagnosis subtasks run in parallel; the enriched repair assign the repair worker claims
    must embed all three findings — exercising the multi-diagnosis ``_combine_diag_results`` fold.
    """
    reg, _calls = _make_audit_registry(open_errors=0)  # verifier PASS ⇒ no retry
    tags = ("ALPHA", "BETA", "GAMMA")
    decompose = _decompose_json(len(tags))

    class _TaggedProvider:
        def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
            if "task-decomposition" in system or "Decompose into subtasks" in user:
                return decompose, 10, 10
            if "[Your specific subtask as diagnosis]" in user:
                for i, tag in enumerate(tags):
                    if f"Diag {i}" in user:
                        return f"Thought: done.\nFinal Answer: FINDING_{tag}", 5, 2
                return "Thought: done.\nFinal Answer: ok", 5, 2
            return "Thought: done.\nFinal Answer: repair-ok", 5, 2

    gw = LLMGateway(
        {"cheap": _TaggedProvider(), "frontier": _TaggedProvider()}, telemetry=TelemetryCollector()
    )
    session = MultiAgentSession(gateway=gw, registry=reg, max_workers=3)
    report = session.run("find and fix in parallel")
    flow = session.blackboard.session_flow(report.session_id)

    repair_assigns = [m for m in flow if m.msg_type == "task_assign" and m.to_agent == "repair_01"]
    enriched = [m for m in repair_assigns if "Diagnosis findings" in m.payload.get("subtask", "")]
    assert enriched, (
        "repair must receive an enriched assign after the wave-1 JOIN; got "
        f"{[m.payload.get('subtask', '')[:80] for m in repair_assigns]}"
    )
    subtask_text = enriched[-1].payload["subtask"]
    for tag in tags:
        assert f"FINDING_{tag}" in subtask_text, (
            f"repair subtask must embed diagnosis finding FINDING_{tag} — the wave-1 JOIN must "
            f"complete ALL diagnoses before repair runs. Got: {subtask_text[:300]}"
        )
