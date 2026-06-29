"""CASE 1 deep-dive: worker open_errors metric mismatch can produce FALSE FAIL/PASS."""
from __future__ import annotations

import sqlite3
import uuid

from owcopilot.core.skills import CostTier, SideEffect, Skill, SkillRegistry
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.telemetry import TelemetryCollector
from owcopilot.fakes import MockProvider
from owcopilot.multi_agent import (
    AgentBlackboard, AgentMessage, TaskResultPayload, VerifierAgent, VerifyResultPayload,
)
from owcopilot.multi_agent.workers import _count_errors_in_answer


def gw():
    m = MockProvider()
    return LLMGateway({"cheap": m, "frontier": m}, telemetry=TelemetryCollector())


def bb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return AgentBlackboard(c)


def make_reg(open_errors):
    reg = SkillRegistry()
    reg.register(Skill(name="audit_project", description="f", cost_tier=CostTier.DETERMINISTIC,
        side_effect=SideEffect.READ_ONLY, handler=lambda **k: {"open_errors": open_errors, "issues": []}))
    reg.register(Skill(name="list_issues", description="f", cost_tier=CostTier.DETERMINISTIC,
        side_effect=SideEffect.READ_ONLY, handler=lambda **k: {"count": 0}))
    return reg


def verify(reg, claimed, answer):
    board = bb(); sid = str(uuid.uuid4())
    tr = AgentMessage(session_id=sid, from_agent="diag_01", to_agent="orchestrator",
        msg_type="task_result",
        payload=TaskResultPayload(task_msg_id="x", worker_role="diagnosis", final_answer=answer,
            open_errors=claimed, stop_reason="finished", step_count=1).to_dict(), status="done")
    board.post_message(tr)
    vq = AgentMessage(session_id=sid, from_agent="orchestrator", to_agent="verifier_01",
        msg_type="verify_request", payload={"target_msg_id": tr.id})
    board.post_message(vq)
    v = VerifierAgent(agent_id="verifier_01", gateway=gw(), registry=reg)
    return VerifyResultPayload.from_dict(v.verify(vq, board).payload)


print("FALSE FAIL: honest worker, audit & worker AGREE on count, but worker's")
print("open_errors field (substring 'error' count) diverges from the true number.\n")

# Scenario: world truly has 2 open errors. An honest diag worker writes a thorough
# answer that uses the word 'error' many times (explaining each). The worker's
# auto-computed open_errors becomes the substring count, NOT 2.
answer = (
    "Audit found 2 open errors. The first error is a broken reference error; "
    "the second error is a timeline error. Both errors should be fixed. "
    "No other error categories triggered."
)
worker_field = _count_errors_in_answer(answer)
print(f"True open errors (audit) = 2")
print(f"Worker's honest narrative answer = {answer!r}")
print(f"Worker's auto-computed open_errors field = {worker_field}  (substring 'error' count)")
reg = make_reg(2)
vr = verify(reg, worker_field, answer)
print(f"--> VERDICT = {vr.verdict}")
print(f"    {vr.rationale}\n")
if vr.verdict == "fail":
    print(">>> FALSE FAIL: an honest worker is flagged as a liar purely because the")
    print(">>> worker's open_errors metric != the audit's open_errors metric.\n")

print("-" * 70)
print("FALSE PASS: worker LIES in prose but the substring count happens to land")
print("within +/-1 of the audit count.\n")
# audit=0 (clean world). Worker lies 'found tons of problems' but its prose uses
# 'error' once -> worker_field=1 -> delta(0,1)=1 -> pass. The lie about content
# is invisible because only the integer is compared, not the claim.
answer2 = "I found a catastrophic error that breaks the entire main quest chain."
wf2 = _count_errors_in_answer(answer2)
reg0 = make_reg(0)
vr2 = verify(reg0, wf2, answer2)
print(f"True open errors (audit) = 0 (clean)")
print(f"Worker LIE answer = {answer2!r}")
print(f"Worker open_errors field = {wf2}")
print(f"--> VERDICT = {vr2.verdict}  (delta={abs(0-wf2)})")
print(f"    {vr2.rationale}")
