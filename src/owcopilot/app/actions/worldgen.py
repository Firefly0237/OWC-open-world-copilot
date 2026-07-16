# 世界生成：从零播种世界 + 在既有世界上扩写一批内容。
# World seeding + grounded expansion onto an existing world.

"""worldgen actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...assist.calibration import critic_from_trail, primary_dim_from_trail
from ...assist.review_queue import ReviewQueue
from ...audit.baseline import issue_fingerprint
from ...audit.context import AuditContext
from ...telemetry import llm_step, summarize_workflow
from ...worldgen import (
    LLMPremiseEvaluator,
    OfflineWorldExpandProvider,
    OfflineWorldSeedProvider,
    WorldExpandBrief,
    WorldExpandService,
    WorldQuestCritic,
    WorldSeedBrief,
    WorldSeedService,
)
from .._common import open_project as _project
from ._shared import _bundle_counts, _gateway, _merge_preview


def run_world_seed_action(
    content_root: str | Path,
    *,
    brief: dict[str, Any],
    sqlite_path: str | None = None,
    budget_tokens: int = 1800,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
    refine_rounds: int = 1,
    premise_candidates: int = 1,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    parsed = WorldSeedBrief.model_validate(brief)
    with _project(content_root, sqlite_path) as project:
        gateway, telemetry = _gateway(
            task="world_seed",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineWorldSeedProvider(),
        )
        # The quests-stage critique→refine loop is ON by default (1 round): the capstone stage is
        # where coherence is won or lost, so the agent raises its own quality before the human
        # review queue rather than leaning on the reviewer to fix a thin first draft.
        critic = WorldQuestCritic(gateway=gateway) if refine_rounds > 0 else None
        # Premise ToT is opt-in (>1 candidate: explore N dramatic spines, keep the best). The LLM
        # evaluator discriminates among complete spines where the deterministic score saturates —
        # real mode only: the offline double has no quality judgement, so offline ToT honestly
        # runs on the deterministic ``score_premise`` value function instead.
        premise_evaluator = (
            LLMPremiseEvaluator(gateway=gateway)
            if premise_candidates > 1 and llm_mode == "real"
            else None
        )
        draft = WorldSeedService(
            gateway=gateway,
            bundle=project.bundle,
            project_context_builder=project.context_builder,
            reference_context_builder=project.reference_context_builder,
            critic=critic,
            max_refine_rounds=refine_rounds,
            premise_candidates=premise_candidates,
            premise_evaluator=premise_evaluator,
        ).generate(parsed, budget_tokens=budget_tokens, progress=progress)
        issues = project.audit_runner.run(AuditContext.from_bundle(draft.bundle)).issues
        _trail_dump = [r.model_dump(mode="json") for r in draft.refine_trail]
        critic_verdict, critic_score = critic_from_trail(_trail_dump)
        critic_primary_dim = primary_dim_from_trail(_trail_dump)
        item = ReviewQueue(project.sqlite_store).add_world_seed(
            {
                "id": draft.id,
                "brief": draft.brief.model_dump(mode="json"),
                "summary": draft.summary,
                "bundle": draft.bundle.model_dump(mode="json", exclude_none=True),
                "reference_report": [row.model_dump(mode="json") for row in draft.reference_report],
                "project_context_refs": draft.project_context_refs,
                "inspiration_context_refs": draft.inspiration_context_refs,
                "refine_trail": _trail_dump,
            },
            issue_refs=[issue_fingerprint(issue) for issue in issues],
            critic_verdict=critic_verdict,
            critic_score=critic_score,
            critic_primary_dim=critic_primary_dim,
        )
        telemetry_summary = telemetry.summary()
        return {
            "id": draft.id,
            "summary": draft.summary,
            "bundle": draft.bundle.model_dump(mode="json", exclude_none=True),
            "counts": _bundle_counts(draft.bundle),
            "reference_report": [row.model_dump(mode="json") for row in draft.reference_report],
            "project_context_refs": draft.project_context_refs,
            "inspiration_context_refs": draft.inspiration_context_refs,
            "refine_trail": [r.model_dump(mode="json") for r in draft.refine_trail],
            # The honest normalisation ledger: shortfalls, dangling refs, dropped relations and
            # derived stages recorded instead of silently completing. Surfaced to CLI/REST/web.
            "conversion_report": draft.conversion_report.model_dump(mode="json"),
            "issues": [issue.model_dump(mode="json") for issue in issues],
            "review_item_id": item.id,
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step("world_seed", telemetry_summary)]
            ).budget.model_dump(mode="json"),
        }


def run_world_expand_action(
    content_root: str | Path,
    *,
    brief: dict[str, Any],
    sqlite_path: str | None = None,
    budget_tokens: int = 1800,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
    refine_rounds: int = 1,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Grow a batch of new, canon-grounded content on the EXISTING world at one focus, then queue it
    for review through the same write path a world seed uses (the conflict check there blocks any id
    that would overwrite canon). Reuses the ``world_seed`` gateway task — same generous timeout /
    output cap and the same StaticRouter mapping the staged service + critic both call."""
    parsed = WorldExpandBrief.model_validate(brief)
    with _project(content_root, sqlite_path) as project:
        gateway, telemetry = _gateway(
            task="world_seed",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineWorldExpandProvider(),
        )
        critic = WorldQuestCritic(gateway=gateway) if refine_rounds > 0 else None
        draft = WorldExpandService(
            gateway=gateway,
            bundle=project.bundle,
            project_context_builder=project.context_builder,
            reference_context_builder=project.reference_context_builder,
            critic=critic,
            max_refine_rounds=refine_rounds,
        ).expand(parsed, budget_tokens=budget_tokens, progress=progress)
        # Audit the MERGED preview (existing ∪ new), then report only the issues this batch
        # introduces. The new bundle alone would mis-flag every reference to an existing entity as
        # dangling — those refs are exactly the grounding we want, valid against the merged world.
        baseline = {
            issue_fingerprint(issue)
            for issue in project.audit_runner.run(AuditContext.from_bundle(project.bundle)).issues
        }
        merged = _merge_preview(project.bundle, draft.bundle)
        new_issues = [
            issue
            for issue in project.audit_runner.run(AuditContext.from_bundle(merged)).issues
            if issue_fingerprint(issue) not in baseline
        ]
        _trail_dump = [r.model_dump(mode="json") for r in draft.refine_trail]
        critic_verdict, critic_score = critic_from_trail(_trail_dump)
        critic_primary_dim = primary_dim_from_trail(_trail_dump)
        item = ReviewQueue(project.sqlite_store).add_world_seed(
            {
                "id": draft.id,
                "kind": "world_expand",
                "brief": parsed.model_dump(mode="json"),  # lets a revision re-grow the batch
                "focus_ref": parsed.focus_ref,
                "focus_label": draft.focus_label,
                "angle": draft.angle,
                "summary": f"扩写 · {draft.focus_label}",
                "bundle": draft.bundle.model_dump(mode="json", exclude_none=True),
                "grounding": draft.grounding.model_dump(mode="json"),
                "reference_report": [row.model_dump(mode="json") for row in draft.reference_report],
                "project_context_refs": draft.project_context_refs,
                "inspiration_context_refs": draft.inspiration_context_refs,
                "refine_trail": _trail_dump,
            },
            issue_refs=[issue_fingerprint(issue) for issue in new_issues],
            critic_verdict=critic_verdict,
            critic_score=critic_score,
            critic_primary_dim=critic_primary_dim,
        )
        telemetry_summary = telemetry.summary()
        return {
            "id": draft.id,
            "focus_ref": parsed.focus_ref,
            "focus_label": draft.focus_label,
            "angle": draft.angle,
            "summary": f"扩写 · {draft.focus_label}",
            "bundle": draft.bundle.model_dump(mode="json", exclude_none=True),
            "counts": _bundle_counts(draft.bundle),
            "grounding": draft.grounding.model_dump(mode="json"),
            "density": draft.density.model_dump(mode="json"),
            "reference_report": [row.model_dump(mode="json") for row in draft.reference_report],
            "project_context_refs": draft.project_context_refs,
            "inspiration_context_refs": draft.inspiration_context_refs,
            "refine_trail": [r.model_dump(mode="json") for r in draft.refine_trail],
            # The honest normalisation ledger (same key world_seed already surfaces): shortfalls,
            # dangling refs, dropped relations and derived stages recorded instead of silently
            # completing. Surfaced to CLI/REST/web so a partial expand is visible, not hidden.
            "conversion_report": draft.conversion_report.model_dump(mode="json"),
            "auto_review_incomplete": draft.auto_review_incomplete,
            "issues": [issue.model_dump(mode="json") for issue in new_issues],
            "review_item_id": item.id,
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step("world_seed", telemetry_summary)]
            ).budget.model_dump(mode="json"),
        }
