# AI 生成域：草拟/吼叫/抽取/对白/风味 + 矛盾/主题扫描，全部经审阅队列。
# AI drafting -> review queue.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from ...app.actions import (
    detect_contradictions_action,
    dismiss_sweep_finding_action,
    run_theme_sweep_action,
)
from ...assist.barks import BarkBatchService
from ...assist.critic import BarkCritic, DialogueCritic, FlavorCritic, QuestCritic
from ...assist.dialogue_trees import DialogueTreeService, OfflineDialogueTreeProvider
from ...assist.drafts import QuestDraftService
from ...assist.flavor import FlavorBatchService, OfflineFlavorProvider
from ...assist.offline import OfflineBarksProvider, OfflineQuestDraftProvider
from ...assist.review_queue import ReviewQueue
from ...audit.baseline import issue_fingerprint
from ...audit.context import AuditContext
from ...extraction import (
    ExtractionDraft,
    ExtractionService,
    OfflineExtractionProvider,
    apply_gap_answers,
    quests_from_beats,
)
from ...telemetry import llm_step, summarize_workflow
from ..api import _require_real_allowed, _task_gateway
from ..dependencies import _project_root_or_404, _registered_project
from ..schemas import (
    ContradictionScanRequest,
    ProjectBarksRequest,
    ProjectBarksResponse,
    ProjectDialogueTreeRequest,
    ProjectDialogueTreeResponse,
    ProjectDraftRequest,
    ProjectDraftResponse,
    ProjectExtractionRunRequest,
    ProjectExtractionRunResponse,
    ProjectExtractionSubmitRequest,
    ProjectExtractionSubmitResponse,
    ProjectFlavorRequest,
    ProjectFlavorResponse,
    ProjectSweepRequest,
    ProjectSweepResponse,
    SweepDismissRequest,
)

router = APIRouter()


@router.post("/projects/{project}/contents/quests:draft", response_model=ProjectDraftResponse)
def draft_project_quest(
    project: str,
    req: ProjectDraftRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ProjectDraftResponse:
    request_id = str(uuid.uuid4())
    project_context = _registered_project(project)
    try:
        gateway, telemetry = _task_gateway(
            req,
            request=request,
            task="quest_draft",
            offline_provider=OfflineQuestDraftProvider(),
            namespace=project,
        )
        assert gateway is not None
        critic = QuestCritic(gateway=gateway) if req.refine_rounds > 0 else None
        result = QuestDraftService(
            gateway=gateway,
            context_builder=project_context.context_builder,
            audit_runner=project_context.audit_runner,
            bundle=project_context.bundle,
            critic=critic,
            max_refine_rounds=req.refine_rounds,
        ).draft_quest(req.brief, budget_tokens=req.budget_tokens)
        queue = ReviewQueue(project_context.sqlite_store)
        item = queue.add_quest_draft(
            result.quest.model_dump(mode="json", exclude_none=True),
            issue_refs=[issue_fingerprint(issue) for issue in result.issues],
        )
        telemetry_summary = telemetry.summary()
        return ProjectDraftResponse(
            request_id=request_id,
            project=project,
            quest=result.quest.model_dump(mode="json", exclude_none=True),
            issues=[issue.model_dump(mode="json") for issue in result.issues],
            refine_trail=[r.model_dump(mode="json") for r in result.refine_trail],
            auto_review_incomplete=result.auto_review_incomplete,
            review_item_id=item.id,
            telemetry=telemetry_summary,
            cost_budget=summarize_workflow(
                [llm_step("quest_draft", telemetry_summary)],
                budget_usd=req.max_cost_usd,
            ).budget.model_dump(mode="json"),
        )
    finally:
        project_context.close()


@router.post("/projects/{project}/assist/barks:batch", response_model=ProjectBarksResponse)
def batch_project_barks(
    project: str,
    req: ProjectBarksRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ProjectBarksResponse:
    request_id = str(uuid.uuid4())
    project_context = _registered_project(project)
    try:
        unknown = [
            speaker for speaker in req.speaker_ids if speaker not in project_context.bundle.entities
        ]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"unknown speaker entities: {', '.join(unknown)}",
            )
        gateway, telemetry = _task_gateway(
            req,
            request=request,
            task="barks_batch",
            offline_provider=OfflineBarksProvider(),
            namespace=project,
        )
        assert gateway is not None
        allowed = set(req.speaker_ids) | set(req.allowed_entity_ids)
        result = BarkBatchService(
            gateway=gateway,
            bundle=project_context.bundle,
            review_queue=ReviewQueue(project_context.sqlite_store),
            critic=BarkCritic(gateway=gateway) if req.refine_rounds > 0 else None,
            max_refine_rounds=req.refine_rounds,
        ).generate(
            speaker_ids=req.speaker_ids,
            topic=req.topic,
            variants_per_speaker=req.variants_per_speaker,
            max_chars=req.max_chars,
            allowed_entity_ids=allowed,
        )
        telemetry_summary = telemetry.summary()
        return ProjectBarksResponse(
            request_id=request_id,
            project=project,
            accepted=[
                {"speaker_id": variant.speaker_id, "text": variant.text}
                for variant in result.accepted
            ],
            rejected=[
                {
                    "speaker_id": rejected.speaker_id,
                    "text": rejected.text,
                    "issues": [issue.model_dump(mode="json") for issue in rejected.issues],
                }
                for rejected in result.rejected
            ],
            review_item_ids=[item.id for item in result.review_items],
            telemetry=telemetry_summary,
            cost_budget=summarize_workflow(
                [llm_step("barks_batch", telemetry_summary)],
                budget_usd=req.max_cost_usd,
            ).budget.model_dump(mode="json"),
        )
    finally:
        project_context.close()


@router.post("/projects/{project}/extractions:run", response_model=ProjectExtractionRunResponse)
def run_project_extraction(
    project: str,
    req: ProjectExtractionRunRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ProjectExtractionRunResponse:
    request_id = str(uuid.uuid4())
    project_context = _registered_project(project)
    try:
        gateway, telemetry = _task_gateway(
            req,
            request=request,
            task="extract_lore",
            offline_provider=OfflineExtractionProvider(),
            namespace=project,
            extra_tasks=["verify_faithfulness"],
        )
        assert gateway is not None
        draft = ExtractionService(gateway=gateway, bundle=project_context.bundle).extract(
            title=req.title,
            text=req.text,
            source_kind=req.source_kind,
            glean_rounds=req.glean_rounds,
            verify_faithfulness=req.verify_faithfulness and req.llm_mode == "real",
        )
        telemetry_summary = telemetry.summary()
        return ProjectExtractionRunResponse(
            request_id=request_id,
            project=project,
            draft=draft.model_dump(mode="json", exclude_none=True),
            stats=draft.stats,
            telemetry=telemetry_summary,
            cost_budget=summarize_workflow(
                [llm_step("extract_lore", telemetry_summary)],
                budget_usd=req.max_cost_usd,
            ).budget.model_dump(mode="json"),
        )
    finally:
        project_context.close()


@router.post(
    "/projects/{project}/extractions:submit",
    response_model=ProjectExtractionSubmitResponse,
)
def submit_project_extraction(
    project: str,
    req: ProjectExtractionSubmitRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ProjectExtractionSubmitResponse:
    request_id = str(uuid.uuid4())
    try:
        parsed = ExtractionDraft.model_validate(req.draft)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"invalid extraction draft: {e}") from e
    if req.answers:
        parsed = apply_gap_answers(parsed, req.answers)
    if req.include_beats_as_quests:
        parsed.bundle.quests.update(quests_from_beats(parsed))
    project_context = _registered_project(project)
    try:
        issues = project_context.audit_runner.run(AuditContext.from_bundle(parsed.bundle)).issues
        item = ReviewQueue(project_context.sqlite_store).add_import_draft(
            {
                "id": parsed.id,
                "source_title": parsed.source_title,
                "source_kind": parsed.source_kind,
                "summary": parsed.summary,
                "bundle": parsed.bundle.model_dump(mode="json", exclude_none=True),
                "plot_beats": [b.model_dump(mode="json") for b in parsed.plot_beats],
                "open_gaps": [g.model_dump(mode="json") for g in parsed.gaps],
            },
            issue_refs=[issue_fingerprint(issue) for issue in issues],
        )
        return ProjectExtractionSubmitResponse(
            request_id=request_id,
            project=project,
            review_item_id=item.id,
            open_gaps=len(parsed.gaps),
            issues=[issue.model_dump(mode="json") for issue in issues],
        )
    finally:
        project_context.close()


@router.post(
    "/projects/{project}/assist/dialogue_trees:draft",
    response_model=ProjectDialogueTreeResponse,
)
def draft_project_dialogue_tree(
    project: str,
    req: ProjectDialogueTreeRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ProjectDialogueTreeResponse:
    request_id = str(uuid.uuid4())
    project_context = _registered_project(project)
    try:
        unknown = [pid for pid in req.participant_ids if pid not in project_context.bundle.entities]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"unknown participant entities: {', '.join(unknown)}",
            )
        gateway, telemetry = _task_gateway(
            req,
            request=request,
            task="dialogue_tree",
            offline_provider=OfflineDialogueTreeProvider(),
            namespace=project,
        )
        assert gateway is not None
        critic = DialogueCritic(gateway=gateway) if req.refine_rounds > 0 else None
        result = DialogueTreeService(
            gateway=gateway,
            bundle=project_context.bundle,
            review_queue=ReviewQueue(project_context.sqlite_store),
            critic=critic,
            max_refine_rounds=req.refine_rounds,
        ).generate(
            participant_ids=req.participant_ids,
            brief=req.brief,
            quest_id=req.quest_id,
            max_nodes=req.max_nodes,
            max_chars=req.max_chars,
        )
        telemetry_summary = telemetry.summary()
        return ProjectDialogueTreeResponse(
            request_id=request_id,
            project=project,
            tree=result.tree.model_dump(mode="json", exclude_none=True),
            lint_issues=[i.model_dump(mode="json") for i in result.lint_issues],
            structure_problems=result.structure_problems,
            refine_trail=[r.model_dump(mode="json") for r in result.refine_trail],
            auto_review_incomplete=result.auto_review_incomplete,
            review_item_id=result.review_item.id if result.review_item else None,
            telemetry=telemetry_summary,
            cost_budget=summarize_workflow(
                [llm_step("dialogue_tree", telemetry_summary)],
                budget_usd=req.max_cost_usd,
            ).budget.model_dump(mode="json"),
        )
    finally:
        project_context.close()


@router.post("/projects/{project}/assist/flavor:batch", response_model=ProjectFlavorResponse)
def batch_project_flavor(
    project: str,
    req: ProjectFlavorRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ProjectFlavorResponse:
    request_id = str(uuid.uuid4())
    project_context = _registered_project(project)
    try:
        gateway, telemetry = _task_gateway(
            req,
            request=request,
            task="flavor_batch",
            offline_provider=OfflineFlavorProvider(),
            namespace=project,
        )
        assert gateway is not None
        result = FlavorBatchService(
            gateway=gateway,
            bundle=project_context.bundle,
            review_queue=ReviewQueue(project_context.sqlite_store),
            critic=FlavorCritic(gateway=gateway) if req.refine_rounds > 0 else None,
            max_refine_rounds=req.refine_rounds,
        ).generate(
            category=req.category,
            names=req.names,
            theme=req.theme,
            max_chars=req.max_chars,
        )
        telemetry_summary = telemetry.summary()
        return ProjectFlavorResponse(
            request_id=request_id,
            project=project,
            batch_id=result.batch_id,
            accepted=[e.model_dump(mode="json") for e in result.accepted],
            rejected=[
                {"name": r.name, "text": r.text, "issues": [i.code for i in r.issues]}
                for r in result.rejected
            ],
            review_item_id=result.review_item.id if result.review_item else None,
            telemetry=telemetry_summary,
            cost_budget=summarize_workflow(
                [llm_step("flavor_batch", telemetry_summary)],
                budget_usd=req.max_cost_usd,
            ).budget.model_dump(mode="json"),
        )
    finally:
        project_context.close()


# ---- round-13 platform endpoints: the standardized surface any client (the Vue
# frontend, studio pipelines, CI) integrates against. Same gates as everything else:
# optional API key, rate limit, real mode fail-closed.


@router.post("/projects/{project}/contradictions:scan")
def scan_contradictions(
    project: str,
    req: ContradictionScanRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Batch-2: find semantic contradictions in canon (judge confirms, else queued)."""
    if req.use_llm and req.llm_mode == "real":
        _require_real_allowed(request)
    result = detect_contradictions_action(
        _project_root_or_404(project),
        use_llm=req.use_llm,
        semantic_threshold=req.semantic_threshold,
        max_judge=req.max_judge,
        llm_mode=req.llm_mode,
        llm_model=req.llm_model or "deepseek-v4-flash",
    )
    return {"project": project, **result}


@router.post("/projects/{project}/sweeps:run", response_model=ProjectSweepResponse)
def run_theme_sweep(
    project: str,
    req: ProjectSweepRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ProjectSweepResponse:
    if req.use_llm and req.llm_mode == "real":
        _require_real_allowed(request)
    result = run_theme_sweep_action(
        _project_root_or_404(project),
        theme=req.theme,
        extra_terms=req.extra_terms,
        use_llm=req.use_llm,
        llm_mode=req.llm_mode,
        llm_model=req.llm_model,
        max_judge=req.max_judge,
    )
    return ProjectSweepResponse(
        project=project, **{k: v for k, v in result.items() if k != "terms"}
    )


@router.post("/projects/{project}/sweeps:dismiss")
def dismiss_sweep_finding(
    project: str,
    req: SweepDismissRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """逐一排除 one sweep finding (signed, persisted); the same theme's next sweep suppresses it."""
    try:
        result = dismiss_sweep_finding_action(
            _project_root_or_404(project),
            theme=req.theme,
            finding_id=req.finding_id,
            ref=req.ref,
            locator=req.locator,
            layer=req.layer,
            operator=req.operator,
            reason=req.reason,
        )
    except ValueError as e:  # empty signature / missing 排除码 → client error, not a 500
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {"project": project, **result}
