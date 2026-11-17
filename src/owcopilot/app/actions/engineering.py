# 工程实验室：将叙事模型检查、MCTS 修复规划、风格清单与 Agent 运行史
# 暴露为 UI 无关动作，供 CLI、FastAPI 或其他客户端复用。
"""UI-agnostic engineering diagnostics.

All operations in this module leave canon untouched.  They inspect the same project bundle and
runtime store as the rest of the application; the quality harness may persist patch proposals,
but no action here applies a repair.  Model-assisted repair candidates and rubric judging are
explicit opt-ins and report their actual gateway telemetry; the default remains deterministic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...assist.review_queue import ReviewItemType, ReviewQueue
from ...assist.sweep import (
    OfflineSweepJudgeProvider,
    RubricSweepService,
    render_rubric_markdown,
)
from ...llm.telemetry import TelemetryCollector
from ...logic import build_flag_xref, explore_dialogue_tree, explore_quest, walk_dialogue_tree
from ...patches import PatchSuggestService, deterministic_candidates
from ...patches.search import CandidateProvider, plan_repairs
from ...pipeline.harness import run_quality_harness
from ...spec_authoring import (
    GatewaySpecTranslator,
    OfflineSpecTranslator,
    OutcomeStatus,
    ScriptedSpecTranslator,
    SpecDraft,
    translate_intent,
)
from ...telemetry import deterministic_step, llm_step, summarize_workflow
from .._common import deterministic_cost_budget as _deterministic_cost_budget
from .._common import open_project as _project
from ._shared import _gateway


def engineering_catalog_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """IDs available to the engineering workbench, with no inferred or fabricated options."""

    with _project(content_root, sqlite_path) as project:
        return {
            "quest_ids": sorted(project.bundle.quests),
            "dialogue_tree_ids": sorted(project.bundle.dialogue_trees),
            "style_rubrics": [
                {
                    "id": rubric.id,
                    "label": rubric.label or rubric.id,
                    "version": rubric.version,
                    "element_count": len(rubric.elements),
                }
                for rubric in sorted(
                    project.bundle.style_rubrics.values(), key=lambda item: item.id
                )
            ],
            "cost_budget": _deterministic_cost_budget("engineering_catalog"),
        }


def translate_temporal_spec_action(
    content_root: str | Path,
    *,
    quest_id: str,
    intent: str,
    use_llm: bool = False,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
    max_cost_usd: float | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Translate one planner intent without mutating canon or queue state.

    The default is the deterministic quoted-surface classifier. A real-model opt-in replaces only
    that structural translation node; closed-world grounding, formula assembly/type checking and
    back-translation remain deterministic. Invalid model JSON is surfaced as an explicit degraded
    result even when the deterministic fallback can still produce a usable candidate.
    """

    normalized_intent = intent.strip()
    if not normalized_intent:
        raise ValueError("规约意图不能为空。")
    if use_llm and llm_mode != "real":
        raise ValueError("use_llm=true 需要 llm_mode=real；离线模式请使用确定性翻译。")
    with _project(content_root, sqlite_path) as project:
        quest = project.bundle.quests.get(quest_id)
        if quest is None:
            available = "、".join(sorted(project.bundle.quests)) or "（当前世界没有任务）"
            raise ValueError(f"任务「{quest_id}」不存在。当前可用任务：{available}")

        telemetry = TelemetryCollector()
        gateway_translator: GatewaySpecTranslator | None = None
        translator: GatewaySpecTranslator | OfflineSpecTranslator
        if use_llm:
            gateway, telemetry = _gateway(
                task="spec_translate",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=None,
            )
            gateway_translator = GatewaySpecTranslator(gateway)
            translator = gateway_translator
        else:
            translator = OfflineSpecTranslator()

        outcome = translate_intent(normalized_intent, quest, project.bundle, translator)
        telemetry_summary = telemetry.summary()
        used_llm = bool(telemetry_summary["calls"])
        fallback_reason = (
            gateway_translator.fallback_reason
            if gateway_translator is not None and gateway_translator.used_fallback
            else ""
        )
        translator_mode = (
            "real_model_fallback"
            if fallback_reason
            else "real_model"
            if use_llm
            else "deterministic"
        )
        cost_budget = (
            summarize_workflow(
                [llm_step("spec_translate", telemetry_summary)],
                budget_usd=max_cost_usd,
            ).budget.model_dump(mode="json")
            if use_llm
            else summarize_workflow(
                [deterministic_step("spec_translate")],
                budget_usd=max_cost_usd,
            ).budget.model_dump(mode="json")
        )
        return {
            "quest_id": quest_id,
            "outcome": outcome.model_dump(mode="json"),
            "translator_mode": translator_mode,
            "used_llm": used_llm,
            "degraded": bool(fallback_reason),
            "degradation_reasons": [fallback_reason] if fallback_reason else [],
            "telemetry": telemetry_summary,
            "cost_budget": cost_budget,
            "writes_canon": False,
        }


def submit_temporal_spec_action(
    content_root: str | Path,
    *,
    quest_id: str,
    intent: str,
    draft: SpecDraft | dict[str, Any],
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Re-run a client-confirmed draft against current canon and enqueue it for human review.

    The client cannot submit a free-form formula. It sends only the original intent and structural
    :class:`SpecDraft`; the server rebuilds grounding, formula and back-translation from scratch.
    Rejected/unsupported or now-stale groundings never reach the queue, and enqueueing never writes
    quest metadata.
    """

    normalized_intent = intent.strip()
    if not normalized_intent:
        raise ValueError("规约意图不能为空。")
    validated_draft = SpecDraft.model_validate(draft)
    with _project(content_root, sqlite_path) as project:
        quest = project.bundle.quests.get(quest_id)
        if quest is None:
            available = "、".join(sorted(project.bundle.quests)) or "（当前世界没有任务）"
            raise ValueError(f"任务「{quest_id}」不存在。当前可用任务：{available}")
        outcome = translate_intent(
            normalized_intent,
            quest,
            project.bundle,
            ScriptedSpecTranslator(validated_draft),
        )
        if outcome.status is not OutcomeStatus.NEEDS_CONFIRMATION or outcome.spec is None:
            raise ValueError(
                "规约草稿未通过服务端封闭世界接地/解析校验，不能送审：" + outcome.message
            )

        payload = {
            "quest_id": quest_id,
            "quest_title": quest.title,
            "intent": normalized_intent,
            "draft": validated_draft.model_dump(mode="json"),
            "groundings": [row.model_dump(mode="json") for row in outcome.groundings],
            "spec": outcome.spec.model_dump(mode="json"),
            "back_translation": outcome.back_translation,
        }
        queue = ReviewQueue(project.sqlite_store)
        for pending in queue.list_pending():
            pending_spec = pending.payload.get("spec")
            if (
                pending.item_type is ReviewItemType.TEMPORAL_SPEC_DRAFT
                and pending.payload.get("quest_id") == quest_id
                and isinstance(pending_spec, dict)
                and pending_spec.get("id") == outcome.spec.id
            ):
                raise ValueError(
                    f"相同的时序规约「{outcome.spec.id}」已经在审核队列中，请勿重复送审。"
                )
        item = queue.add_temporal_spec_draft(payload)
        return {
            "quest_id": quest_id,
            "review_item_id": item.id,
            "review_item": item.model_dump(mode="json"),
            "outcome": outcome.model_dump(mode="json"),
            "writes_canon": False,
            "cost_budget": _deterministic_cost_budget("spec_submit"),
        }


def quest_explore_action(
    content_root: str | Path,
    *,
    quest_id: str,
    max_states: int = 100_000,
    max_depth: int = 10_000,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Bounded explicit-state exploration of every reachable path in one quest."""

    with _project(content_root, sqlite_path) as project:
        quest = project.bundle.quests.get(quest_id)
        if quest is None:
            available = "、".join(sorted(project.bundle.quests)) or "（当前世界没有任务）"
            raise ValueError(f"任务「{quest_id}」不存在。当前可用任务：{available}")
        report = explore_quest(quest, max_states=max_states, max_depth=max_depth)
        return {
            "quest_id": quest_id,
            "report": report.model_dump(mode="json"),
            "execution_mode": "deterministic_bounded_search",
            "cost_budget": _deterministic_cost_budget("quest_explore"),
        }


def flag_xref_action(
    content_root: str | Path,
    *,
    flag: str | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Report declarations, readers and writers for one flag or the whole world."""

    with _project(content_root, sqlite_path) as project:
        payload = build_flag_xref(project.bundle).report(flag.strip() if flag else None)
        payload["cost_budget"] = _deterministic_cost_budget("flag_xref")
        return payload


def dialogue_walk_action(
    content_root: str | Path,
    *,
    tree_id: str,
    explore: bool = False,
    choices: list[str] | None = None,
    initial_state: dict[str, Any] | None = None,
    max_endings: int = 1_000,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Walk one dialogue path or enumerate every satisfiable ending."""

    with _project(content_root, sqlite_path) as project:
        tree = project.bundle.dialogue_trees.get(tree_id)
        if tree is None:
            available = "、".join(sorted(project.bundle.dialogue_trees)) or "（当前世界没有对白树）"
            raise ValueError(f"对白树「{tree_id}」不存在。当前可用对白树：{available}")
        if explore:
            result_payload = explore_dialogue_tree(
                tree,
                initial_state=initial_state,
                max_endings=max_endings,
            ).model_dump(mode="json")
            mode = "explore"
        else:
            result_payload = walk_dialogue_tree(
                tree,
                choices=choices,
                initial_state=initial_state,
            ).model_dump(mode="json")
            mode = "walk"
        return {
            "tree_id": tree_id,
            "mode": mode,
            "result": result_payload,
            "cost_budget": _deterministic_cost_budget("dialogue_walk"),
        }


def repair_plan_action(
    content_root: str | Path,
    *,
    max_iterations: int = 200,
    max_depth: int | None = None,
    seed: int = 0,
    llm_candidates: bool = False,
    max_llm_calls: int = 32,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
    max_cost_usd: float | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Search a non-worsening repair sequence; return a proposal and never apply it.

    Model candidates widen the same MCTS move space as ``repair-plan --llm-candidates``.  Every
    move still goes through the shared shadow validator and non-worsening pruning in
    :func:`plan_repairs`; opting in therefore changes candidate generation, never the write
    boundary.  Offline mode intentionally keeps the deterministic provider, matching the CLI.
    """

    with _project(content_root, sqlite_path) as project:
        candidate_provider: CandidateProvider = deterministic_candidates
        model_candidates_enabled = llm_candidates and llm_mode == "real"
        telemetry = TelemetryCollector()
        if model_candidates_enabled:
            gateway, telemetry = _gateway(
                task="patch_suggest",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=None,
            )
            candidate_provider = PatchSuggestService(
                bundle=project.bundle,
                audit_runner=project.audit_runner,
                gateway=gateway,
                context_builder=project.context_builder,
            ).as_candidate_provider(max_llm_calls=max_llm_calls)
        plan = plan_repairs(
            project.bundle,
            project.audit_runner,
            max_iterations=max_iterations,
            max_depth=max_depth,
            seed=seed,
            candidate_provider=candidate_provider,
        )
        telemetry_summary = telemetry.summary()
        used_llm = bool(telemetry_summary["calls"])
        cost_budget = (
            summarize_workflow(
                [llm_step("patch_suggest", telemetry_summary)],
                budget_usd=max_cost_usd,
            ).budget.model_dump(mode="json")
            if model_candidates_enabled
            else summarize_workflow([deterministic_step("repair_plan")]).budget.model_dump(
                mode="json"
            )
        )
        return {
            "plan": plan.model_dump(mode="json"),
            "candidate_source": (
                "deterministic_plus_model" if model_candidates_enabled else "deterministic_fixers"
            ),
            "used_llm": used_llm,
            "llm_mode": llm_mode if model_candidates_enabled else "offline",
            "telemetry": telemetry_summary,
            "writes_canon": False,
            "cost_budget": cost_budget,
        }


def quality_harness_action(
    content_root: str | Path,
    *,
    propose_fixes: bool = True,
    max_issues: int = 5,
    max_candidates_per_issue: int = 1,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Run the project quality loop; proposals may persist, but canon is never modified."""

    with _project(content_root, sqlite_path) as project:
        report = run_quality_harness(
            project,
            propose_fixes=propose_fixes,
            max_issues=max_issues,
            max_candidates_per_issue=max_candidates_per_issue,
        )
        return {
            **report.model_dump(mode="json"),
            "writes_canon": False,
            "cost_budget": _deterministic_cost_budget("quality_harness"),
        }


def style_sweep_action(
    content_root: str | Path,
    *,
    rubric_id: str,
    semantic_threshold: float = 0.5,
    use_llm: bool = False,
    max_judge: int = 400,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
    max_cost_usd: float | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Run the StyleRubric cascade with an optional, bounded L3 judge.

    ``llm_mode='offline'`` uses the same explicit test/CI judge double as the CLI.  Real mode uses
    the shared provider gateway after the service router has admitted the request.  Both paths are
    read-only over canon and expose actual call telemetry rather than inferring use from a toggle.
    """

    with _project(content_root, sqlite_path) as project:
        rubric = project.bundle.style_rubrics.get(rubric_id)
        if rubric is None:
            available = (
                "、".join(sorted(project.bundle.style_rubrics)) or "（当前世界没有风格清单）"
            )
            raise ValueError(f"风格清单「{rubric_id}」不存在。当前可用清单：{available}")
        gateway = None
        telemetry = TelemetryCollector()
        if use_llm:
            gateway, telemetry = _gateway(
                task="theme_sweep",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=OfflineSweepJudgeProvider(),
            )
        report = RubricSweepService(
            bundle=project.bundle,
            gateway=gateway,
            embedder=project.embedder,
        ).sweep(
            rubric,
            use_llm=use_llm,
            max_judge=max_judge,
            semantic_threshold=semantic_threshold,
        )
        telemetry_summary = telemetry.summary()
        llm_used = bool(telemetry_summary["calls"])
        judge_mode = (
            "real_model"
            if use_llm and llm_mode == "real"
            else "offline_double"
            if use_llm
            else "disabled"
        )
        cost_budget = (
            summarize_workflow(
                [llm_step("theme_sweep", telemetry_summary)],
                budget_usd=max_cost_usd,
            ).budget.model_dump(mode="json")
            if use_llm
            else _deterministic_cost_budget("style_sweep")
        )
        return {
            "rubric_id": report.rubric_id,
            "rubric_label": report.rubric_label,
            "rubric_version": report.rubric_version,
            "rubric_fingerprint": report.rubric_fingerprint,
            "abstract_tag": report.abstract_tag,
            "scanned_total": report.scanned_total,
            "scanned_by_kind": report.scanned_by_kind,
            "scanned_fields": report.scanned_fields,
            "semantic_used": report.semantic_used,
            "semantic_threshold": report.semantic_threshold,
            "llm_used": llm_used,
            "judge_mode": judge_mode,
            "judge_limit": max_judge,
            "judged_count": report.judged_count,
            "judge_skipped": report.judge_skipped,
            "dismissed_count": report.dismissed_count,
            "l1_total": report.l1_total,
            "l2_total": report.l2_total,
            "l1_l2_overlap": report.l1_l2_overlap,
            "hits": [finding.__dict__ for finding in report.hits],
            "review_suggested": [finding.__dict__ for finding in report.review_suggested],
            "elements": [
                {
                    "element_id": element.element_id,
                    "element_text": element.element_text,
                    "kind": element.kind,
                    "polarity": element.polarity,
                    "l1_refs": element.l1_refs,
                    "l2_refs": element.l2_refs,
                    "l3_refs": element.l3_refs,
                    "candidate_refs": element.candidate_refs,
                    "findings": [finding.__dict__ for finding in element.findings],
                }
                for element in report.elements
            ],
            "markdown": render_rubric_markdown(report),
            "telemetry": telemetry_summary,
            "cost_budget": cost_budget,
        }


def list_agent_runs_action(
    content_root: str | Path,
    *,
    limit: int = 50,
    offset: int = 0,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """List lightweight run summaries; transcripts stay behind the detail action."""

    with _project(content_root, sqlite_path) as project:
        runs = project.sqlite_store.list_agent_runs(limit=limit, offset=offset)
        total, snapshot_etag = project.sqlite_store.agent_runs_snapshot()
        return {
            "total": total,
            "offset": offset,
            "snapshot_etag": snapshot_etag,
            "runs": runs,
            "cost_budget": _deterministic_cost_budget("agent_history"),
        }


def get_agent_run_action(
    content_root: str | Path,
    *,
    run_id: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Load one persisted run including its full transcript."""

    with _project(content_root, sqlite_path) as project:
        run = project.sqlite_store.get_agent_run(run_id)
        if run is None:
            raise ValueError(f"Agent 运行「{run_id}」不存在。请先刷新运行历史。")
        return {
            "run": run,
            "cost_budget": _deterministic_cost_budget("agent_history"),
        }
