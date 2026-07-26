# 合规：规则包扫描/报告/整改案流转与复扫。
# Compliance: scan, report, case transition, rescan.

"""compliance actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .._common import deterministic_cost_budget as _deterministic_cost_budget
from .._common import open_project as _project
from ._shared import _gateway


# >>> WS-D compliance remediation workflow >>>
def _rule_pack(rule_pack: dict[str, Any] | None) -> Any:
    from ...compliance.models import DEFAULT_RULE_PACK, RulePack

    return RulePack.model_validate(rule_pack) if rule_pack else DEFAULT_RULE_PACK


def run_compliance_scan_action(
    content_root: str | Path,
    *,
    rule_pack: dict[str, Any] | None = None,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Run the sweep with a rule pack, open/refresh remediation cases, return the report."""
    from ...assist.sweep import OfflineSweepJudgeProvider, ThemeSweepService
    from ...compliance.service import build_compliance_report, open_cases_from_sweep
    from ...compliance.store import CaseStore

    pack = _rule_pack(rule_pack)
    if not pack.terms:
        raise ValueError("规则包没有任何词条，无法清查")
    use_llm = llm_mode == "real"
    with _project(content_root, sqlite_path) as project:
        gateway = None
        if use_llm:
            gateway, _telemetry = _gateway(
                task="theme_sweep",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=OfflineSweepJudgeProvider(),
            )
        report = ThemeSweepService(
            bundle=project.bundle, gateway=gateway, embedder=project.embedder
        ).sweep(
            pack.terms[0],
            extra_terms=pack.terms[1:],
            use_llm=use_llm,
            semantic_threshold=pack.semantic_threshold,
        )
        store = CaseStore(project.content_store.root)
        cases = open_cases_from_sweep(report, store.load())
        store.save(cases)
        return {
            "scanned_total": report.scanned_total,
            "hits": len(report.hits),
            "report": build_compliance_report(cases, rule_pack_id=pack.id).model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("compliance_scan"),
        }


def compliance_report_action(
    content_root: str | Path,
    *,
    rule_pack_id: str = "default",
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ...compliance.service import build_compliance_report
    from ...compliance.store import CaseStore

    with _project(content_root, sqlite_path) as project:
        cases = CaseStore(project.content_store.root).load()
        return {
            "report": build_compliance_report(cases, rule_pack_id=rule_pack_id).model_dump(
                mode="json"
            ),
            "cost_budget": _deterministic_cost_budget("compliance_report"),
        }


def transition_case_action(
    content_root: str | Path,
    *,
    case_id: str,
    to: str,
    operator: str,
    note: str = "",
    assignee: str | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ...compliance.models import CaseStatus
    from ...compliance.service import transition
    from ...compliance.store import CaseStore

    with _project(content_root, sqlite_path) as project:
        store = CaseStore(project.content_store.root)
        cases = store.load()
        case = cases.get(case_id)
        if case is None:
            raise ValueError(f"整改案件不存在：{case_id}")
        transition(case, CaseStatus(to), operator=operator, note=note, assignee=assignee)
        store.save(cases)
        return {
            "case": case.model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("case_transition"),
        }


def rescan_case_action(
    content_root: str | Path,
    *,
    case_id: str,
    operator: str,
    rule_pack: dict[str, Any] | None = None,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ...assist.sweep import OfflineSweepJudgeProvider
    from ...compliance.service import rescan_case
    from ...compliance.store import CaseStore

    pack = _rule_pack(rule_pack)
    use_llm = llm_mode == "real"
    with _project(content_root, sqlite_path) as project:
        store = CaseStore(project.content_store.root)
        cases = store.load()
        case = cases.get(case_id)
        if case is None:
            raise ValueError(f"整改案件不存在：{case_id}")
        gateway = None
        if use_llm:
            gateway, _telemetry = _gateway(
                task="theme_sweep",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=OfflineSweepJudgeProvider(),
            )
        _case, still = rescan_case(
            case,
            project.bundle,
            rule_pack=pack,
            operator=operator,
            gateway=gateway,
            embedder=project.embedder,
            use_llm=use_llm,
        )
        store.save(cases)
        return {
            "case": case.model_dump(mode="json"),
            "still_flagged": still,
            "cost_budget": _deterministic_cost_budget("case_rescan"),
        }
