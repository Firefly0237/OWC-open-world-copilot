"""UI-agnostic actions that execute project workflow steps for any front-end.

Each action opens the project, delegates to the same `pipeline/*` workflows the CLI and REST
layers use, and returns a plain JSON-able dict. Importing no UI framework here means the whole
Workbench behaviour is unit-testable in core CI; the Vue front-end calls these through the API.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..assist.barks import BarkBatchService
from ..assist.calibration import build_calibration_report, critic_from_trail
from ..assist.characters import (
    CharacterDraft,
    CharacterProfileService,
    OfflineCharacterProvider,
)
from ..assist.critic import BarkCritic, DialogueCritic, FlavorCritic, QuestCritic
from ..assist.dialogue_trees import DialogueTreeService, OfflineDialogueTreeProvider
from ..assist.drafts import QuestDraftService
from ..assist.flavor import FlavorBatchService, OfflineFlavorProvider
from ..assist.offline import OfflineBarksProvider, OfflineQuestDraftProvider
from ..assist.prose_check import check_prose
from ..assist.review_queue import ReviewItemType, ReviewQueue
from ..audit.baseline import issue_fingerprint
from ..audit.context import AuditContext
from ..audit.report import render_audit_markdown
from ..content.hash import content_hash
from ..content.models import (
    ContentBundle,
    DialogueNode,
    DialogueTree,
    Entity,
    EntityType,
    Quest,
    Relation,
)
from ..content.relation_kinds import is_symmetric_kind
from ..exporters import EngineTarget, export_content_bundle
from ..exporters.lorebook import write_lorebook
from ..extraction import (
    ExtractionDraft,
    ExtractionService,
    OfflineExtractionProvider,
    OfflineGapFillProvider,
    apply_gap_answers,
    quests_from_beats,
)
from ..impact import Change, ChangeSet, ChangeType, ImpactAnalyzer, ImpactLevel
from ..llm.cache import CacheBackend, NoOpCache, build_cache_backend
from ..llm.gateway import LLMGateway, OpenAICompatProvider, require_offline_llm_allowed
from ..llm.router import StaticRouter
from ..llm.telemetry import TelemetryCollector
from ..pipeline.audit import run_full_audit
from ..pipeline.export_gate import assert_export_ready
from ..pipeline.ingest import run_ingest
from ..pipeline.patches import (
    apply_patch_workflow,
    find_issue,
    rollback_patch_workflow,
    suggest_for_issue,
)
from ..pipeline.review import decide_review_item
from ..qa.community_index import CommunityIndexService
from ..qa.offline import OfflineCommunityReportProvider, OfflineQAProvider
from ..qa.service import LoreQAService
from ..retrieval.embedding import resolve_embedder
from ..telemetry import deterministic_step, llm_step, summarize_workflow
from ..util import load_dotenv
from ..worldgen import (
    OfflineWorldExpandProvider,
    OfflineWorldSeedProvider,
    WorldExpandBrief,
    WorldExpandService,
    WorldQuestCritic,
    WorldSeedBrief,
    WorldSeedService,
)
from ._common import PROJECT_NAMESPACE as _PROJECT_NAMESPACE
from ._common import deterministic_cost_budget as _deterministic_cost_budget
from ._common import open_project as _project


def run_project_audit_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        result = run_full_audit(project, persist=persist)
        return {
            "audit_run": result.run.model_dump(mode="json"),
            "issues": [issue.model_dump(mode="json") for issue in result.issues],
            "open_errors": len(result.open_errors),
            "markdown_report": render_audit_markdown(
                result, content_hash=content_hash(project.bundle)
            ),
            "cost_budget": _deterministic_cost_budget("audit_project"),
        }


def list_project_issues_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
    severity: str | None = None,
    rule_code: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        issues = project.sqlite_store.list_issues(
            severity=severity, rule_code=rule_code, status=status
        )
        return {
            "count": len(issues),
            "issues": [issue.model_dump(mode="json") for issue in issues],
            "cost_budget": _deterministic_cost_budget("list_issues"),
        }


def run_ask_action(
    content_root: str | Path,
    *,
    query: str,
    sqlite_path: str | None = None,
    budget_tokens: int = 800,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
    max_cost_usd: float | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        gateway, telemetry = _gateway(
            task="qa_answer",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineQAProvider(),
            extra_tasks=["qa_expand"],
        )
        answer = LoreQAService(
            gateway=gateway,
            # QA-only builder: also recalls GraphRAG macro-overview reports so holistic questions
            # ground on cluster/global summaries instead of thinning out on row retrieval.
            context_builder=project.qa_context_builder(),
            bundle=project.bundle,
            # Widen recall with LLM query expansion on the real path; offline stays deterministic
            # (the offline provider returns no variants), so $0 tests are unaffected.
            expand=(llm_mode == "real"),
        ).ask(query, budget_tokens=budget_tokens)
        telemetry_summary = telemetry.summary()
        return {
            "answer": answer.model_dump(mode="json"),
            "llm_mode": llm_mode,
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step("ask_lore", telemetry_summary)], budget_usd=max_cost_usd
            ).budget.model_dump(mode="json"),
        }


def run_build_overview_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Build (or refresh) the GraphRAG macro-overview index: deterministic community detection +
    cached LLM community/global reports. Holistic questions in `ask` then retrieve over these.

    Cost scales with the number of *changed* communities (each is a small cheap-tier call); a
    re-run on an unchanged world is $0 (every report is a fingerprint cache hit)."""
    with _project(content_root, sqlite_path) as project:
        gateway, telemetry = _gateway(
            task="community_report",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineCommunityReportProvider(),
        )
        result = CommunityIndexService(
            gateway=gateway, store=project.sqlite_store, bundle=project.bundle
        ).build(progress=progress)
        telemetry_summary = telemetry.summary()
        return {
            "community_count": result.community_count,
            "regenerated": result.regenerated,
            "reports": [r.model_dump(mode="json") for r in result.reports],
            "llm_mode": llm_mode,
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step("build_overview", telemetry_summary)]
            ).budget.model_dump(mode="json"),
        }


def run_impact_action(
    content_root: str | Path,
    *,
    changes: list[dict[str, str]],
    sqlite_path: str | None = None,
    max_depth: int = 2,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        parsed = [
            Change(
                change_type=ChangeType(str(spec["change_type"])),
                target_ref=str(spec["target_ref"]),
            )
            for spec in changes
        ]
        result = ImpactAnalyzer(project.graph).analyze(
            ChangeSet(changes=parsed), max_depth=max_depth
        )
        return {
            "must_change": [
                item.model_dump(mode="json") for item in result.by_level(ImpactLevel.MUST_CHANGE)
            ],
            "suggest_check": [
                item.model_dump(mode="json") for item in result.by_level(ImpactLevel.SUGGEST_CHECK)
            ],
            "total": len(result.items),
            "cost_budget": _deterministic_cost_budget("impact_of"),
        }


def run_suggest_action(
    content_root: str | Path,
    *,
    issue_id: str,
    sqlite_path: str | None = None,
    max_candidates: int = 3,
    budget_tokens: int = 600,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        issue = find_issue(project, issue_id)
        gateway = None
        telemetry = TelemetryCollector()
        if llm_mode == "real":
            gateway, telemetry = _gateway(
                task="patch_suggest",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=None,
            )
        result = suggest_for_issue(
            project,
            issue,
            gateway=gateway,
            max_candidates=max_candidates,
            budget_tokens=budget_tokens,
        )
        telemetry_summary = telemetry.summary()
        return {
            "issue_id": issue_id,
            "candidates": [
                {
                    "patch_id": ranked.candidate.id,
                    "source": ranked.source,
                    "target_resolved": ranked.target_resolved,
                    "resolved_error_count": len(ranked.resolved_errors),
                    "ops": [op.model_dump(mode="json") for op in ranked.candidate.ops],
                    "rationale": ranked.candidate.rationale,
                }
                for ranked in result.candidates
            ],
            "rejected_count": result.rejected_count,
            "parse_failed": result.parse_failed,
            "used_llm": result.used_llm,
            "telemetry": telemetry_summary,
            "cost_budget": (
                summarize_workflow([llm_step("patch_suggest", telemetry_summary)]).budget
                if result.used_llm
                else summarize_workflow([deterministic_step("patch_suggest")]).budget
            ).model_dump(mode="json"),
        }


def list_patches_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        patches = project.sqlite_store.list_patches(status=status)
        return {
            "count": len(patches),
            "patches": patches,
            "cost_budget": _deterministic_cost_budget("list_patches"),
        }


def run_apply_action(
    content_root: str | Path,
    *,
    patch_id: str,
    operator: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        outcome = apply_patch_workflow(project, patch_id, operator=operator)
        return {
            "applied": outcome.applied,
            "patch_id": outcome.patch_id,
            "reason": outcome.reason,
            "introduced_errors": outcome.introduced_errors,
            "resolved_errors": outcome.resolved_errors,
            "post_audit_open_errors": outcome.post_audit_open_errors,
            "cost_budget": _deterministic_cost_budget("apply_patch"),
        }


def run_rollback_action(
    content_root: str | Path,
    *,
    patch_id: str,
    operator: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        outcome = rollback_patch_workflow(project, patch_id, operator=operator)
        return {
            "rolled_back": outcome.rolled_back,
            "patch_id": outcome.patch_id,
            "post_audit_open_errors": outcome.post_audit_open_errors,
            "cost_budget": _deterministic_cost_budget("rollback_patch"),
        }


def run_draft_action(
    content_root: str | Path,
    *,
    brief: str,
    sqlite_path: str | None = None,
    budget_tokens: int = 800,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
    refine_rounds: int = 2,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        gateway, telemetry = _gateway(
            task="quest_draft",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineQuestDraftProvider(),
        )
        # The critic + refine loop raises autonomous quality so the review queue gets near
        # production-ready drafts; deterministic audit stays the hard gate and review stays final.
        critic = QuestCritic(gateway=gateway) if refine_rounds > 0 else None
        result = QuestDraftService(
            gateway=gateway,
            context_builder=project.context_builder,
            audit_runner=project.audit_runner,
            bundle=project.bundle,
            critic=critic,
            max_refine_rounds=refine_rounds,
        ).draft_quest(brief, budget_tokens=budget_tokens)
        critic_verdict, critic_score = critic_from_trail(
            [r.model_dump(mode="json") for r in result.refine_trail]
        )
        item = ReviewQueue(project.sqlite_store).add_quest_draft(
            result.quest.model_dump(mode="json", exclude_none=True),
            issue_refs=[issue_fingerprint(issue) for issue in result.issues],
            critic_verdict=critic_verdict,
            critic_score=critic_score,
        )
        telemetry_summary = telemetry.summary()
        return {
            "quest": result.quest.model_dump(mode="json", exclude_none=True),
            "issues": [issue.model_dump(mode="json") for issue in result.issues],
            "refine_trail": [r.model_dump(mode="json") for r in result.refine_trail],
            "auto_review_incomplete": result.auto_review_incomplete,
            "review_item_id": item.id,
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step("quest_draft", telemetry_summary)]
            ).budget.model_dump(mode="json"),
        }


def run_barks_action(
    content_root: str | Path,
    *,
    speaker_ids: list[str],
    topic: str,
    sqlite_path: str | None = None,
    variants_per_speaker: int = 4,
    max_chars: int = 40,
    refine_rounds: int = 0,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        unknown = [sid for sid in speaker_ids if sid not in project.bundle.entities]
        if unknown:
            raise ValueError(f"unknown speaker entities: {', '.join(unknown)}")
        gateway, telemetry = _gateway(
            task="barks_batch",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineBarksProvider(),
        )
        result = BarkBatchService(
            gateway=gateway,
            bundle=project.bundle,
            review_queue=ReviewQueue(project.sqlite_store),
            critic=BarkCritic(gateway=gateway) if refine_rounds > 0 else None,
            max_refine_rounds=refine_rounds,
        ).generate(
            speaker_ids=speaker_ids,
            topic=topic,
            variants_per_speaker=variants_per_speaker,
            max_chars=max_chars,
            allowed_entity_ids=set(speaker_ids),
        )
        telemetry_summary = telemetry.summary()
        return {
            "accepted": [
                {"speaker_id": variant.speaker_id, "text": variant.text}
                for variant in result.accepted
            ],
            "rejected": [
                {
                    "speaker_id": rejected.speaker_id,
                    "text": rejected.text,
                    "issues": [issue.code for issue in rejected.issues],
                }
                for rejected in result.rejected
            ],
            "review_item_ids": [item.id for item in result.review_items],
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step("barks_batch", telemetry_summary)]
            ).budget.model_dump(mode="json"),
        }


def add_reference_action(
    content_root: str | Path,
    *,
    title: str,
    text: str,
    sqlite_path: str | None = None,
    source_type: str = "uploaded_file",
    original_filename: str | None = None,
    allowed_uses: list[str] | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        result = project.reference_store.add_text(
            title=title,
            text=text,
            source_type=source_type,
            original_filename=original_filename,
            allowed_uses=allowed_uses,
        )
        project.reference_store.sync_index(project.sqlite_store)
        return {
            "source": result.source.model_dump(mode="json"),
            "chunks": [chunk.model_dump(mode="json") for chunk in result.chunks],
            "indexed_count": result.indexed_count,
            # Untrusted reference text that matched a prompt-injection pattern, surfaced so the
            # human can make a risk call before it grounds a generation prompt (OWASP LLM01).
            "injection_flagged_chunks": result.injection_flagged_chunks,
            "cost_budget": _deterministic_cost_budget("reference_add"),
        }


def list_references_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        sources = project.reference_store.list_sources()
        return {
            "count": len(sources),
            "sources": [source.model_dump(mode="json") for source in sources],
            "cost_budget": _deterministic_cost_budget("reference_list"),
        }


def search_references_action(
    content_root: str | Path,
    *,
    query: str,
    sqlite_path: str | None = None,
    budget_tokens: int = 1000,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        pack = project.reference_context_builder.build(query, budget_tokens=budget_tokens)
        return {
            "query": query,
            "refs": pack.refs,
            "hits": [hit.model_dump(mode="json") for hit in pack.hits],
            "cost_budget": _deterministic_cost_budget("reference_search"),
        }


def run_world_seed_action(
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
        draft = WorldSeedService(
            gateway=gateway,
            bundle=project.bundle,
            project_context_builder=project.context_builder,
            reference_context_builder=project.reference_context_builder,
            critic=critic,
            max_refine_rounds=refine_rounds,
        ).generate(parsed, budget_tokens=budget_tokens, progress=progress)
        issues = project.audit_runner.run(AuditContext.from_bundle(draft.bundle)).issues
        item = ReviewQueue(project.sqlite_store).add_world_seed(
            {
                "id": draft.id,
                "brief": draft.brief.model_dump(mode="json"),
                "summary": draft.summary,
                "bundle": draft.bundle.model_dump(mode="json", exclude_none=True),
                "reference_report": [row.model_dump(mode="json") for row in draft.reference_report],
                "project_context_refs": draft.project_context_refs,
                "inspiration_context_refs": draft.inspiration_context_refs,
                "refine_trail": [r.model_dump(mode="json") for r in draft.refine_trail],
            },
            issue_refs=[issue_fingerprint(issue) for issue in issues],
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
                "refine_trail": [r.model_dump(mode="json") for r in draft.refine_trail],
            },
            issue_refs=[issue_fingerprint(issue) for issue in new_issues],
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
            "auto_review_incomplete": draft.auto_review_incomplete,
            "issues": [issue.model_dump(mode="json") for issue in new_issues],
            "review_item_id": item.id,
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step("world_seed", telemetry_summary)]
            ).budget.model_dump(mode="json"),
        }


def _merge_preview(existing: ContentBundle, new: ContentBundle) -> ContentBundle:
    """A throwaway existing∪new bundle for auditing what an expansion batch would land — never
    persisted (accept does the real merge through the review write path)."""
    merged = existing.model_copy(deep=True)
    merged.entities.update(new.entities)
    merged.pois.update(new.pois)
    merged.regions.update(new.regions)
    merged.quests.update(new.quests)
    merged.terms.update(new.terms)
    merged.relations.extend(new.relations)
    return merged


def list_review_items_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        pending = ReviewQueue(project.sqlite_store).list_pending()
        return {
            "count": len(pending),
            "items": [item.model_dump(mode="json") for item in pending],
            "cost_budget": _deterministic_cost_budget("review_list"),
        }


def decide_review_action(
    content_root: str | Path,
    *,
    item_id: str,
    decision: str,
    operator: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        outcome = decide_review_item(project, item_id, decision=decision, operator=operator)
        return {
            "decision": outcome.decision,
            "item": outcome.item.model_dump(mode="json"),
            "written_ref": outcome.written_ref,
            "post_audit_open_errors": outcome.post_audit_open_errors,
            "cost_budget": _deterministic_cost_budget("review_decide"),
        }


def reviewer_calibration_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """How well does the critic's verdict track the human's final accept/reject? Surfaces the
    false-pass blind spot (critic said pass, human rejected) over the resolved review history."""
    with _project(content_root, sqlite_path) as project:
        resolved = ReviewQueue(project.sqlite_store).list_resolved()
        report = build_calibration_report(resolved)
        return {
            **report.model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("review_calibration"),
        }


def revise_draft_action(
    content_root: str | Path,
    *,
    item_id: str,
    feedback: str,
    sqlite_path: str | None = None,
    budget_tokens: int = 800,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
) -> dict[str, Any]:
    """Feedback-driven revision: regenerate a pending draft to address a reviewer's note.

    The reviewer is no longer limited to accept/reject — they can ask for changes and the product
    revises in place (reusing each kind's regenerate-with-feedback path), and the item stays in
    review for another look. The revised draft never lands without a human still approving it."""
    note = feedback.strip()
    if not note:
        raise ValueError("feedback is required for a revision")
    with _project(content_root, sqlite_path) as project:
        queue = ReviewQueue(project.sqlite_store)
        item = queue.get(item_id)
        if item.status != "pending_review":
            raise ValueError(f"review item {item_id} is not pending (status={item.status})")

        if item.item_type is ReviewItemType.QUEST_DRAFT:
            gateway, telemetry = _gateway(
                task="quest_draft",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=OfflineQuestDraftProvider(),
            )
            revised = QuestDraftService(
                gateway=gateway,
                context_builder=project.context_builder,
                audit_runner=project.audit_runner,
                bundle=project.bundle,
            ).revise(Quest.model_validate(item.payload), note, budget_tokens=budget_tokens)
            new_payload = revised.quest.model_dump(mode="json", exclude_none=True)
            task = "quest_draft"
        elif item.item_type is ReviewItemType.CHARACTER_PROFILE:
            gateway, telemetry = _gateway(
                task="character_profile",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=OfflineCharacterProvider(),
            )
            entity = Entity.model_validate(item.payload.get("entity") or {})
            prior = CharacterDraft(
                entity=entity,
                relations=[
                    Relation.model_validate(raw) for raw in (item.payload.get("relations") or [])
                ],
                profile=dict(item.payload.get("profile") or entity.metadata.get("profile") or {}),
            )
            character = CharacterProfileService(
                gateway=gateway, bundle=project.bundle, context_builder=project.context_builder
            ).revise(prior, note, budget_tokens=budget_tokens)
            new_payload = {
                "entity": character.entity.model_dump(mode="json"),
                "relations": [r.model_dump(mode="json") for r in character.relations],
                "profile": character.profile,
                "suggested_relations": character.suggested_relations,
            }
            task = "character_profile"
        elif item.item_type is ReviewItemType.DIALOGUE_TREE:
            gateway, telemetry = _gateway(
                task="dialogue_tree",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=OfflineDialogueTreeProvider(),
            )
            tree = DialogueTreeService(gateway=gateway, bundle=project.bundle).revise(
                DialogueTree.model_validate(item.payload), note
            )
            new_payload = tree.model_dump(mode="json", exclude_none=True)
            task = "dialogue_tree"
        elif (
            item.item_type is ReviewItemType.WORLD_SEED
            and item.payload.get("kind") == "world_expand"
        ):
            # World-expansion drafts carry item_type WORLD_SEED but a different payload shape (focus
            # /angle, no seed brief). Re-grow the cohesive batch at the same focus, steered by the
            # note, via the tested staged expand pipeline — NOT the seed-revise path, which would
            # crash on the missing brief and lose the focus grounding.
            gateway, telemetry = _gateway(
                task="world_seed",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=OfflineWorldExpandProvider(),
            )
            expand_brief = WorldExpandBrief.model_validate(
                item.payload.get("brief")
                or {  # pre-brief items still carry focus_ref/angle — enough to reconstruct
                    "focus_ref": item.payload.get("focus_ref", ""),
                    "angle": item.payload.get("angle", ""),
                }
            )
            draft = WorldExpandService(
                gateway=gateway,
                bundle=project.bundle,
                project_context_builder=project.context_builder,
                reference_context_builder=project.reference_context_builder,
            ).expand(expand_brief, budget_tokens=budget_tokens, feedback=note)
            new_payload = {
                **item.payload,
                "brief": expand_brief.model_dump(mode="json"),
                "focus_label": draft.focus_label,
                "angle": draft.angle,
                "summary": f"扩写 · {draft.focus_label}",
                "bundle": draft.bundle.model_dump(mode="json", exclude_none=True),
                "grounding": draft.grounding.model_dump(mode="json"),
                "reference_report": [row.model_dump(mode="json") for row in draft.reference_report],
                "project_context_refs": draft.project_context_refs,
                "inspiration_context_refs": draft.inspiration_context_refs,
                "refine_trail": [r.model_dump(mode="json") for r in draft.refine_trail],
            }
            task = "world_seed"
        elif item.item_type is ReviewItemType.WORLD_SEED:
            gateway, telemetry = _gateway(
                task="world_seed",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=OfflineWorldSeedProvider(),
            )
            prior_bundle = ContentBundle.model_validate(item.payload.get("bundle") or {})
            seed_brief = WorldSeedBrief.model_validate(item.payload.get("brief") or {})
            draft_id = str(item.payload.get("id") or item.object_ref.split(":", 1)[-1])
            revised_bundle, revised_stage = WorldSeedService(
                gateway=gateway,
                bundle=project.bundle,
                project_context_builder=project.context_builder,
                reference_context_builder=project.reference_context_builder,
            ).revise(prior_bundle, seed_brief, note, draft_id=draft_id)
            new_payload = {
                **item.payload,
                "bundle": revised_bundle.model_dump(mode="json", exclude_none=True),
                "revised_stage": revised_stage,
            }
            task = "world_seed"
        else:
            raise ValueError(f"feedback revision is not yet supported for {item.item_type.value}")

        updated = queue.update_payload(item_id, new_payload)
        telemetry_summary = telemetry.summary()
        return {
            "item": updated.model_dump(mode="json"),
            "revised_payload": new_payload,
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step(task, telemetry_summary)]
            ).budget.model_dump(mode="json"),
        }


def run_project_export_action(
    content_root: str | Path,
    *,
    output_dir: str | Path,
    target_engine: EngineTarget | str = EngineTarget.GENERIC,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    engine = EngineTarget(target_engine)
    actual_output = Path(output_dir) / engine.value
    with _project(content_root, sqlite_path) as project:
        assert_export_ready(project)
        manifest = export_content_bundle(project.bundle, actual_output, target_engine=engine)
        return {
            "output_dir": str(actual_output),
            "manifest": manifest.model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("export_project"),
        }


# Long-form generation needs more wall-clock and output headroom than chat-sized calls.
# Floors are combined with the user-tunable base (OWCOPILOT_PROVIDER_TIMEOUT_SEC, settable
# from 设置→高级): effective timeout = max(base, floor). Round-11 trigger: a default-scale
# world_seed (8 npcs / 5 quests) takes well past the old flat 30s and timed out for real.
_TASK_TIMEOUT_FLOOR_SEC: dict[str, float] = {
    "world_seed": 240.0,
    "extract_lore": 240.0,
    "character_profile": 120.0,
    "extract_fill": 120.0,
    "dialogue_tree": 120.0,
    "flavor_batch": 90.0,
    "quest_draft": 90.0,
    "barks_batch": 90.0,
    "patch_suggest": 90.0,
}
_TASK_TIMEOUT_DEFAULT_SEC = 60.0
# Output caps: enough headroom that a full default-scale bundle is not truncated
# mid-JSON, while still stopping runaway generations.
_TASK_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "world_seed": 6000,
    "extract_lore": 4500,
    # dialogue_tree raised 4000->6000: the 二游 quality bar (subtext, distinct voices) makes lines
    # longer, so a 12-node tree over a rich world occasionally truncated mid-JSON and failed parse.
    "dialogue_tree": 6000,
    # quest_draft used to run terse; the 二游 quality bar makes stages real scenes, so the JSON is
    # bigger now — give it room or rich drafts truncate mid-object and fail to parse.
    "quest_draft": 3500,
}


def _task_timeout_sec(task: str) -> float:
    base = float(os.getenv("OWCOPILOT_PROVIDER_TIMEOUT_SEC", "0") or 0)
    floor = _TASK_TIMEOUT_FLOOR_SEC.get(task, _TASK_TIMEOUT_DEFAULT_SEC)
    return max(base, floor)


_ACTION_CACHE: CacheBackend | None = None
_ACTION_CACHE_LOCK = threading.Lock()


def _action_cache() -> CacheBackend:
    """One process-lifetime L1/L2 cache shared by every real-mode action gateway.

    Same assembly as the REST service (`service.api._build_cache`): without it, every
    Workbench rerun built a throwaway gateway with NoOpCache and a user asking the same
    question twice paid twice. Offline mode stays uncached on purpose — it is the test
    substrate, and cross-test result sharing would couple unrelated tests.
    """
    global _ACTION_CACHE
    if _ACTION_CACHE is None:
        # Double-checked under a lock: concurrent job-runner threads must share one cache (and one
        # embedder load), not each build their own and defeat the reuse this exists for.
        with _ACTION_CACHE_LOCK:
            if _ACTION_CACHE is None:
                # bge-m3 (when installed) so the L2 paraphrase cache works for CJK; falls back
                # to the deterministic hashing stub by env (tests, or no [semantic] extra).
                _ACTION_CACHE = build_cache_backend("exact+semantic", embedder=resolve_embedder())
    return _ACTION_CACHE


def _gateway(
    *,
    task: str,
    llm_mode: str,
    llm_model: str,
    offline_provider: Any,
    extra_tasks: list[str] | None = None,
) -> tuple[LLMGateway, TelemetryCollector]:
    telemetry = TelemetryCollector()
    real = llm_mode == "real"
    if real:
        load_dotenv()  # pick up provider keys from .env; shell env wins
        provider: Any = OpenAICompatProvider(
            model=llm_model,
            timeout=_task_timeout_sec(task),
            max_output_tokens=_TASK_MAX_OUTPUT_TOKENS.get(task),
        )
    else:
        require_offline_llm_allowed()  # offline fakes are a test/CI fixture, not a product mode
        provider = offline_provider
    mapping = {t: "cheap" for t in (task, *(extra_tasks or []))}
    gateway = LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping=mapping),
        cache=_action_cache() if real else NoOpCache(),
        telemetry=telemetry,
        max_retries=1 if real else 0,
        retry_backoff_seconds=1.0 if real else 0.0,
        namespace=_PROJECT_NAMESPACE.get(),
    )
    return gateway, telemetry


def _bundle_counts(bundle: Any) -> dict[str, int]:
    return {
        "entities": len(bundle.entities),
        "relations": len(bundle.relations),
        "quests": len(bundle.quests),
        "regions": len(bundle.regions),
        "pois": len(bundle.pois),
        "terms": len(bundle.terms),
        "style_guides": len(bundle.style_guides),
    }


def run_extraction_action(
    content_root: str | Path,
    *,
    title: str,
    text: str,
    source_kind: str = "文稿",
    sqlite_path: str | None = None,
    glean_rounds: int = 1,
    verify_faithfulness: bool = False,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Distill an unstructured manuscript into a reviewable draft (entities/relations/beats).

    The whole manuscript is covered automatically — granularity and language handling are the
    service's responsibility, not a knob the caller (or the creator) has to set. ``glean_rounds``
    adds GraphRAG-style recovery passes per chunk (default one) to catch overlooked entities.
    ``verify_faithfulness`` adds the LLM entailment tier (real mode only — it needs a real judge,
    not the offline fake)."""
    with _project(content_root, sqlite_path) as project:
        gateway, telemetry = _gateway(
            task="extract_lore",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineExtractionProvider(),
        )
        draft = ExtractionService(gateway=gateway, bundle=project.bundle).extract(
            title=title,
            text=text,
            source_kind=source_kind,
            glean_rounds=glean_rounds,
            verify_faithfulness=verify_faithfulness and llm_mode == "real",
            progress=progress,
        )
        telemetry_summary = telemetry.summary()
        return {
            "draft": draft.model_dump(mode="json", exclude_none=True),
            "stats": draft.stats,
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step("extract_lore", telemetry_summary)]
            ).budget.model_dump(mode="json"),
        }


def fill_extraction_gaps_action(
    content_root: str | Path,
    *,
    draft: dict[str, Any],
    gap_refs: list[str] | None = None,
    sqlite_path: str | None = None,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
) -> dict[str, Any]:
    """Ask the model to suggest values for missing fields; the user confirms before submit."""
    parsed = ExtractionDraft.model_validate(draft)
    with _project(content_root, sqlite_path):
        gateway, telemetry = _gateway(
            task="extract_fill",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineGapFillProvider(),
        )
        service = ExtractionService(gateway=gateway, bundle=ContentBundle())
        updated = service.fill_gaps(parsed, gap_refs=gap_refs)
        telemetry_summary = telemetry.summary()
        return {
            "draft": updated.model_dump(mode="json", exclude_none=True),
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step("extract_fill", telemetry_summary)]
            ).budget.model_dump(mode="json"),
        }


def submit_extraction_action(
    content_root: str | Path,
    *,
    draft: dict[str, Any],
    answers: dict[str, str] | None = None,
    include_beats_as_quests: bool = False,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Apply confirmed gap answers and push the draft into the review queue (no direct write)."""
    parsed = ExtractionDraft.model_validate(draft)
    if answers:
        parsed = apply_gap_answers(parsed, answers)
    if include_beats_as_quests:
        parsed.bundle.quests.update(quests_from_beats(parsed))
    with _project(content_root, sqlite_path) as project:
        issues = project.audit_runner.run(AuditContext.from_bundle(parsed.bundle)).issues
        item = ReviewQueue(project.sqlite_store).add_import_draft(
            {
                "id": parsed.id,
                "source_title": parsed.source_title,
                "source_kind": parsed.source_kind,
                "summary": parsed.summary,
                "bundle": parsed.bundle.model_dump(mode="json", exclude_none=True),
                "plot_beats": [beat.model_dump(mode="json") for beat in parsed.plot_beats],
                "open_gaps": [gap.model_dump(mode="json") for gap in parsed.gaps],
            },
            issue_refs=[issue_fingerprint(issue) for issue in issues],
        )
        return {
            "review_item_id": item.id,
            "draft_id": parsed.id,
            "open_gaps": len(parsed.gaps),
            "issues": [issue.model_dump(mode="json") for issue in issues],
            "counts": _bundle_counts(parsed.bundle),
            "cost_budget": _deterministic_cost_budget("extraction_submit"),
        }


def run_dialogue_tree_action(
    content_root: str | Path,
    *,
    participant_ids: list[str],
    brief: str,
    quest_id: str | None = None,
    sqlite_path: str | None = None,
    max_nodes: int = 12,
    max_chars: int = 120,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
    refine_rounds: int = 1,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        gateway, telemetry = _gateway(
            task="dialogue_tree",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineDialogueTreeProvider(),
        )
        critic = DialogueCritic(gateway=gateway) if refine_rounds > 0 else None
        result = DialogueTreeService(
            gateway=gateway,
            bundle=project.bundle,
            review_queue=ReviewQueue(project.sqlite_store),
            critic=critic,
            max_refine_rounds=refine_rounds,
        ).generate(
            participant_ids=participant_ids,
            brief=brief,
            quest_id=quest_id,
            max_nodes=max_nodes,
            max_chars=max_chars,
        )
        telemetry_summary = telemetry.summary()
        return {
            "tree": result.tree.model_dump(mode="json", exclude_none=True),
            "lint_issues": [issue.model_dump(mode="json") for issue in result.lint_issues],
            "structure_problems": result.structure_problems,
            "refine_trail": [r.model_dump(mode="json") for r in result.refine_trail],
            "auto_review_incomplete": result.auto_review_incomplete,
            "review_item_id": result.review_item.id if result.review_item else None,
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step("dialogue_tree", telemetry_summary)]
            ).budget.model_dump(mode="json"),
        }


def run_flavor_action(
    content_root: str | Path,
    *,
    category: str,
    names: list[str],
    theme: str = "",
    sqlite_path: str | None = None,
    max_chars: int = 120,
    refine_rounds: int = 0,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        gateway, telemetry = _gateway(
            task="flavor_batch",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineFlavorProvider(),
        )
        result = FlavorBatchService(
            gateway=gateway,
            bundle=project.bundle,
            review_queue=ReviewQueue(project.sqlite_store),
            critic=FlavorCritic(gateway=gateway) if refine_rounds > 0 else None,
            max_refine_rounds=refine_rounds,
        ).generate(category=category, names=names, theme=theme, max_chars=max_chars)
        telemetry_summary = telemetry.summary()
        return {
            "batch_id": result.batch_id,
            "category": result.category,
            "accepted": [entry.model_dump(mode="json") for entry in result.accepted],
            "rejected": [
                {
                    "name": rejected.name,
                    "text": rejected.text,
                    "issues": [issue.code for issue in rejected.issues],
                }
                for rejected in result.rejected
            ],
            "review_item_id": result.review_item.id if result.review_item else None,
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step("flavor_batch", telemetry_summary)]
            ).budget.model_dump(mode="json"),
        }


def run_ingest_action(
    content_root: str | Path,
    *,
    paths: list[str],
    sqlite_path: str | None = None,
    dry_run: bool = True,
    write_non_conflicting: bool = False,
) -> dict[str, Any]:
    """Strict-format import (xlsx/json/jsonl/md/luban). Defaults to dry-run preview."""
    with _project(content_root, sqlite_path) as project:
        result = run_ingest(
            project,
            list(paths),
            dry_run=dry_run,
            write_non_conflicting=write_non_conflicting,
        )
        return {
            "dry_run": result.dry_run,
            "incoming_count": result.incoming_count,
            "changes": [change.model_dump(mode="json") for change in result.changes],
            "issues": [issue.model_dump(mode="json") for issue in result.issues],
            "has_errors": result.has_errors,
            "content_hash_before": result.content_hash_before,
            "content_hash_after": result.content_hash_after,
            "cost_budget": _deterministic_cost_budget("ingest"),
        }


def run_character_action(
    content_root: str | Path,
    *,
    brief: dict[str, Any],
    sqlite_path: str | None = None,
    budget_tokens: int = 1200,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
    refine_rounds: int = 1,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Generate one detailed character sheet grounded in the world; queue it for review."""
    from ..assist.characters import (
        CharacterBrief,
        CharacterProfileService,
        OfflineCharacterProvider,
    )
    from ..assist.critic import CharacterCritic

    parsed = CharacterBrief.model_validate(brief)

    def emit(name: str) -> None:
        if progress is not None:
            progress("stage", {"name": name})

    with _project(content_root, sqlite_path) as project:
        gateway, telemetry = _gateway(
            task="character_profile",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineCharacterProvider(),
        )
        emit("retrieving")
        service = CharacterProfileService(
            gateway=gateway,
            bundle=project.bundle,
            context_builder=project.context_builder,
            critic=CharacterCritic(gateway=gateway) if refine_rounds > 0 else None,
            max_refine_rounds=refine_rounds,
        )
        emit("generating")
        draft = service.generate(parsed, budget_tokens=budget_tokens)
        emit("parsing")
        critic_verdict, critic_score = critic_from_trail(
            [r.model_dump(mode="json") for r in draft.refine_trail]
        )
        item = ReviewQueue(project.sqlite_store).add_character_profile(
            {
                "entity": draft.entity.model_dump(mode="json", exclude_none=True),
                "relations": [r.model_dump(mode="json") for r in draft.relations],
                "summary": draft.entity.description,
                "suggested_relations": draft.suggested_relations,
            },
            critic_verdict=critic_verdict,
            critic_score=critic_score,
        )
        telemetry_summary = telemetry.summary()
        return {
            "entity": draft.entity.model_dump(mode="json", exclude_none=True),
            "relations": [r.model_dump(mode="json") for r in draft.relations],
            "profile": draft.profile,
            "suggested_relations": draft.suggested_relations,
            "refine_trail": [r.model_dump(mode="json") for r in draft.refine_trail],
            "auto_review_incomplete": draft.auto_review_incomplete,
            "review_item_id": item.id,
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step("character_profile", telemetry_summary)]
            ).budget.model_dump(mode="json"),
        }


def run_theme_sweep_action(
    content_root: str | Path,
    *,
    theme: str,
    extra_terms: list[str] | None = None,
    use_llm: bool = False,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
    max_judge: int = 400,
    semantic_threshold: float = 0.5,
    sqlite_path: str | None = None,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Theme sweep over the whole world (term layer + semantic paraphrase layer + optional LLM
    judge + graph expansion). Read-only: produces a work order for a human, writes nothing."""
    from ..assist.sweep import OfflineSweepJudgeProvider, ThemeSweepService, render_sweep_markdown

    with _project(content_root, sqlite_path) as project:
        gateway = None
        telemetry_summary: dict[str, Any] | None = None
        if use_llm:
            gateway, telemetry = _gateway(
                task="theme_sweep",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=OfflineSweepJudgeProvider(),
            )
        report = ThemeSweepService(
            bundle=project.bundle,
            gateway=gateway,
            # the project embedder turns on the deterministic paraphrase layer when it is a real
            # semantic model (bge-m3); it self-disables on the hashing stub.
            embedder=project.embedder,
        ).sweep(
            theme,
            extra_terms=extra_terms,
            use_llm=use_llm,
            max_judge=max_judge,
            semantic_threshold=semantic_threshold,
            progress=progress,
        )
        if use_llm:
            telemetry_summary = telemetry.summary()
        return {
            "theme": report.theme,
            "terms": report.terms,
            "scanned_total": report.scanned_total,
            "scanned_by_kind": report.scanned_by_kind,
            "llm_used": report.llm_used,
            "semantic_used": report.semantic_used,
            "semantic_flagged": report.semantic_flagged,
            "judged_count": report.judged_count,
            "judge_skipped": report.judge_skipped,
            "hits": [finding.__dict__ for finding in report.hits],
            "review_suggested": [finding.__dict__ for finding in report.review_suggested],
            "markdown": render_sweep_markdown(report),
            "cost_budget": (
                summarize_workflow([llm_step("theme_sweep", telemetry_summary)]).budget.model_dump(
                    mode="json"
                )
                if telemetry_summary is not None
                else _deterministic_cost_budget("theme_sweep")
            ),
        }


def detect_contradictions_action(
    content_root: str | Path,
    *,
    use_llm: bool = False,
    semantic_threshold: float = 0.6,
    max_judge: int = 200,
    sqlite_path: str | None = None,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
) -> dict[str, Any]:
    """Batch-2 · find semantic contradictions in canon (allies-here-enemies-there, conflicting
    attributes). Read-only: recalls candidate pairs deterministically, an optional LLM judge
    confirms genuine conflicts; without the judge they are surfaced for human review, never
    asserted. Writes nothing."""
    from ..assist.contradiction import ContradictionDetector, OfflineContradictionJudge

    with _project(content_root, sqlite_path) as project:
        gateway = None
        telemetry_summary: dict[str, Any] | None = None
        if use_llm:
            gateway, telemetry = _gateway(
                task="contradiction_judge",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=OfflineContradictionJudge(),
            )
        report = ContradictionDetector(
            bundle=project.bundle, gateway=gateway, embedder=project.embedder
        ).detect(use_llm=use_llm, semantic_threshold=semantic_threshold, max_judge=max_judge)
        if use_llm:
            telemetry_summary = telemetry.summary()
        return {
            "candidate_count": report.candidate_count,
            "judged_count": report.judged_count,
            "semantic_used": report.semantic_used,
            "llm_used": report.llm_used,
            "contradictions": [f.__dict__ for f in report.contradictions],
            "review_suggested": [f.__dict__ for f in report.review_suggested],
            "cost_budget": (
                summarize_workflow(
                    [llm_step("contradiction_judge", telemetry_summary)]
                ).budget.model_dump(mode="json")
                if telemetry_summary is not None
                else _deterministic_cost_budget("contradiction_scan")
            ),
        }


# >>> WS-D compliance remediation workflow >>>
def _rule_pack(rule_pack: dict[str, Any] | None) -> Any:
    from ..compliance.models import DEFAULT_RULE_PACK, RulePack

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
    from ..assist.sweep import OfflineSweepJudgeProvider, ThemeSweepService
    from ..compliance.service import build_compliance_report, open_cases_from_sweep
    from ..compliance.store import CaseStore

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
    from ..compliance.service import build_compliance_report
    from ..compliance.store import CaseStore

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
    from ..compliance.models import CaseStatus
    from ..compliance.service import transition
    from ..compliance.store import CaseStore

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
    from ..assist.sweep import OfflineSweepJudgeProvider
    from ..compliance.service import rescan_case
    from ..compliance.store import CaseStore

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


# <<< WS-D compliance remediation workflow <<<


def update_entity_action(
    content_root: str | Path,
    *,
    entity_id: str,
    name: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    metadata_updates: dict[str, Any] | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Edit an entity's display fields in place. The id never changes here, so references
    in quests/relations stay valid — renames of the id itself go through the impact +
    patch workflow instead. `metadata_updates` shallow-merges (a None value deletes the
    key), which is how character-sheet sections are maintained."""
    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        entity = bundle.entities.get(entity_id)
        if entity is None:
            raise ValueError(f"实体不存在：{entity_id}")
        update: dict[str, Any] = {}
        if name is not None and name.strip():
            update["name"] = name.strip()
        if description is not None:
            update["description"] = description.strip()
        if tags is not None:
            update["tags"] = [str(tag).strip() for tag in tags if str(tag).strip()]
        if metadata_updates:
            merged = dict(entity.metadata)
            for key, value in metadata_updates.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            update["metadata"] = merged
        if update:
            bundle.entities[entity_id] = entity.model_copy(update=update)
            project.content_store.save(bundle)
            project.reload()
        return {
            "entity": bundle.entities[entity_id].model_dump(mode="json"),
            "changed": sorted(update),
            "cost_budget": _deterministic_cost_budget("entity_update"),
        }


def update_style_guide_action(
    content_root: str | Path,
    *,
    guide_id: str = "style_guide",
    body: str | None = None,
    rules: list[str] | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """B10: edit the world's style guide (worldview body + rules) in place — the same direct
    human-edit pipeline as the archive (load→save→reload, signed human, lands immediately, no AI
    review queue). The body and rules round-trip through the full-fidelity style_guides.json."""
    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        guide = bundle.style_guides.get(guide_id)
        if guide is None:
            raise ValueError(f"风格指南不存在：{guide_id}")
        update: dict[str, Any] = {}
        if body is not None:
            update["body"] = body.strip()
        if rules is not None:
            update["rules"] = [str(r).strip() for r in rules if str(r).strip()]
        if update:
            bundle.style_guides[guide_id] = guide.model_copy(update=update)
            project.content_store.save(bundle)
            project.reload()
        return {
            "style_guide": bundle.style_guides[guide_id].model_dump(mode="json"),
            "changed": sorted(update),
            "cost_budget": _deterministic_cost_budget("style_guide_update"),
        }


def delete_object_action(
    content_root: str | Path,
    *,
    ref_type: str,
    object_id: str,
    cascade_relations: bool = True,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Delete one object from the world. Entities optionally cascade their relations;
    anything still referencing the deleted object surfaces in the next audit (by design —
    the UI shows an impact preview before calling this)."""
    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        removed_relations = 0
        if ref_type == "entity":
            if object_id not in bundle.entities:
                raise ValueError(f"实体不存在：{object_id}")
            del bundle.entities[object_id]
            if cascade_relations:
                before = len(bundle.relations)
                bundle.relations = [
                    rel
                    for rel in bundle.relations
                    if rel.source != object_id and rel.target != object_id
                ]
                removed_relations = before - len(bundle.relations)
        elif ref_type == "quest":
            if object_id not in bundle.quests:
                raise ValueError(f"任务不存在：{object_id}")
            del bundle.quests[object_id]
        elif ref_type == "dialogue_tree":
            if object_id not in bundle.dialogue_trees:
                raise ValueError(f"对话树不存在：{object_id}")
            del bundle.dialogue_trees[object_id]
        else:
            raise ValueError(f"不支持删除的类型：{ref_type}")
        # ContentStore.save reconciles directories against the bundle (orphan json files
        # are removed), so no per-file unlink bookkeeping belongs here.
        project.content_store.save(bundle)
        project.reload()
        audit = run_full_audit(project, persist=True)
        return {
            "deleted_ref": f"{ref_type}:{object_id}",
            "removed_relations": removed_relations,
            "post_audit_open_errors": len(audit.open_errors),
            "cost_budget": _deterministic_cost_budget("object_delete"),
        }


# --- graph/timeline/dialogue direct editing -------------------------------------------------------
#
# Same direct-human-edit pipeline as update_entity_action above (load → mutate → save → reload,
# provenance stays human, immediate to canon) — the graph/timeline/dialogue editors are just another
# entry point to it, NOT the AI review queue (that is only for AI products). They fill the
# create/relation/quest/dialogue gaps the archive editor never needed.

_ENTITY_ID_PREFIX: dict[EntityType, str] = {
    EntityType.NPC: "npc",
    EntityType.LOCATION: "loc",
    EntityType.FACTION: "fac",
    EntityType.ITEM: "item",
    EntityType.EVENT: "ev",
    EntityType.REGION: "region",
    EntityType.ORGANIZATION: "org",
    EntityType.CONCEPT: "concept",
    EntityType.TERM: "term",
    EntityType.SKILL: "skill",
    EntityType.ACHIEVEMENT: "ach",
}


def _new_entity_id(bundle: ContentBundle, *, name: str, entity_type: EntityType) -> str:
    prefix = _ENTITY_ID_PREFIX.get(entity_type, "ent")
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    candidate = f"{prefix}_{digest}"
    counter = 2
    while candidate in bundle.entities:
        candidate = f"{prefix}_{digest}_{counter}"
        counter += 1
    return candidate


def create_entity_action(
    content_root: str | Path,
    *,
    name: str,
    entity_type: str,
    description: str = "",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Create a new entity from the graph (human-authored, immediate to canon)."""
    if not name.strip():
        raise ValueError("名字不能为空")
    try:
        resolved_type = EntityType(entity_type)
    except ValueError as exc:
        raise ValueError(f"不支持的实体类型：{entity_type}") from exc
    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        entity = Entity(
            id=_new_entity_id(bundle, name=name, entity_type=resolved_type),
            name=name.strip(),
            type=resolved_type,
            description=description.strip(),
            tags=[str(tag).strip() for tag in (tags or []) if str(tag).strip()],
            metadata=metadata or {},
        )
        bundle.entities[entity.id] = entity
        project.content_store.save(bundle)
        project.reload()
        return {
            "entity": entity.model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("entity_create"),
        }


def update_quest_action(
    content_root: str | Path,
    *,
    quest_id: str,
    title: str | None = None,
    objective: str | None = None,
    timeline_order: int | None = None,
    set_timeline_order: bool = False,
    prerequisites: list[str] | None = None,
    giver_npc: str | None = None,
    location: str | None = None,
    metadata_updates: dict[str, Any] | None = None,
    if_match: str | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Edit a quest's fields in place (drag-to-reorder sets ``timeline_order``).

    ``set_timeline_order`` distinguishes "set the order (possibly to None = unsequenced)" from
    "leave it unchanged", since ``timeline_order=None`` is itself a meaningful value.
    ``if_match`` is an optimistic-concurrency etag (WS-B): a stale one means another author edited
    first, so the write is refused rather than clobbering theirs."""
    from ..collab import ConflictError, check_etag, etag_for

    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        quest = bundle.quests.get(quest_id)
        if quest is None:
            raise ValueError(f"任务不存在：{quest_id}")
        try:
            check_etag(current=etag_for(quest), if_match=if_match)
        except ConflictError as exc:
            raise ValueError(str(exc)) from exc
        update: dict[str, Any] = {}
        if title is not None and title.strip():
            update["title"] = title.strip()
        if objective is not None:
            update["objective"] = objective.strip()
        if set_timeline_order:
            update["timeline_order"] = timeline_order
        if prerequisites is not None:
            update["prerequisites"] = [p for p in prerequisites if p in bundle.quests]
        if giver_npc is not None:
            update["giver_npc"] = giver_npc or None
        if location is not None:
            update["location"] = location or None
        if metadata_updates:
            update["metadata"] = _merge_metadata(quest.metadata, metadata_updates)
        if update:
            bundle.quests[quest_id] = quest.model_copy(update=update)
            project.content_store.save(bundle)
            project.reload()
        return {
            "quest": bundle.quests[quest_id].model_dump(mode="json"),
            "changed": sorted(update),
            "etag": etag_for(bundle.quests[quest_id]),
            "cost_budget": _deterministic_cost_budget("quest_update"),
        }


# >>> WS-B collaboration (assignments / comments / locks / concurrency) >>>
def collab_state_action(
    content_root: str | Path,
    *,
    object_ref: str | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """The collaboration ledger (assignments/comments/locks), optionally scoped to one object."""
    from ..collab import CollabStore

    with _project(content_root, sqlite_path) as project:
        state = CollabStore(project.content_store.root).load()
        if object_ref is not None:
            return {
                "object_ref": object_ref,
                "assignment": (
                    state.assignments[object_ref].model_dump(mode="json")
                    if object_ref in state.assignments
                    else None
                ),
                "comments": [c.model_dump(mode="json") for c in state.comments.get(object_ref, [])],
                "lock": (
                    state.locks[object_ref].model_dump(mode="json")
                    if object_ref in state.locks
                    else None
                ),
            }
        return state.model_dump(mode="json")


def assign_action(
    content_root: str | Path,
    *,
    object_ref: str,
    assignee: str,
    by: str,
    note: str = "",
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ..collab import CollabStore, assign, unassign

    with _project(content_root, sqlite_path) as project:
        store = CollabStore(project.content_store.root)
        state = store.load()
        if assignee.strip():
            assign(state, object_ref=object_ref, assignee=assignee, by=by, note=note)
        else:
            unassign(state, object_ref=object_ref)
        store.save(state)
        return {"object_ref": object_ref, "assignee": assignee.strip()}


def comment_action(
    content_root: str | Path,
    *,
    object_ref: str,
    author: str,
    body: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ..collab import CollabStore, add_comment

    with _project(content_root, sqlite_path) as project:
        store = CollabStore(project.content_store.root)
        state = store.load()
        comment = add_comment(state, object_ref=object_ref, author=author, body=body)
        store.save(state)
        return {"comment": comment.model_dump(mode="json")}


def lock_action(
    content_root: str | Path,
    *,
    object_ref: str,
    holder: str,
    release: bool = False,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ..collab import CollabStore, ConflictError, acquire_lock, release_lock

    with _project(content_root, sqlite_path) as project:
        store = CollabStore(project.content_store.root)
        state = store.load()
        if release:
            release_lock(state, object_ref=object_ref, holder=holder)
            store.save(state)
            return {"object_ref": object_ref, "locked": False}
        try:
            lock = acquire_lock(state, object_ref=object_ref, holder=holder)
        except ConflictError as exc:
            raise ValueError(str(exc)) from exc
        store.save(state)
        return {"object_ref": object_ref, "locked": True, "lock": lock.model_dump(mode="json")}


# <<< WS-B collaboration <<<


def world_analytics_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """WS-H · deterministic world analytics dashboard (counts/density/gaps/coverage). $0."""
    from .analytics import build_world_analytics

    with _project(content_root, sqlite_path) as project:
        return {
            "analytics": build_world_analytics(project.bundle),
            "cost_budget": _deterministic_cost_budget("world_analytics"),
        }


def list_templates_action() -> dict[str, Any]:
    """WS-G · the built-in template / archetype library."""
    from ..templates import list_templates

    return {"templates": [t.model_dump(mode="json") for t in list_templates()]}


def instantiate_template_action(
    content_root: str | Path,
    *,
    template_id: str,
    params: dict[str, Any],
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Stamp out content from a template + params (deterministic) and route it through the review
    queue — a human still signs off, like any draft."""
    from ..templates import instantiate

    with _project(content_root, sqlite_path) as project:
        bundle = project.bundle
        existing = (
            set(bundle.entities)
            | set(bundle.quests)
            | set(bundle.regions)
            | set(bundle.pois)
            | set(bundle.terms)
            | set(bundle.dialogues)
            | set(bundle.dialogue_trees)
        )
        seed = instantiate(template_id, params, existing_ids=existing)
        item = ReviewQueue(project.sqlite_store).add_world_seed(
            {"id": f"template_{template_id}", "bundle": seed.model_dump(mode="json")}
        )
        return {
            "review_item_id": item.id,
            "created": {
                "quests": sorted(seed.quests),
                "entities": sorted(seed.entities),
            },
            "cost_budget": _deterministic_cost_budget("template_instantiate"),
        }


def localization_overview_action(
    content_root: str | Path,
    *,
    locales: list[str] | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """WS-F · localization coverage + per-string status (待译/已译/待校/定稿). $0."""
    from ..localization import LocStore, build_localization_overview

    with _project(content_root, sqlite_path) as project:
        state = LocStore(project.content_store.root).load()
        return {
            "overview": build_localization_overview(project.bundle, state, locales=locales),
            "cost_budget": _deterministic_cost_budget("localization_overview"),
        }


def loc_transition_action(
    content_root: str | Path,
    *,
    text_key: str,
    locale: str,
    to: str,
    by: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ..localization import LocStatus, LocStore, transition

    with _project(content_root, sqlite_path) as project:
        store = LocStore(project.content_store.root)
        state = store.load()
        entry = transition(state, text_key=text_key, locale=locale, to=LocStatus(to), by=by)
        store.save(state)
        return {"entry": entry.model_dump(mode="json")}


def loc_assign_action(
    content_root: str | Path,
    *,
    text_key: str,
    locale: str,
    assignee: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ..localization import LocStore, assign

    with _project(content_root, sqlite_path) as project:
        store = LocStore(project.content_store.root)
        state = store.load()
        entry = assign(state, text_key=text_key, locale=locale, assignee=assignee)
        store.save(state)
        return {"entry": entry.model_dump(mode="json")}


# >>> WS-I asset linking >>>
def asset_list_action(
    content_root: str | Path,
    *,
    object_ref: str | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ..assets import AssetStore

    with _project(content_root, sqlite_path) as project:
        state = AssetStore(project.content_store.root).load()
        if object_ref is not None:
            return {
                "object_ref": object_ref,
                "assets": [a.model_dump(mode="json") for a in state.assets.get(object_ref, [])],
            }
        return {"assets": state.model_dump(mode="json")["assets"]}


def _object_ref_exists(bundle: Any, object_ref: str) -> bool:
    """Does `kind:id` point at an object that actually exists in canon? Guards against orphan
    assets attached to a typo'd / deleted object_ref."""
    kind, sep, oid = object_ref.partition(":")
    if not sep or not oid:
        return False
    table = {
        "entity": bundle.entities,
        "quest": bundle.quests,
        "region": bundle.regions,
        "poi": bundle.pois,
        "term": bundle.terms,
        "dialogue": bundle.dialogue_trees,
        "dialogue_tree": bundle.dialogue_trees,
    }.get(kind)
    return table is not None and oid in table


def asset_attach_action(
    content_root: str | Path,
    *,
    object_ref: str,
    kind: str,
    uri: str,
    title: str = "",
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ..assets import AssetKind, AssetStore, attach

    with _project(content_root, sqlite_path) as project:
        if not _object_ref_exists(project.bundle, object_ref):
            raise ValueError(f"找不到要挂接的对象「{object_ref}」，请先确认它已在正典中。")
        store = AssetStore(project.content_store.root)
        state = store.load()
        asset = attach(state, object_ref=object_ref, kind=AssetKind(kind), uri=uri, title=title)
        store.save(state)
        return {"asset": asset.model_dump(mode="json")}


def asset_detach_action(
    content_root: str | Path,
    *,
    asset_id: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ..assets import AssetStore, detach

    with _project(content_root, sqlite_path) as project:
        store = AssetStore(project.content_store.root)
        state = store.load()
        removed = detach(state, asset_id=asset_id)
        store.save(state)
        return {"removed": removed}


# <<< WS-I asset linking <<<


def import_from_engine_action(
    content_root: str | Path,
    *,
    quests: list[dict[str, Any]],
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """WS-K · pull engine-side quest rows back: diff vs canon, queue new/changed for review."""
    from .engine_sync import plan_engine_import, staged_bundle

    with _project(content_root, sqlite_path) as project:
        plan = plan_engine_import(quests, project.bundle)
        review_item_id = None
        staged = staged_bundle(plan)
        if staged.quests:
            item = ReviewQueue(project.sqlite_store).add_world_seed(
                {"id": "engine_import", "bundle": staged.model_dump(mode="json")}
            )
            review_item_id = item.id
        return {
            "new": plan["new"],
            "changed": plan["changed"],
            "unchanged": plan["unchanged"],
            "review_item_id": review_item_id,
            "cost_budget": _deterministic_cost_budget("engine_import"),
        }


# >>> WS-R recognize foreign project files (tables / articy) → editable plan → review >>>
def _read_table_rows(input_path: str | Path) -> list[dict[str, Any]]:
    """Read a spreadsheet into raw header-keyed rows, reusing the existing tolerant importers
    (GB18030/Chinese-header CSV, openpyxl XLSX, JSON array). Headers are kept verbatim so the
    column-mapping step sees exactly what the planner wrote."""
    path = Path(input_path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        from ..content.importers.csv import CSVImporter

        return [dict(obj.data) for obj in CSVImporter().parse(path)]
    if suffix in {".xlsx", ".xlsm"}:
        from ..content.importers.xlsx import XLSXImporter

        return [dict(obj.data) for obj in XLSXImporter().parse(path)]
    if suffix == ".json":
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [dict(row) for row in data if isinstance(row, dict)]
        raise ValueError("表格 JSON 需为对象数组（list[dict]）")
    raise ValueError(f"不支持的表格文件类型：{suffix or path.name}（支持 .csv/.xlsx/.json）")


_RECOGNIZE_FORMATS = ("table", "articy", "ink", "yarn", "ue", "unity")


def _table_rows_from_text(text: str) -> list[dict[str, Any]]:
    """Parse pasted/uploaded table content: a JSON array of objects, or CSV text."""
    import csv
    import io
    import json

    if text.lstrip().startswith("["):
        data = json.loads(text)
        return [dict(row) for row in data if isinstance(row, dict)]
    reader = csv.DictReader(io.StringIO(text))
    return [{str(k): (v if v is not None else "") for k, v in row.items() if k} for row in reader]


def _recognize_plan(
    project: Any,
    source_format: str,
    *,
    source_name: str,
    rows: list[dict[str, Any]] | None = None,
    text: str | None = None,
    data: Any = None,
    field_mapping: dict[str, Any] | None = None,
    llm_proposer: Any = None,
) -> Any:
    """Build the editable ImportPlan from an already-loaded payload (shared by path + content)."""
    from ..recognize import ColumnMapping, recognize

    canon_ids = list(project.bundle.entities.keys())
    common = {
        "source_file": source_name,
        "enable_llm": llm_proposer is not None,
        "llm_proposer": llm_proposer,
    }
    if source_format == "table":
        mapping = ColumnMapping.model_validate(field_mapping) if field_mapping else None
        return recognize("table", rows=rows or [], mapping=mapping, canon_ids=canon_ids, **common)
    if source_format == "articy":
        return recognize("articy", articy_data=data, **common)
    if source_format in {"ink", "yarn"}:
        return recognize(source_format, text=text or "", llm_text=text or "", **common)
    if source_format in {"ue", "unity"}:
        return recognize(source_format, engine_data=data, canon_ids=canon_ids, **common)
    raise ValueError(
        f"暂不支持的来源格式：{source_format}（支持：{', '.join(_RECOGNIZE_FORMATS)}）"
    )


def _recognize_finish(project: Any, plan: Any, *, apply: bool) -> dict[str, Any]:
    """Diff vs canon, build the response, and (on apply) stage new/changed + audit preview."""
    from ..recognize import diff_against_canon, plan_to_bundle

    plan = diff_against_canon(plan, project.bundle)
    result: dict[str, Any] = {
        "source_format": plan.source_format,
        "summary": plan.summary(),
        "plan": plan.model_dump(mode="json"),
        "new": plan.new,
        "changed": plan.changed,
        "unchanged": plan.unchanged,
        "warnings": plan.warnings,
        "applied": False,
        "cost_budget": _deterministic_cost_budget("recognize_import"),
    }
    if not apply:
        return result

    staged = plan_to_bundle(plan, only_ids=set(plan.new) | set(plan.changed))
    review_item_id = None
    if staged.entities or staged.relations:
        item = ReviewQueue(project.sqlite_store).add_world_seed(
            {"id": "recognition_import", "bundle": staged.model_dump(mode="json")}
        )
        review_item_id = item.id
    preview = project.audit_runner.run(
        AuditContext.from_bundle(_merge_preview(project.bundle, staged))
    )
    result.update(
        {
            "applied": True,
            "review_item_id": review_item_id,
            "audit_preview": {
                "totals": preview.run.totals,
                "issues": [
                    {
                        "rule_code": issue.rule_code,
                        "severity": issue.severity.value,
                        "target_ref": issue.target_ref,
                        "message": issue.message,
                    }
                    for issue in preview.issues[:20]
                ],
            },
        }
    )
    return result


def recognize_import_action(
    content_root: str | Path,
    *,
    source_format: str,
    input_path: str,
    field_mapping: dict[str, Any] | None = None,
    apply: bool = False,
    operator: str = "import",
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """WS-R · read a foreign project FILE on disk and recognize it into an editable ImportPlan
    (see ``_recognize_finish``). Recognition never auto-lands; new/changed go to human review."""
    import json

    with _project(content_root, sqlite_path) as project:
        source_name = Path(input_path).name
        rows = _read_table_rows(input_path) if source_format == "table" else None
        text = (
            Path(input_path).read_text(encoding="utf-8")
            if source_format in {"ink", "yarn"}
            else None
        )
        data = (
            json.loads(Path(input_path).read_text(encoding="utf-8"))
            if source_format in {"articy", "ue", "unity"}
            else None
        )
        plan = _recognize_plan(
            project,
            source_format,
            source_name=source_name,
            rows=rows,
            text=text,
            data=data,
            field_mapping=field_mapping,
        )
        return _recognize_finish(project, plan, apply=apply)


def _rows_from_upload(raw: bytes, filename: str) -> list[dict[str, Any]]:
    """Parse uploaded table bytes (CSV/XLSX/JSON) by writing a temp file with the right suffix and
    reusing the encoding-tolerant importers — so a GB18030 CSV or binary .xlsx parses correctly."""
    import tempfile

    suffix = Path(filename).suffix.lower() or ".csv"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(raw)
        tmp = handle.name
    try:
        return _read_table_rows(tmp)
    finally:
        Path(tmp).unlink(missing_ok=True)


def recognize_content_action(
    content_root: str | Path,
    *,
    source_format: str,
    content: str | None = None,
    content_base64: str | None = None,
    filename: str = "upload",
    field_mapping: dict[str, Any] | None = None,
    apply: bool = False,
    enable_llm: bool = False,
    llm_mode: str = "real",
    llm_model: str = "deepseek-v4-flash",
    operator: str = "import",
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """WS-R · recognize from inline content (the REST/frontend path; server reads no local path).

    ``content`` is decoded text (pasted) or ``content_base64`` is raw file bytes (uploaded — needed
    for binary .xlsx and for non-UTF-8 CSV). ``source_format='auto'`` sniffs the format from the
    filename + a content sample (always overridable). ``enable_llm`` turns on the §8-guarded
    LLM relation pass (default off; needs a connected model under ``llm_mode='real'``)."""
    import base64
    import json

    from ..content.encoding import decode_bytes
    from ..recognize import (
        OfflineRelationProvider,
        build_llm_relation_proposer,
        sniff_source_format,
    )

    raw = base64.b64decode(content_base64) if content_base64 else None
    text_all = content if content is not None else (decode_bytes(raw) if raw is not None else "")
    fmt = source_format
    if fmt == "auto":
        fmt = sniff_source_format(filename, text_all[:4096])

    with _project(content_root, sqlite_path) as project:
        # Built inside the project block so the gateway cache is scoped to this project (the
        # namespace contextvar is only set within `_project`).
        llm_proposer = None
        if enable_llm:
            gateway, _ = _gateway(
                task="recognize_relations",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=OfflineRelationProvider(),
            )
            llm_proposer = build_llm_relation_proposer(gateway)

        rows: list[dict[str, Any]] | None = None
        text: str | None = None
        data: Any = None
        if fmt == "table":
            rows = (
                _rows_from_upload(raw, filename)
                if raw is not None
                else _table_rows_from_text(text_all)
            )
        elif fmt in {"ink", "yarn"}:
            text = text_all
        elif fmt in {"articy", "ue", "unity"}:
            data = json.loads(text_all)
        plan = _recognize_plan(
            project,
            fmt,
            source_name=filename,
            rows=rows,
            text=text,
            data=data,
            field_mapping=field_mapping,
            llm_proposer=llm_proposer,
        )
        return _recognize_finish(project, plan, apply=apply)


def recognize_apply_plan_action(
    content_root: str | Path,
    *,
    plan: dict[str, Any],
    operator: str = "import",
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """WS-R · stage an EDITED ImportPlan into review. The UI sends back the plan after the human has
    dropped wrong proposals / fixed fields, so exactly the kept entities + relations get queued."""
    from ..recognize import ImportPlan

    with _project(content_root, sqlite_path) as project:
        parsed = ImportPlan.model_validate(plan)
        return _recognize_finish(project, parsed, apply=True)


def _mapping_store_path(content_root: str | Path) -> Path:
    return Path(content_root) / ".owcopilot" / "recognize_mappings.json"


def _read_mappings(content_root: str | Path) -> dict[str, Any]:
    import json

    path = _mapping_store_path(content_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_mappings(content_root: str | Path, data: dict[str, Any]) -> None:
    import json

    path = _mapping_store_path(content_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_mapping_templates_action(
    content_root: str | Path, *, sqlite_path: str | None = None
) -> dict[str, Any]:
    """Project-local saved column mappings (same-studio table schemas are stable, so reuse pays)."""
    return {"templates": _read_mappings(content_root)}


def save_mapping_template_action(
    content_root: str | Path, *, name: str, mapping: dict[str, Any], sqlite_path: str | None = None
) -> dict[str, Any]:
    from ..recognize import ColumnMapping

    if not name.strip():
        raise ValueError("模板名不能为空")
    validated = ColumnMapping.model_validate(mapping).model_dump(mode="json")
    data = _read_mappings(content_root)
    data[name] = validated
    _write_mappings(content_root, data)
    return {"templates": data, "saved": name}


def delete_mapping_template_action(
    content_root: str | Path, *, name: str, sqlite_path: str | None = None
) -> dict[str, Any]:
    data = _read_mappings(content_root)
    data.pop(name, None)
    _write_mappings(content_root, data)
    return {"templates": data, "deleted": name}


# >>> WS-A logic endpoints (human-edit pipeline; logic audits surface via the normal audit) >>>
def update_quest_logic_action(
    content_root: str | Path,
    *,
    quest_id: str,
    logic: dict[str, Any] | None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Set/replace a quest's native logic layer (None clears it). Malformed expressions are rejected
    up front (clean 4xx, not a 500); the saved logic's deterministic issues are returned so the
    editor sees unreachable stages / deadlocks / undefined vars immediately."""
    from ..content.models import QuestLogic
    from ..logic import LogicSyntaxError, audit_quest_logic, parse_expr

    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        quest = bundle.quests.get(quest_id)
        if quest is None:
            raise ValueError(f"任务不存在：{quest_id}")
        parsed_logic: QuestLogic | None = None
        if logic is not None:
            try:
                parsed_logic = QuestLogic.model_validate(logic)
            except ValueError as exc:
                raise ValueError(f"逻辑结构不合法：{exc}") from exc
            for source in _logic_expression_sources(parsed_logic):
                try:
                    parse_expr(source)
                except LogicSyntaxError as exc:
                    raise ValueError(f"逻辑表达式无法解析「{source}」：{exc}") from exc
        bundle.quests[quest_id] = quest.model_copy(update={"logic": parsed_logic})
        project.content_store.save(bundle)
        project.reload()
        updated = bundle.quests[quest_id]
        issues = audit_quest_logic(updated)
        return {
            "quest": updated.model_dump(mode="json"),
            "logic_issues": [{"code": i.code, "message": i.message, "ref": i.ref} for i in issues],
            "cost_budget": _deterministic_cost_budget("quest_logic_update"),
        }


def _logic_expression_sources(logic: Any) -> list[str]:
    sources = [logic.precondition, *(s.precondition for s in logic.stage_logic)]
    sources += [b.condition for b in logic.branches]
    return [s for s in sources if isinstance(s, str) and s.strip()]


def draft_quest_logic_action(
    content_root: str | Path,
    *,
    quest_id: str,
    intent: str = "",
    refine_rounds: int = 2,
    sqlite_path: str | None = None,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
) -> dict[str, Any]:
    """B7: draft a quest's logic layer with the model, gated by the deterministic logic audit, then
    queue it for human review (HITL — it never lands automatically). The audit runs every round, so
    the reviewer sees logic already machine-checked for deadlocks / unreachable stages / undefined
    vars."""
    from ..assist.offline import OfflineLogicDraftProvider
    from ..logic.draft import LOGIC_DRAFT_TASK, draft_quest_logic

    with _project(content_root, sqlite_path) as project:
        quest = project.bundle.quests.get(quest_id)
        if quest is None:
            raise ValueError(f"任务不存在：{quest_id}")
        gateway, telemetry = _gateway(
            task=LOGIC_DRAFT_TASK,
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineLogicDraftProvider(),
        )
        result = draft_quest_logic(
            gateway=gateway, quest=quest, intent=intent, max_rounds=max(0, refine_rounds) + 1
        )
        critic_verdict, critic_score = critic_from_trail(
            [r.model_dump(mode="json") for r in result.trail]
        )
        issue_refs = [f"{i.code}:{i.ref}" for i in result.issues]
        item = ReviewQueue(project.sqlite_store).add_quest_logic_draft(
            {
                "quest_id": quest_id,
                "quest_title": quest.title,
                "logic": result.logic.model_dump(mode="json"),
            },
            issue_refs=issue_refs,
            critic_verdict=critic_verdict,
            critic_score=critic_score,
        )
        telemetry_summary = telemetry.summary()
        return {
            "quest_id": quest_id,
            "logic": result.logic.model_dump(mode="json"),
            "logic_issues": [
                {"code": i.code, "message": i.message, "ref": i.ref} for i in result.issues
            ],
            "refine_trail": [r.model_dump(mode="json") for r in result.trail],
            "auto_review_incomplete": result.auto_review_incomplete,
            "review_item_id": item.id,
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step("draft_quest_logic", telemetry_summary)]
            ).budget.model_dump(mode="json"),
        }


def simulate_quest_action(
    content_root: str | Path,
    *,
    quest_id: str,
    choices: list[str] | None = None,
    initial_state: dict[str, Any] | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """WS-E playtest: deterministically walk a quest's logic and report the path + outcome."""
    from ..logic import simulate_quest

    with _project(content_root, sqlite_path) as project:
        quest = project.bundle.quests.get(quest_id)
        if quest is None:
            raise ValueError(f"任务不存在：{quest_id}")
        run = simulate_quest(quest, choices=choices, initial_state=initial_state)
        return {
            "quest_id": quest_id,
            "run": run.model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("quest_simulate"),
        }


# <<< WS-A logic endpoints <<<


# >>> WS-C search + safe refactor >>>
def search_all_action(
    content_root: str | Path,
    *,
    query: str,
    kinds: list[str] | None = None,
    limit: int = 30,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Global literal search across the canon (jump-to). Deterministic, $0."""
    from ..search import search_all

    with _project(content_root, sqlite_path) as project:
        hits = search_all(project.bundle, query, kinds=set(kinds) if kinds else None, limit=limit)
        return {
            "query": query,
            "hits": [hit.model_dump(mode="json") for hit in hits],
            "cost_budget": _deterministic_cost_budget("search"),
        }


def plan_rename_action(
    content_root: str | Path,
    *,
    ref: str,
    new_name: str | None = None,
    new_id: str | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Dry-run a rename: the edits it would make + any conflicts. Mutates nothing."""
    from ..content.refactor import plan_rename

    with _project(content_root, sqlite_path) as project:
        plan = plan_rename(project.bundle, ref=ref, new_name=new_name, new_id=new_id)
        return {
            "plan": plan.model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("rename_plan"),
        }


def apply_rename_action(
    content_root: str | Path,
    *,
    ref: str,
    operator: str,
    new_name: str | None = None,
    new_id: str | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Apply a rename atomically: snapshot (undo point) -> rewrite every reference -> save -> reload
    -> re-audit (must stay clean). Returns the snapshot id so the caller can undo."""
    from ..content.refactor import apply_rename, plan_rename
    from ..content.snapshot import write_snapshot

    if not operator.strip():
        raise ValueError("请先填写署名")
    with _project(content_root, sqlite_path) as project:
        plan = plan_rename(project.bundle, ref=ref, new_name=new_name, new_id=new_id)
        if plan.conflicts:
            raise ValueError("；".join(plan.conflicts))
        snapshot = write_snapshot(project.content_store, label=f"rename {ref} by {operator}")
        renamed = apply_rename(project.bundle, plan)
        project.content_store.save(renamed)
        project.reload()
        audit = run_full_audit(project, persist=True)
        return {
            "plan": plan.model_dump(mode="json"),
            "undo_snapshot_id": snapshot.id,
            "post_audit_open_errors": len(audit.open_errors),
            "cost_budget": _deterministic_cost_budget("rename_apply"),
        }


def restore_snapshot_action(
    content_root: str | Path,
    *,
    snapshot_id: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Undo by restoring a snapshot (e.g. the undo point a rename returned)."""
    from ..content.snapshot import load_snapshot

    with _project(content_root, sqlite_path) as project:
        bundle = load_snapshot(project.content_store, snapshot_id)
        if bundle is None:
            raise ValueError(f"快照不存在：{snapshot_id}")
        project.content_store.save(bundle)
        project.reload()
        return {
            "restored": snapshot_id,
            "cost_budget": _deterministic_cost_budget("snapshot_restore"),
        }


# <<< WS-C search + safe refactor <<<


def add_relation_action(
    content_root: str | Path,
    *,
    source: str,
    target: str,
    kind: str,
    symmetric: bool | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Add a typed relation between two objects (dedup; symmetric kinds are peer/undirected)."""
    kind = kind.strip()
    if not kind:
        raise ValueError("关系类型不能为空")
    if source == target:
        raise ValueError("不能给同一个对象连自身")
    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        if not _object_exists(bundle, source) or not _object_exists(bundle, target):
            raise ValueError("关系两端必须都是已存在的对象")
        exists = any(
            r.source == source and r.target == target and r.kind == kind for r in bundle.relations
        )
        if not exists:
            is_sym = is_symmetric_kind(kind) if symmetric is None else symmetric
            metadata = {"symmetric": True} if is_sym else {}
            bundle.relations.append(
                Relation(source=source, target=target, kind=kind, metadata=metadata)
            )
            project.content_store.save(bundle)
            project.reload()
        return {
            "relation": {"source": source, "target": target, "kind": kind},
            "added": not exists,
            "cost_budget": _deterministic_cost_budget("relation_add"),
        }


def remove_relation_action(
    content_root: str | Path,
    *,
    source: str,
    target: str,
    kind: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Remove a typed relation (exact source/kind/target match)."""
    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        before = len(bundle.relations)
        bundle.relations = [
            r
            for r in bundle.relations
            if not (r.source == source and r.target == target and r.kind == kind)
        ]
        removed = before - len(bundle.relations)
        if removed:
            project.content_store.save(bundle)
            project.reload()
        return {"removed": removed, "cost_budget": _deterministic_cost_budget("relation_remove")}


def update_dialogue_tree_action(
    content_root: str | Path,
    *,
    tree_id: str,
    title: str | None = None,
    root_node: str | None = None,
    nodes: dict[str, Any] | None = None,
    metadata_updates: dict[str, Any] | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Replace a dialogue tree's editable fields (nodes map / root / title), validated and saved."""
    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        tree = bundle.dialogue_trees.get(tree_id)
        if tree is None:
            raise ValueError(f"对话树不存在：{tree_id}")
        update: dict[str, Any] = {}
        if title is not None:
            update["title"] = title.strip()
        if nodes is not None:
            try:
                parsed = {nid: DialogueNode.model_validate(node) for nid, node in nodes.items()}
            except ValidationError as exc:
                # malformed node shape from the editor is bad input, not a server fault → clean 400
                raise ValueError(f"对话节点格式不合法：{exc.error_count()} 处问题") from exc
            update["nodes"] = parsed
        new_nodes = update.get("nodes", tree.nodes)
        if root_node is not None:
            update["root_node"] = root_node
        # Validate the EFFECTIVE root against the (possibly replaced) node map — not only a root the
        # caller passed this time. Replacing `nodes` while dropping/renaming the existing root id
        # must not silently persist a tree whose root points at a node that no longer exists.
        effective_root = root_node if root_node is not None else tree.root_node
        if effective_root and new_nodes and effective_root not in new_nodes:
            raise ValueError(f"根节点不存在：{effective_root}")
        if metadata_updates:
            update["metadata"] = _merge_metadata(tree.metadata, metadata_updates)
        if update:
            bundle.dialogue_trees[tree_id] = tree.model_copy(update=update)
            project.content_store.save(bundle)
            project.reload()
        return {
            "tree": bundle.dialogue_trees[tree_id].model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("dialogue_tree_update"),
        }


def set_object_position_action(
    content_root: str | Path,
    *,
    ref: str,
    x: float,
    y: float,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Persist a dragged node's position into the object's ``metadata.graph_pos`` (the layout uses
    the override when present, else falls back to the deterministic layout)."""
    object_type, _, object_id = ref.partition(":")
    pos = [round(float(x), 1), round(float(y), 1)]
    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        collections: dict[str, MutableMapping[str, Any]] = {
            "entity": bundle.entities,
            "quest": bundle.quests,
            "poi": bundle.pois,
            "region": bundle.regions,
        }
        collection = collections.get(object_type)
        if collection is None or object_id not in collection:
            raise ValueError(f"对象不存在：{ref}")
        obj = collection[object_id]
        collection[object_id] = obj.model_copy(
            update={"metadata": {**obj.metadata, "graph_pos": pos}}
        )
        project.content_store.save(bundle)
        project.reload()
        return {
            "ref": ref,
            "graph_pos": pos,
            "cost_budget": _deterministic_cost_budget("node_move"),
        }


def _object_exists(bundle: ContentBundle, object_id: str) -> bool:
    return (
        object_id in bundle.entities
        or object_id in bundle.quests
        or object_id in bundle.pois
        or object_id in bundle.regions
        # terms are first-class graph nodes too (the index adds them, the audit treats them as
        # objects), so a relation to/from a term is legitimate and must not be rejected as unknown.
        or object_id in bundle.terms
    )


def _merge_metadata(current: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    for key, value in updates.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def probe_llm_connection_action(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 12.0,
    provider: Any | None = None,
) -> dict[str, Any]:
    """Probe the configured provider with a minimal completion (BYO-key onboarding).

    The key is used in-process only; env vars are restored afterwards. `provider` is
    injectable so tests run offline.
    """
    import time

    from ..llm.gateway import _classify_provider_error

    overrides = {"OPENAI_BASE_URL": base_url.strip(), "OPENAI_API_KEY": api_key.strip()}
    saved = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        probe = provider or OpenAICompatProvider(model=model, timeout=timeout, max_output_tokens=64)
        started = time.perf_counter()
        result = probe.complete(
            system="You are a connectivity probe.",
            user="Reply with the single word: pong",
            model="cheap",
        )
        latency_ms = (time.perf_counter() - started) * 1000
        text = str(result[0]) if result else ""
        return {
            "ok": True,
            "model": model,
            "latency_ms": round(latency_ms, 1),
            "sample": text[:60],
        }
    except ModuleNotFoundError:
        return {
            "ok": False,
            "category": "missing_dependency",
            "message": "未安装真实模型依赖：pip install owcopilot[live]",
        }
    except Exception as e:  # classified, never raised: the UI shows a friendly verdict
        return {
            "ok": False,
            "category": _classify_provider_error(e),
            "message": str(e)[:200],
        }
    finally:
        for env_key, previous in saved.items():
            if previous is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = previous


def run_lorebook_export_action(
    content_root: str | Path,
    *,
    output_dir: str | Path,
    formats: tuple[str, ...] = ("md", "docx"),
    title: str = "世界设定集",
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Export the readable lore book (creator-facing deliverable, deterministic)."""
    with _project(content_root, sqlite_path) as project:
        files = write_lorebook(project.bundle, output_dir, title=title, formats=formats)
        return {
            "output_dir": str(Path(output_dir)),
            "files": files,
            "cost_budget": _deterministic_cost_budget("lorebook_export"),
        }


def run_prose_check_action(
    content_root: str | Path,
    *,
    text: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Check a pasted chapter against the archive: mentions, forbidden terms, unknowns."""
    with _project(content_root, sqlite_path) as project:
        report = check_prose(text, project.bundle)
        return {
            "resolved_mentions": [m.model_dump(mode="json") for m in report.resolved_mentions],
            "issues": [issue.model_dump(mode="json") for issue in report.issues],
            "stats": report.stats,
            "cost_budget": _deterministic_cost_budget("prose_check"),
        }
