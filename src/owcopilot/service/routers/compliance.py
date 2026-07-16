# 合规：规则包扫描/报告/整改案流转与复扫。
# Compliance scan/report/case transition/rescan.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request

from ...app.actions import (
    compliance_report_action,
    rescan_case_action,
    run_compliance_scan_action,
    transition_case_action,
)
from ..dependencies import _manage_error, _project_root_or_404
from ..schemas import CaseRescanRequest, CaseTransitionRequest, ComplianceScanRequest

router = APIRouter()


@router.post("/projects/{project}/compliance:scan")
def compliance_scan(
    project: str,
    req: ComplianceScanRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Run the sweep with a rule pack and open/refresh remediation cases."""
    try:
        result = run_compliance_scan_action(
            _project_root_or_404(project),
            rule_pack=req.rule_pack,
            llm_mode=req.llm_mode,
            llm_model=req.llm_model or "deepseek-v4-flash",
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.get("/projects/{project}/compliance/report")
def compliance_report(
    project: str,
    request: Request,
    rule_pack_id: str = "default",
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    result = compliance_report_action(_project_root_or_404(project), rule_pack_id=rule_pack_id)
    return {"project": project, **result}


@router.post("/projects/{project}/compliance/cases/{case_id}:transition")
def compliance_transition(
    project: str,
    case_id: str,
    req: CaseTransitionRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        result = transition_case_action(
            _project_root_or_404(project),
            case_id=case_id,
            to=req.to,
            operator=req.operator,
            note=req.note,
            assignee=req.assignee,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.post("/projects/{project}/compliance/cases/{case_id}:rescan")
def compliance_rescan(
    project: str,
    case_id: str,
    req: CaseRescanRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        result = rescan_case_action(
            _project_root_or_404(project),
            case_id=case_id,
            operator=req.operator,
            rule_pack=req.rule_pack,
            llm_mode=req.llm_mode,
            llm_model=req.llm_model or "deepseek-v4-flash",
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}
