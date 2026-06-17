"""WS-D · 版号合规整改工作流：开案 → 状态机留痕 → 复扫闭环 → 签核 → 报告。"""

from __future__ import annotations

import pytest

from owcopilot.app.actions import (
    compliance_report_action,
    rescan_case_action,
    run_compliance_scan_action,
    transition_case_action,
)
from owcopilot.assist.sweep import SweepFinding, SweepReport
from owcopilot.compliance.models import CaseStatus, RemediationCase
from owcopilot.compliance.service import (
    build_compliance_report,
    open_cases_from_sweep,
    transition,
)
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.store import ContentStore


def _report(*findings: SweepFinding) -> SweepReport:
    return SweepReport(
        theme="赌博",
        terms=["赌博"],
        scanned_total=len(findings),
        scanned_by_kind={},
        findings=list(findings),
        llm_used=False,
        judged_count=0,
        judge_skipped=0,
    )


def _hit(ref: str) -> SweepFinding:
    return SweepFinding(
        ref=ref,
        name=ref,
        object_kind="entity",
        layer="term",
        evidence="命中词「赌博」",
        verdict="hit",
    )


# --------------------------------------------------------------- open + state machine
def test_open_cases_from_sweep_and_dedupe() -> None:
    cases = open_cases_from_sweep(_report(_hit("entity:a"), _hit("entity:b")), {})
    assert len(cases) == 2
    again = open_cases_from_sweep(_report(_hit("entity:a")), cases)  # idempotent
    assert len(again) == 2


def test_signed_off_case_reopens_on_regression() -> None:
    cases = open_cases_from_sweep(_report(_hit("entity:a")), {})
    cid = next(iter(cases))
    cases[cid].status = CaseStatus.SIGNED_OFF
    reopened = open_cases_from_sweep(_report(_hit("entity:a")), cases)
    assert reopened[cid].status == CaseStatus.FLAGGED  # regression reopened, not silently closed


def test_transition_legal_and_illegal() -> None:
    case = RemediationCase(
        id="c", object_ref="entity:a", rule_id="term", category="x", evidence="e"
    )
    transition(case, CaseStatus.ASSIGNED, operator="ed", assignee="alice")
    assert case.status == CaseStatus.ASSIGNED and case.assignee == "alice"
    assert len(case.history) == 1 and case.history[0].operator == "ed"
    # cannot jump straight to signed off (must rescan first) — the "未消除不可签核" guard
    with pytest.raises(ValueError, match="不允许的流转"):
        transition(case, CaseStatus.SIGNED_OFF, operator="ed")


def test_transition_requires_signature() -> None:
    case = RemediationCase(
        id="c", object_ref="entity:a", rule_id="term", category="x", evidence="e"
    )
    with pytest.raises(ValueError, match="署名"):
        transition(case, CaseStatus.ASSIGNED, operator="  ")


def test_report_aggregates_by_status() -> None:
    cases = open_cases_from_sweep(_report(_hit("entity:a"), _hit("entity:b")), {})
    report = build_compliance_report(cases, rule_pack_id="default")
    assert report.total == 2 and report.open_unresolved == 2 and report.signed_off == 0


# ----------------------------------------------------------- action-level lifecycle (real sweep)
def _world(root, *, dirty: bool) -> None:
    desc = "他在巷子里组织赌博" if dirty else "他在巷子里巡逻"
    ContentStore(root).save(
        ContentBundle(
            entities={
                "npc_x": Entity(id="npc_x", name="老周", type=EntityType.NPC, description=desc)
            }
        )
    )


def test_full_lifecycle_scan_assign_fix_rescan_signoff(tmp_path) -> None:
    root = tmp_path / "content"
    _world(root, dirty=True)

    scan = run_compliance_scan_action(root)
    assert scan["hits"] >= 1
    cid = scan["report"]["cases"][0]["id"]

    transition_case_action(root, case_id=cid, to="assigned", operator="ed", assignee="alice")
    transition_case_action(root, case_id=cid, to="fixing", operator="alice")

    # rescan WITHOUT fixing -> still flagged, pushed back to fixing, NOT sign-offable
    still = rescan_case_action(root, case_id=cid, operator="alice")
    assert still["still_flagged"] is True
    with pytest.raises(ValueError, match="不允许的流转"):
        transition_case_action(root, case_id=cid, to="signed_off", operator="ed")

    # actually fix the canon, then rescan -> clean -> RESCAN_PASSED -> sign off
    _world(root, dirty=False)
    passed = rescan_case_action(root, case_id=cid, operator="alice")
    assert passed["still_flagged"] is False
    assert passed["case"]["status"] == "rescan_passed"
    signed = transition_case_action(
        root, case_id=cid, to="signed_off", operator="ed", note="复核通过"
    )
    assert signed["case"]["status"] == "signed_off"

    report = compliance_report_action(root)["report"]
    assert report["signed_off"] == 1 and report["open_unresolved"] == 0
    # the full trail is recorded (assign→fix→rescan→rescan→signoff)
    assert len(signed["case"]["history"]) >= 5
