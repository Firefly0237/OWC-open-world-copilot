# AI 生成动作：草拟/吼叫/抽取/对白/风味/角色/主题扫描/矛盾检测。
# AI-generation actions (all route through the review queue).

"""assist actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ...assist.barks import BarkBatchService
from ...assist.calibration import critic_from_trail, primary_dim_from_trail
from ...assist.characters import CharacterProfileService, OfflineCharacterProvider
from ...assist.critic import BarkCritic, DialogueCritic, FlavorCritic, QuestCritic
from ...assist.dialogue_trees import DialogueTreeService, OfflineDialogueTreeProvider
from ...assist.drafts import QuestDraftService
from ...assist.flavor import FlavorBatchService, OfflineFlavorProvider
from ...assist.offline import OfflineBarksProvider, OfflineQuestDraftProvider
from ...assist.review_queue import ReviewQueue
from ...audit.baseline import issue_fingerprint
from ...audit.context import AuditContext
from ...content.models import ContentBundle
from ...extraction import (
    ExtractionDraft,
    ExtractionService,
    OfflineExtractionProvider,
    OfflineGapFillProvider,
    apply_gap_answers,
    quests_from_beats,
)
from ...llm.gateway import lesson_injection_enabled
from ...telemetry import llm_step, summarize_workflow
from .._common import deterministic_cost_budget as _deterministic_cost_budget
from .._common import open_project as _project
from ._shared import _bundle_counts, _gateway


def run_draft_action(
    content_root: str | Path,
    *,
    brief: str,
    sqlite_path: str | None = None,
    budget_tokens: int = 800,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
    refine_rounds: int = 2,
    inject_lessons: bool | None = None,  # Item 6: None = read OWCOPILOT_INJECT_LESSONS env
) -> dict[str, Any]:
    # Item 6: resolve lesson injection flag — explicit arg wins, else consult env var.
    _inject = lesson_injection_enabled() if inject_lessons is None else inject_lessons
    with _project(content_root, sqlite_path) as project:
        gateway, telemetry = _gateway(
            task="quest_draft",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineQuestDraftProvider(),
        )
        # IN-3 BE-1: fetch lessons from the cross-session archive so they can be injected
        # into the generation system prompt. Returns [] (not None) when the archive is empty,
        # so the service receives an explicit empty list rather than None, which means
        # inject_lessons is still True — the prompt block just renders as "" (no-op).
        # Item 6: skip DB read entirely when injection is disabled — no point fetching.
        lessons = project.sqlite_store.get_lessons_for_type("quest_draft") if _inject else []
        # The critic + refine loop raises autonomous quality so the review queue gets
        # higher-quality drafts; deterministic audit stays the hard gate and review stays final.
        critic = QuestCritic(gateway=gateway) if refine_rounds > 0 else None
        result = QuestDraftService(
            gateway=gateway,
            context_builder=project.context_builder,
            audit_runner=project.audit_runner,
            bundle=project.bundle,
            critic=critic,
            max_refine_rounds=refine_rounds,
            lessons=lessons,  # IN-3 BE-1: cross-session lesson archive
        ).draft_quest(brief, budget_tokens=budget_tokens)
        _trail_dump = [r.model_dump(mode="json") for r in result.refine_trail]
        critic_verdict, critic_score = critic_from_trail(_trail_dump)
        critic_primary_dim = primary_dim_from_trail(_trail_dump)
        item = ReviewQueue(project.sqlite_store).add_quest_draft(
            result.quest.model_dump(mode="json", exclude_none=True),
            issue_refs=[issue_fingerprint(issue) for issue in result.issues],
            critic_verdict=critic_verdict,
            critic_score=critic_score,
            critic_primary_dim=critic_primary_dim,
        )
        # Item 8: persist telemetry (non-fatal — observability must not break the primary flow).
        try:
            project.sqlite_store.record_telemetry(telemetry.records)
        except Exception:  # noqa: BLE001
            pass
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
    inject_lessons: bool | None = None,  # Item 6: None = read OWCOPILOT_INJECT_LESSONS env
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
        # Cross-session lessons for this artifact type (item_type == ReviewItemType.BARK_VARIANT),
        # same wiring as the quest draft path. inject flag: explicit arg wins, else env kill switch.
        _inject = lesson_injection_enabled() if inject_lessons is None else inject_lessons
        lessons = project.sqlite_store.get_lessons_for_type("bark_variant") if _inject else []
        result = BarkBatchService(
            gateway=gateway,
            bundle=project.bundle,
            review_queue=ReviewQueue(project.sqlite_store),
            critic=BarkCritic(gateway=gateway) if refine_rounds > 0 else None,
            max_refine_rounds=refine_rounds,
            lessons=lessons,
        ).generate(
            speaker_ids=speaker_ids,
            topic=topic,
            variants_per_speaker=variants_per_speaker,
            max_chars=max_chars,
            allowed_entity_ids=set(speaker_ids),
        )
        # Item 8: persist telemetry (non-fatal).
        try:
            project.sqlite_store.record_telemetry(telemetry.records)
        except Exception:  # noqa: BLE001
            pass
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
            extra_tasks=["verify_faithfulness"],
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
    inject_lessons: bool | None = None,  # None = read OWCOPILOT_INJECT_LESSONS env
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        gateway, telemetry = _gateway(
            task="dialogue_tree",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineDialogueTreeProvider(),
        )
        # Cross-session lessons for item_type == ReviewItemType.DIALOGUE_TREE (same wiring as
        # the quest draft path). inject flag: explicit arg wins, else env kill switch.
        _inject = lesson_injection_enabled() if inject_lessons is None else inject_lessons
        lessons = project.sqlite_store.get_lessons_for_type("dialogue_tree") if _inject else []
        critic = DialogueCritic(gateway=gateway) if refine_rounds > 0 else None
        result = DialogueTreeService(
            gateway=gateway,
            bundle=project.bundle,
            review_queue=ReviewQueue(project.sqlite_store),
            critic=critic,
            max_refine_rounds=refine_rounds,
            lessons=lessons,
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
    inject_lessons: bool | None = None,  # None = read OWCOPILOT_INJECT_LESSONS env
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        gateway, telemetry = _gateway(
            task="flavor_batch",
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineFlavorProvider(),
        )
        # Cross-session lessons for item_type == ReviewItemType.FLAVOR_BATCH (same wiring as the
        # quest draft path). inject flag: explicit arg wins, else env kill switch.
        _inject = lesson_injection_enabled() if inject_lessons is None else inject_lessons
        lessons = project.sqlite_store.get_lessons_for_type("flavor_batch") if _inject else []
        result = FlavorBatchService(
            gateway=gateway,
            bundle=project.bundle,
            review_queue=ReviewQueue(project.sqlite_store),
            critic=FlavorCritic(gateway=gateway) if refine_rounds > 0 else None,
            max_refine_rounds=refine_rounds,
            lessons=lessons,
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
    inject_lessons: bool | None = None,  # Item 6: None = read OWCOPILOT_INJECT_LESSONS env
) -> dict[str, Any]:
    """Generate one detailed character sheet grounded in the world; queue it for review."""
    from ...assist.characters import (
        CharacterBrief,
    )
    from ...assist.critic import CharacterCritic

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
        # Cross-session lessons for item_type == ReviewItemType.CHARACTER_PROFILE (same wiring as
        # the quest draft path). inject flag: explicit arg wins, else env kill switch.
        _inject = lesson_injection_enabled() if inject_lessons is None else inject_lessons
        lessons = project.sqlite_store.get_lessons_for_type("character_profile") if _inject else []
        service = CharacterProfileService(
            gateway=gateway,
            bundle=project.bundle,
            context_builder=project.context_builder,
            critic=CharacterCritic(gateway=gateway) if refine_rounds > 0 else None,
            max_refine_rounds=refine_rounds,
            lessons=lessons,
        )
        emit("generating")
        draft = service.generate(parsed, budget_tokens=budget_tokens)
        emit("parsing")
        _char_trail_dump = [r.model_dump(mode="json") for r in draft.refine_trail]
        critic_verdict, critic_score = critic_from_trail(_char_trail_dump)
        critic_primary_dim = primary_dim_from_trail(_char_trail_dump)
        item = ReviewQueue(project.sqlite_store).add_character_profile(
            {
                "entity": draft.entity.model_dump(mode="json", exclude_none=True),
                "relations": [r.model_dump(mode="json") for r in draft.relations],
                "summary": draft.entity.description,
                "suggested_relations": draft.suggested_relations,
            },
            critic_verdict=critic_verdict,
            critic_score=critic_score,
            critic_primary_dim=critic_primary_dim,
        )
        # Item 8: persist telemetry (non-fatal).
        try:
            project.sqlite_store.record_telemetry(telemetry.records)
        except Exception:  # noqa: BLE001
            pass
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
    judge + graph expansion). Read-only over canon: produces a work order for a human, writes no
    canon. Dismissals a reviewer made for THIS theme (逐一排除) are auto-loaded from the per-world
    ``SweepExclusionStore`` and suppressed, so a dismiss made via the product path bites on re-run;
    ``dismissed_count`` reports how many were suppressed. Passing no dismissals is the default and
    keeps the sweep behaviour and response identical to before (backward compatible)."""
    from ...assist.sweep import (
        OfflineSweepJudgeProvider,
        SweepExclusionStore,
        ThemeSweepService,
        render_sweep_markdown,
    )

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
        # Auto-load this theme's previously-dismissed findings from the per-world ledger and feed
        # them to the sweep. Empty (the common case, and any first run) → identical to before.
        exclusions = SweepExclusionStore(project.content_store.root).dismissed_ids(theme)
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
            exclusions=exclusions,
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
            # per-finding dismissal handle (finding_id) and sentence-level location (locator) ride
            # through on finding.__dict__ so "逐一排除" and句级定位 are visible to any client.
            "hits": [finding.__dict__ for finding in report.hits],
            "review_suggested": [finding.__dict__ for finding in report.review_suggested],
            "dismissed_count": report.dismissed_count,
            "markdown": render_sweep_markdown(report),
            "cost_budget": (
                summarize_workflow([llm_step("theme_sweep", telemetry_summary)]).budget.model_dump(
                    mode="json"
                )
                if telemetry_summary is not None
                else _deterministic_cost_budget("theme_sweep")
            ),
        }


def dismiss_sweep_finding_action(
    content_root: str | Path,
    *,
    theme: str,
    finding_id: str,
    ref: str,
    operator: str,
    locator: str = "",
    layer: str = "",
    reason: str = "",
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """逐一排除: mark ONE sweep finding a false positive so re-running the SAME theme no longer
    surfaces it. The dismissal is signed (operator) and persisted in the per-world
    ``SweepExclusionStore`` beside the world (no canon write). The caller passes back the finding's
    identity from a prior sweep response — ``finding_id`` is the stable dismissal key, ``ref`` /
    ``locator`` / ``layer`` are the audit trail persisted with it."""
    from ...assist.sweep import SweepExclusionStore, SweepFinding

    if not str(finding_id).strip():
        raise ValueError("缺少排除码（finding_id），无法逐一排除")
    with _project(content_root, sqlite_path) as project:
        store = SweepExclusionStore(project.content_store.root)
        # A transient finding carrying only what the store persists (finding_id/ref/locator/layer);
        # the display fields (name/evidence/verdict) are not stored, so they stay empty here.
        finding = SweepFinding(
            ref=ref,
            name="",
            object_kind="",
            layer=layer,
            evidence="",
            verdict="",
            locator=locator,
            finding_id=finding_id,
        )
        exclusion = store.dismiss(finding, theme, operator=operator, reason=reason)
        return {
            "dismissed": True,
            "finding_id": exclusion.finding_id,
            "theme": exclusion.theme,
            "exclusion": asdict(exclusion),
            "cost_budget": _deterministic_cost_budget("sweep_dismiss"),
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
    from ...assist.contradiction import ContradictionDetector, OfflineContradictionJudge

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
            # honesty counter: candidates past ``max_judge`` the judge never read (kept as review,
            # never silently dropped) — surfaced so the product can say "N pairs still unjudged".
            "judge_skipped": report.judge_skipped,
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
