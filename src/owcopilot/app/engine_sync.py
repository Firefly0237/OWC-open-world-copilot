# 引擎回同步：把引擎侧改的任务拉回审阅。
"""WS-K · engine back-sync: pull quest changes made on the engine side back into review.

The S14 export is one-way (canon -> engine). This closes the loop: given quest rows a UE/Unity
project (or a tool) edited and exported, coerce them to v2 Quests (reusing the adapter coercion),
diff against canon by content fingerprint, and queue the new/changed ones for human review — engine
edits never auto-land. The live-bridge pull itself is the deployment step; the diff+queue is here.
"""

from __future__ import annotations

from typing import Any

from ..collab import etag_for
from ..content.engine_coerce import coerce_quest
from ..content.models import ContentBundle


def plan_engine_import(incoming: list[dict[str, Any]], bundle: ContentBundle) -> dict[str, Any]:
    """Classify each incoming quest vs canon: new / changed / unchanged (by content fingerprint).

    Thin wrapper over :func:`plan_engine_sync_back` with no baseline, so the REST engine-import
    endpoint and the CLI ``sync-back`` share ONE classification implementation (same fingerprints,
    same staging rules). The response is a superset of the historical shape — callers that only
    read ``new``/``changed``/``unchanged`` are unaffected, and REST consumers now also get the
    honest ``warnings`` (e.g. "no baseline, conflicts undetectable")."""
    return plan_engine_sync_back(incoming, bundle, baseline=None)


def staged_bundle(plan: dict[str, Any]) -> ContentBundle:
    """A bundle of just the new/changed quests, to route through the review queue (HITL)."""
    bundle = ContentBundle()
    for qid in plan["new"] + plan["changed"]:
        bundle.quests[qid] = plan["_quests"][qid]
    return bundle


def plan_engine_sync_back(
    incoming: list[dict[str, Any]],
    bundle: ContentBundle,
    *,
    baseline: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Three-way classification of engine-side quest rows against canon AND the export baseline.

    ``baseline`` is the engine JSON OWCopilot originally exported (the export-time snapshot). With
    it, a divergence is attributed: ``changed`` = only the engine moved (safe to stage for review);
    ``conflicts`` = BOTH the engine and the library moved since export (never staged — a human must
    reconcile); ``library_ahead`` = only the library moved (the engine copy is stale; importing it
    would silently revert newer library work, so it is skipped). Deletions are covered too:
    ``engine_missing`` = present at export time but absent from the engine rows (an engine-side
    delete or a partial export — surfaced as a warning, never auto-deleted); a quest the LIBRARY
    deleted after export is NOT resurrected as "new": unchanged-vs-baseline engine copies go to
    ``library_ahead`` (the deletion is newer), engine-edited ones go to ``conflicts``. Without a
    baseline the split is two-way and conflicts/deletions are UNDETECTABLE — reported honestly as
    a warning, and every divergence lands in ``changed`` for human review.

    Fingerprints reuse the same ``coerce_quest`` + ``etag_for`` pair everywhere so "changed" means
    the same thing on every path (the REST engine-import endpoint delegates here with
    ``baseline=None``).
    """
    engine: dict[str, Any] = {}
    for raw in incoming:
        quest = coerce_quest(raw)
        engine[quest.id] = quest
    base_etags: dict[str, str] = {}
    if baseline is not None:
        for raw in baseline:
            quest = coerce_quest(raw)
            base_etags[quest.id] = etag_for(quest)

    new: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    conflicts: list[str] = []
    library_ahead: list[str] = []
    warnings: list[str] = []
    staged: dict[str, Any] = {}

    for qid, quest in engine.items():
        existing = bundle.quests.get(qid)
        if existing is None:
            base = base_etags.get(qid)
            if base is None:
                new.append(qid)
                staged[qid] = quest
            elif etag_for(quest) == base:
                # The LIBRARY deleted this quest after export and the engine copy is untouched —
                # staging it would resurrect deliberately-deleted content. The deletion is newer.
                library_ahead.append(qid)
                warnings.append(
                    f"任务「{qid}」已在库侧删除而引擎侧未改（引擎数据过期），未复活——如需恢复请人工决定"
                )
            else:
                conflicts.append(qid)
                warnings.append(
                    f"任务「{qid}」库侧已删除但引擎侧又有修改（冲突），未入审阅队列——请人工裁决去留"
                )
            continue
        engine_etag, library_etag = etag_for(quest), etag_for(existing)
        if engine_etag == library_etag:
            unchanged.append(qid)
            continue
        if baseline is None:
            changed.append(qid)
            staged[qid] = quest
            continue
        base = base_etags.get(qid)
        engine_moved = base is None or engine_etag != base
        library_moved = base is None or library_etag != base
        if engine_moved and library_moved:
            conflicts.append(qid)
            warnings.append(
                f"任务「{qid}」引擎侧与库侧都相对导出基线发生了修改（冲突），未入审阅队列——"
                "请人工合并后重新回流"
            )
        elif engine_moved:
            changed.append(qid)
            staged[qid] = quest
        else:  # library moved, engine copy is stale — importing it would revert newer work
            library_ahead.append(qid)
            warnings.append(
                f"任务「{qid}」库侧已比导出基线更新而引擎侧未改（引擎数据过期），已跳过"
            )

    # Engine-side deletions: exported at baseline time but absent from the incoming rows. Never
    # auto-deleted (zero authoritative writes) — surfaced so a human notices the disappearance.
    engine_missing: list[str] = []
    if baseline is not None:
        for qid in base_etags:
            if qid not in engine:
                engine_missing.append(qid)
                warnings.append(
                    f"任务「{qid}」在导出基线中存在但引擎数据里缺失（引擎侧删除或导出不完整）——"
                    "未做任何删除，请人工确认"
                )

    if baseline is None and changed:
        warnings.append("未提供导出基线（--baseline），无法判定双侧冲突——全部差异按待审变更处理")

    return {
        "new": sorted(new),
        "changed": sorted(changed),
        "unchanged": sorted(unchanged),
        "conflicts": sorted(conflicts),
        "library_ahead": sorted(library_ahead),
        "engine_missing": sorted(engine_missing),
        "baseline_provided": baseline is not None,
        "warnings": warnings,
        "_quests": staged,  # internal: only new + safely-changed quests, never conflicts
    }
