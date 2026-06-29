"""Adversarial reproductions for R4 multi-agent verifier audit."""
from __future__ import annotations

import sqlite3
import uuid

from owcopilot.core.skills import CostTier, SideEffect, Skill, SkillParameter, SkillRegistry
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.telemetry import TelemetryCollector
from owcopilot.fakes import MockProvider
from owcopilot.multi_agent import (
    AgentBlackboard,
    AgentMessage,
    TaskResultPayload,
    VerifierAgent,
    VerifyResultPayload,
)


def gw():
    m = MockProvider()
    return LLMGateway({"cheap": m, "frontier": m}, telemetry=TelemetryCollector())


def bb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return AgentBlackboard(c)


def make_audit_registry(audit_return):
    """audit_return is a dict (the whole audit_project output)."""
    reg = SkillRegistry()
    reg.register(Skill(
        name="audit_project", description="fake", cost_tier=CostTier.DETERMINISTIC,
        side_effect=SideEffect.READ_ONLY, handler=lambda **k: dict(audit_return),
    ))
    reg.register(Skill(
        name="list_issues", description="fake", cost_tier=CostTier.DETERMINISTIC,
        side_effect=SideEffect.READ_ONLY, handler=lambda **k: {"count": 0, "issues": []},
    ))
    return reg


def post_tr(board, sid, *, claimed_errors, final_answer):
    tr = AgentMessage(
        session_id=sid, from_agent="diag_01", to_agent="orchestrator",
        msg_type="task_result",
        payload=TaskResultPayload(
            task_msg_id="x", worker_role="diagnosis", final_answer=final_answer,
            open_errors=claimed_errors, stop_reason="finished", step_count=1,
        ).to_dict(), status="done",
    )
    board.post_message(tr)
    vq = AgentMessage(
        session_id=sid, from_agent="orchestrator", to_agent="verifier_01",
        msg_type="verify_request", payload={"target_msg_id": tr.id},
    )
    board.post_message(vq)
    return vq


def run_verify(reg, *, claimed_errors, final_answer):
    board = bb()
    sid = str(uuid.uuid4())
    vq = post_tr(board, sid, claimed_errors=claimed_errors, final_answer=final_answer)
    v = VerifierAgent(agent_id="verifier_01", gateway=gw(), registry=reg)
    rmsg = v.verify(vq, board)
    return VerifyResultPayload.from_dict(rmsg.payload)


print("=" * 70)
print("CASE 1: worker open_errors semantics — substring 'error' count vs audit")
print("=" * 70)
# Worker's open_errors is _count_errors_in_answer = answer.lower().count('error').
# Simulate a HONEST worker who found 3 real errors, audit also says 3.
# But the worker's answer naturally contains the word 'error' a different # of times.
from owcopilot.multi_agent.workers import _count_errors_in_answer
honest_answer = "Audit complete. Found 3 open errors: broken-ref, timeline-error, and a localization error."
print(f"honest worker answer: {honest_answer!r}")
print(f"  worker open_errors (_count_errors_in_answer) = {_count_errors_in_answer(honest_answer)}")
print("  ^ counts the substring 'error' (errors, timeline-error, error) = mismatched with true count 3")

reg3 = make_audit_registry({"open_errors": 3, "issues": []})
vr = run_verify(reg3, claimed_errors=_count_errors_in_answer(honest_answer), final_answer=honest_answer)
print(f"  audit open_errors=3, worker_claimed={_count_errors_in_answer(honest_answer)}")
print(f"  VERDICT={vr.verdict} delta-from-rationale: {vr.rationale}")

print()
print("=" * 70)
print("CASE 2: delta<=1 tolerance — does it let a liar through?")
print("=" * 70)
# audit says 3, worker claims 2 (under by 1) -> delta=1 -> PASS (lie slips)
reg = make_audit_registry({"open_errors": 3, "issues": []})
vr = run_verify(reg, claimed_errors=2, final_answer="found 2 errors")
print(f"audit=3, worker_claimed=2, verdict={vr.verdict}  (delta=1 -> tolerated)")
vr = run_verify(reg, claimed_errors=4, final_answer="found 4 errors")
print(f"audit=3, worker_claimed=4 (over-report), verdict={vr.verdict}  (delta=1 -> tolerated)")

print()
print("=" * 70)
print("CASE 3: malformed audit outputs (defensive checks)")
print("=" * 70)
for label, out in [
    ("missing open_errors key", {"issues": []}),
    ("open_errors=None", {"open_errors": None}),
    ("open_errors=bool True", {"open_errors": True}),
    ("open_errors=float 3.0", {"open_errors": 3.0}),
    ("open_errors=str '3'", {"open_errors": "3"}),
    ("open_errors=-5 (negative)", {"open_errors": -5}),
]:
    reg = make_audit_registry(out)
    vr = run_verify(reg, claimed_errors=3, final_answer="一致性审计发现 3 个待修复错误")
    print(f"{label:35s} -> verdict={vr.verdict:10s} verified={vr.open_errors_verified} src_in_rationale={'deterministic-audit' in vr.rationale}")

print()
print("=" * 70)
print("CASE 4: audit tool raises / hangs")
print("=" * 70)
reg = SkillRegistry()
reg.register(Skill(name="audit_project", description="boom", cost_tier=CostTier.DETERMINISTIC,
    side_effect=SideEffect.READ_ONLY, handler=lambda **k: (_ for _ in ()).throw(RuntimeError("audit blew up"))))
reg.register(Skill(name="list_issues", description="x", cost_tier=CostTier.DETERMINISTIC,
    side_effect=SideEffect.READ_ONLY, handler=lambda **k: {"count": 0}))
vr = run_verify(reg, claimed_errors=2, final_answer="一致性审计发现 2 个待修复错误")
print(f"audit raises RuntimeError -> falls back to LLM-answer path; verdict={vr.verdict}")
print(f"  rationale: {vr.rationale[:120]}")
