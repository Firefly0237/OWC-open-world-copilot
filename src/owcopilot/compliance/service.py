"""Remediation case lifecycle on top of the deterministic theme sweep (WS-D).

Open cases from a sweep, drive them through a legal state machine with an audit trail, rescan to
confirm a fix (a violation that is NOT gone can never be signed off), and build a compliance report.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from ..assist.sweep import SweepReport, ThemeSweepService
from ..content.models import ContentBundle
from ..llm.cache import Embedder
from ..llm.gateway import LLMGateway
from .models import (
    MANUAL_TRANSITIONS,
    CaseEvent,
    CaseStatus,
    ComplianceReport,
    RemediationCase,
    RulePack,
)

_TERMINAL = {CaseStatus.SIGNED_OFF, CaseStatus.DISMISSED}
_CATEGORY = {
    "term": "敏感词命中",
    "semantic": "语义近似待查",
    "judge": "模型判定",
    "graph": "关系扩散",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _case_id(object_ref: str, rule_id: str) -> str:
    digest = hashlib.sha256(f"{object_ref}|{rule_id}".encode()).hexdigest()[:12]
    return f"case_{digest}"


def open_cases_from_sweep(
    report: SweepReport, existing: dict[str, RemediationCase]
) -> dict[str, RemediationCase]:
    """Merge a sweep's hits into the case ledger. New violations open as FLAGGED; a violation that
    reappears on an already SIGNED_OFF case reopens it (a regression must not stay closed)."""
    cases = dict(existing)
    for finding in report.hits:
        cid = _case_id(finding.ref, finding.layer)
        if cid not in cases:
            cases[cid] = RemediationCase(
                id=cid,
                object_ref=finding.ref,
                rule_id=finding.layer,
                category=_CATEGORY.get(finding.layer, "未分类"),
                evidence=finding.evidence,
            )
        elif cases[cid].status == CaseStatus.SIGNED_OFF:
            case = cases[cid]
            case.history.append(
                CaseEvent(
                    at=_now(),
                    operator="system",
                    from_status=case.status.value,
                    to_status=CaseStatus.FLAGGED.value,
                    note="违规复现，自动重新开案",
                )
            )
            case.status = CaseStatus.FLAGGED
            case.evidence = finding.evidence
    return cases


def transition(
    case: RemediationCase,
    to: CaseStatus,
    *,
    operator: str,
    note: str = "",
    assignee: str | None = None,
) -> RemediationCase:
    """Apply a human-driven status change, validating it is legal and recording the trail."""
    if not operator.strip():
        raise ValueError("请先填写署名")
    if to not in MANUAL_TRANSITIONS.get(case.status, set()):
        raise ValueError(f"不允许的流转：{case.status.value} → {to.value}")
    case.history.append(
        CaseEvent(
            at=_now(),
            operator=operator,
            from_status=case.status.value,
            to_status=to.value,
            note=note,
        )
    )
    case.status = to
    if to == CaseStatus.ASSIGNED and assignee is not None:
        case.assignee = assignee
    return case


def rescan_case(
    case: RemediationCase,
    bundle: ContentBundle,
    *,
    rule_pack: RulePack,
    operator: str,
    gateway: LLMGateway | None = None,
    embedder: Embedder | None = None,
    use_llm: bool = False,
) -> tuple[RemediationCase, bool]:
    """Re-run the sweep and check the case's object. Clean -> RESCAN_PASSED (ready to sign off);
    still flagged -> FIXING with refreshed evidence. Returns (case, still_flagged)."""
    if case.status not in {CaseStatus.FIXING, CaseStatus.RESCAN_PASSED}:
        raise ValueError(f"只有整改中的 case 可复扫，当前：{case.status.value}")
    report = _run_sweep(bundle, rule_pack, gateway=gateway, embedder=embedder, use_llm=use_llm)
    still = {f.ref for f in report.hits}
    hit = case.object_ref in still
    target = CaseStatus.FIXING if hit else CaseStatus.RESCAN_PASSED
    note = "复扫仍命中，打回整改" if hit else "复扫确认违规已消除，待签核"
    if hit:
        case.evidence = next(f.evidence for f in report.hits if f.ref == case.object_ref)
    case.history.append(
        CaseEvent(
            at=_now(),
            operator=operator,
            from_status=case.status.value,
            to_status=target.value,
            note=note,
        )
    )
    case.status = target
    return case, hit


def _run_sweep(
    bundle: ContentBundle,
    rule_pack: RulePack,
    *,
    gateway: LLMGateway | None,
    embedder: Embedder | None,
    use_llm: bool,
) -> SweepReport:
    service = ThemeSweepService(bundle=bundle, gateway=gateway, embedder=embedder)
    theme = rule_pack.terms[0] if rule_pack.terms else ""
    return service.sweep(
        theme,
        extra_terms=rule_pack.terms[1:],
        use_llm=use_llm,
        semantic_threshold=rule_pack.semantic_threshold,
    )


def build_compliance_report(
    cases: dict[str, RemediationCase], *, rule_pack_id: str
) -> ComplianceReport:
    by_status: dict[str, int] = {}
    for case in cases.values():
        by_status[case.status.value] = by_status.get(case.status.value, 0) + 1
    ordered = sorted(cases.values(), key=lambda c: (c.status.value, c.id))
    return ComplianceReport(
        rule_pack=rule_pack_id,
        generated_at=_now(),
        total=len(cases),
        by_status=by_status,
        open_unresolved=sum(1 for c in cases.values() if c.status not in _TERMINAL),
        signed_off=by_status.get(CaseStatus.SIGNED_OFF.value, 0),
        cases=ordered,
    )
