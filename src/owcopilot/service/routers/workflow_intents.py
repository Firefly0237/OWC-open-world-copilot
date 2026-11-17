"""Explicit author intent confirmation and isolated candidate-review records, never canon apply."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...app.actions.intent_workflow import (
    IntentAuthorDecisionInput,
    IntentCandidateReviewInput,
    IntentConfirmInput,
    IntentContractInput,
    confirm_intent_contract_action,
    create_intent_candidate_review_action,
    create_intent_contract_action,
    decide_intent_candidate_action,
    get_intent_candidate_review_action,
    get_intent_contract_action,
    list_intent_contracts_action,
)
from ..dependencies import _project_root_or_404
from ..profile import require_local_capability
from .workflows import _call


def _local_studio(request: Request) -> None:
    require_local_capability(request, "creative_workflows")


router = APIRouter(prefix="/projects/{project}/creative", dependencies=[Depends(_local_studio)])


@router.post("/tasks/{task_id}/intent-contracts", status_code=201)
def propose_contract(project: str, task_id: str, req: IntentContractInput):
    return _call(
        create_intent_contract_action, _project_root_or_404(project), project, task_id, req
    )


@router.get("/tasks/{task_id}/intent-contracts")
def list_contracts(project: str, task_id: str):
    return _call(list_intent_contracts_action, _project_root_or_404(project), project, task_id)


@router.get("/intent-contracts/{contract_id}")
def get_contract(project: str, contract_id: str):
    return _call(get_intent_contract_action, _project_root_or_404(project), project, contract_id)


@router.post("/intent-contracts/{contract_id}:confirm", status_code=201)
def confirm_contract(project: str, contract_id: str, req: IntentConfirmInput):
    return _call(
        confirm_intent_contract_action, _project_root_or_404(project), project, contract_id, req
    )


@router.post("/intent-contracts/{contract_id}/candidate-reviews", status_code=201)
def review_candidate(project: str, contract_id: str, req: IntentCandidateReviewInput):
    return _call(
        create_intent_candidate_review_action,
        _project_root_or_404(project),
        project,
        contract_id,
        req,
    )


@router.get("/intent-reviews/{review_id}")
def get_review(project: str, review_id: str):
    return _call(
        get_intent_candidate_review_action, _project_root_or_404(project), project, review_id
    )


@router.post("/intent-reviews/{review_id}/author-decision", status_code=201)
def decide_candidate(project: str, review_id: str, req: IntentAuthorDecisionInput):
    return _call(
        decide_intent_candidate_action, _project_root_or_404(project), project, review_id, req
    )
