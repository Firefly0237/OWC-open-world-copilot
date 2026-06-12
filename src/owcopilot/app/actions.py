"""UI actions that execute project workflow steps without importing Streamlit.

Each action opens the project, delegates to the same `pipeline/*` workflows the CLI and REST
layers use, and returns a plain JSON-able dict. Keeping this layer Streamlit-free means the
whole Workbench behaviour is unit-testable in core CI, and the dashboard file stays a thin shell.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..assist.barks import BarkBatchService
from ..assist.dialogue_trees import DialogueTreeService, OfflineDialogueTreeProvider
from ..assist.drafts import QuestDraftService
from ..assist.flavor import FlavorBatchService, OfflineFlavorProvider
from ..assist.offline import OfflineBarksProvider, OfflineQuestDraftProvider
from ..assist.prose_check import check_prose
from ..assist.review_queue import ReviewQueue
from ..audit.baseline import issue_fingerprint
from ..audit.context import AuditContext
from ..audit.report import render_audit_markdown
from ..content.hash import content_hash
from ..content.models import ContentBundle
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
from ..llm.gateway import LLMGateway, OpenAICompatProvider
from ..llm.router import StaticRouter
from ..llm.telemetry import TelemetryCollector
from ..pipeline.audit import run_full_audit
from ..pipeline.ingest import run_ingest
from ..pipeline.patches import (
    apply_patch_workflow,
    find_issue,
    rollback_patch_workflow,
    suggest_for_issue,
)
from ..pipeline.project import ProjectContext
from ..pipeline.review import decide_review_item
from ..qa.offline import OfflineQAProvider
from ..qa.service import LoreQAService
from ..telemetry import deterministic_step, llm_step, summarize_workflow
from ..util import load_dotenv
from ..worldgen import OfflineWorldSeedProvider, WorldSeedBrief, WorldSeedService


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
        )
        answer = LoreQAService(
            gateway=gateway,
            context_builder=project.context_builder,
            bundle=project.bundle,
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
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        gateway, telemetry = _gateway(
            task="quest_draft",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineQuestDraftProvider(),
        )
        result = QuestDraftService(
            gateway=gateway,
            context_builder=project.context_builder,
            audit_runner=project.audit_runner,
            bundle=project.bundle,
        ).draft_quest(brief, budget_tokens=budget_tokens)
        item = ReviewQueue(project.sqlite_store).add_quest_draft(
            result.quest.model_dump(mode="json", exclude_none=True),
            issue_refs=[issue_fingerprint(issue) for issue in result.issues],
        )
        telemetry_summary = telemetry.summary()
        return {
            "quest": result.quest.model_dump(mode="json", exclude_none=True),
            "issues": [issue.model_dump(mode="json") for issue in result.issues],
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
) -> dict[str, Any]:
    parsed = WorldSeedBrief.model_validate(brief)
    with _project(content_root, sqlite_path) as project:
        gateway, telemetry = _gateway(
            task="world_seed",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineWorldSeedProvider(),
        )
        draft = WorldSeedService(
            gateway=gateway,
            bundle=project.bundle,
            project_context_builder=project.context_builder,
            reference_context_builder=project.reference_context_builder,
        ).generate(parsed, budget_tokens=budget_tokens)
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
            "issues": [issue.model_dump(mode="json") for issue in issues],
            "review_item_id": item.id,
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step("world_seed", telemetry_summary)]
            ).budget.model_dump(mode="json"),
        }


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
        manifest = export_content_bundle(project.bundle, actual_output, target_engine=engine)
        return {
            "output_dir": str(actual_output),
            "manifest": manifest.model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("export_project"),
        }


_ACTION_CACHE: CacheBackend | None = None


def _action_cache() -> CacheBackend:
    """One process-lifetime L1/L2 cache shared by every real-mode action gateway.

    Same assembly as the REST service (`service.api._build_cache`): without it, every
    Workbench rerun built a throwaway gateway with NoOpCache and a user asking the same
    question twice paid twice. Offline mode stays uncached on purpose — it is the test
    substrate, and cross-test result sharing would couple unrelated tests.
    """
    global _ACTION_CACHE
    if _ACTION_CACHE is None:
        _ACTION_CACHE = build_cache_backend("exact+semantic")
    return _ACTION_CACHE


def _gateway(
    *, task: str, llm_mode: str, llm_model: str, offline_provider: Any
) -> tuple[LLMGateway, TelemetryCollector]:
    telemetry = TelemetryCollector()
    real = llm_mode == "real"
    if real:
        load_dotenv()  # pick up provider keys from .env; shell env wins
        provider: Any = OpenAICompatProvider(model=llm_model)
    else:
        provider = offline_provider
    gateway = LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={task: "cheap"}),
        cache=_action_cache() if real else NoOpCache(),
        telemetry=telemetry,
        max_retries=1 if real else 0,
        retry_backoff_seconds=1.0 if real else 0.0,
    )
    return gateway, telemetry


@contextmanager
def _project(content_root: str | Path, sqlite_path: str | None) -> Iterator[ProjectContext]:
    root = Path(content_root)
    if not root.exists():
        raise FileNotFoundError(f"content root does not exist: {root}")
    runtime_path = Path(sqlite_path) if sqlite_path else root / ".owcopilot" / "runtime.sqlite"
    if str(runtime_path) != ":memory:":
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
    project = ProjectContext.open(root, sqlite_path=runtime_path)
    try:
        yield project
    finally:
        project.close()


def _deterministic_cost_budget(step_name: str) -> dict[str, Any]:
    return summarize_workflow([deterministic_step(step_name)]).budget.model_dump(mode="json")


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
    max_chunks: int = 12,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
) -> dict[str, Any]:
    """Distill an unstructured manuscript into a reviewable draft (entities/relations/beats)."""
    with _project(content_root, sqlite_path) as project:
        gateway, telemetry = _gateway(
            task="extract_lore",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineExtractionProvider(),
        )
        draft = ExtractionService(gateway=gateway, bundle=project.bundle).extract(
            title=title, text=text, source_kind=source_kind, max_chunks=max_chunks
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
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        gateway, telemetry = _gateway(
            task="dialogue_tree",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineDialogueTreeProvider(),
        )
        result = DialogueTreeService(
            gateway=gateway,
            bundle=project.bundle,
            review_queue=ReviewQueue(project.sqlite_store),
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
        probe = provider or OpenAICompatProvider(model=model, timeout=timeout, max_output_tokens=16)
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
