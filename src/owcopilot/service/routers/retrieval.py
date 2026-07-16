# 检索：混合上下文包 + 有据可查的 lore 问答（无据即拒答）。
# Context pack + cited lore QA.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from ...llm.gateway import LLMGateway
from ...llm.resilience import build_real_provider
from ...llm.router import StaticRouter
from ...llm.telemetry import TelemetryCollector
from ...qa.offline import OfflineQAProvider
from ...qa.service import LoreQAService
from ...storage import SQLiteStore
from ...telemetry import llm_step, summarize_workflow
from ..api import _require_offline_allowed, _require_real_allowed
from ..dependencies import (
    _context_builder_for_bundle,
    _deterministic_cost_budget,
    _open_project_context,
)
from ..schemas import (
    ProjectAskRequest,
    ProjectAskResponse,
    ProjectContextPackRequest,
    ProjectContextPackResponse,
)

router = APIRouter()


@router.post("/projects/{project}/context:pack", response_model=ProjectContextPackResponse)
def create_context_pack(
    project: str,
    req: ProjectContextPackRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ProjectContextPackResponse:
    request_id = str(uuid.uuid4())
    if req.content is None:
        project_context = _open_project_context(project)
        if project_context is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"project {project!r} is not registered; configure "
                    "OWCOPILOT_PROJECTS_JSON or send inline content"
                ),
            )
        try:
            pack = project_context.context_builder.build(
                req.query,
                budget_tokens=req.budget_tokens,
            )
            return ProjectContextPackResponse(
                request_id=request_id,
                project=project,
                refs=pack.refs,
                hits=[hit.model_dump(mode="json") for hit in pack.hits],
                cost_budget=_deterministic_cost_budget("build_context_pack"),
            )
        finally:
            project_context.close()

    store, builder = _context_builder_for_bundle(req.content)
    try:
        pack = builder.build(req.query, budget_tokens=req.budget_tokens)
        return ProjectContextPackResponse(
            request_id=request_id,
            project=project,
            refs=pack.refs,
            hits=[hit.model_dump(mode="json") for hit in pack.hits],
            cost_budget=_deterministic_cost_budget("build_context_pack"),
        )
    finally:
        store.close()


@router.post("/projects/{project}/ask", response_model=ProjectAskResponse)
def ask_project(
    project: str,
    req: ProjectAskRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ProjectAskResponse:
    request_id = str(uuid.uuid4())
    if req.faithfulness == "llm" and req.llm_mode != "real":
        # No silent downgrade: the llm judge would otherwise be the offline QA double (not an
        # entailment judge). Fail early — before opening any resource — with an actionable
        # message instead of a misleading score.
        raise HTTPException(
            status_code=400,
            detail=(
                "faithfulness='llm' requires llm_mode='real' (the answering model judges each "
                "claim); for offline use faithfulness='offline' (deterministic $0 judge)."
            ),
        )
    project_context = None
    store: SQLiteStore | None = None
    if req.content is None:
        project_context = _open_project_context(project)
        if project_context is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"project {project!r} is not registered; configure "
                    "OWCOPILOT_PROJECTS_JSON or send inline content"
                ),
            )
        builder = project_context.qa_context_builder()
        bundle = project_context.bundle
    else:
        store, builder = _context_builder_for_bundle(req.content)
        bundle = req.content

    telemetry = TelemetryCollector()
    if req.llm_mode == "real":
        _require_real_allowed(request)
        ask_provider: Any = build_real_provider(model=req.llm_model, timeout=60.0)
    else:
        _require_offline_allowed()
        ask_provider = OfflineQAProvider()
    gateway = LLMGateway(
        providers={"cheap": ask_provider},
        router=StaticRouter(mapping={"qa_answer": "cheap", "qa_expand": "cheap"}),
        # app-lifetime L1/L2 cache: repeated lore questions cost $0
        cache=request.app.state.service_cache,
        telemetry=telemetry,
        max_retries=1 if req.llm_mode == "real" else 0,
        retry_backoff_seconds=1.0 if req.llm_mode == "real" else 0.0,
        namespace=project,  # scope cached answers to this project (shared app-lifetime cache)
    )
    try:
        answer = LoreQAService(
            gateway=gateway,
            context_builder=builder,
            bundle=bundle,
            expand=(req.llm_mode == "real"),
            faithfulness=req.faithfulness,
        ).ask(req.query, budget_tokens=req.budget_tokens)
        telemetry_summary = telemetry.summary()
        cost_budget = summarize_workflow(
            [llm_step("ask_lore", telemetry_summary)],
            budget_usd=req.max_cost_usd,
        ).budget
        return ProjectAskResponse(
            request_id=request_id,
            project=project,
            answer=answer.model_dump(mode="json"),
            telemetry=telemetry_summary,
            cost_budget=cost_budget.model_dump(mode="json"),
        )
    finally:
        if project_context is not None:
            project_context.close()
        if store is not None:
            store.close()
