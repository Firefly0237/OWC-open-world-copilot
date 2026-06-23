"""View-model builders for the UI layer.

These functions import no UI framework: the FastAPI service exposes them to the Vue front-end,
and core CI can test UI-facing data without installing or running a web app.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from ..content.hash import content_hash
from ..content.relation_kinds import relation_kind_catalog
from ..content.snapshot import bundle_diff, list_snapshots, load_snapshot, write_snapshot
from ..content.store import ContentStore
from ..exporters import EngineTarget, load_export_manifest
from ..graph.dialogue_view import build_dialogue_flow
from ..graph.graph_view import build_graph_overview, build_graph_view
from ..graph.timeline_view import build_timeline_view
from ..readiness import assess_readiness
from ..trust import summarize_provenance
from ._common import deterministic_cost_budget as _deterministic_cost_budget
from ._common import open_project as _project


def build_project_overview(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        bundle = project.bundle
        return {
            "content_root": str(Path(content_root)),
            "content_hash": content_hash(bundle),
            "counts": {
                "entities": len(bundle.entities),
                "relations": len(bundle.relations),
                "quests": len(bundle.quests),
                "regions": len(bundle.regions),
                "pois": len(bundle.pois),
                "dialogues": len(bundle.dialogues),
                "terms": len(bundle.terms),
                "style_guides": len(bundle.style_guides),
            },
            "graph": {
                "nodes": len(project.graph.node_refs()),
                "edges": len(project.graph.edge_refs()),
            },
            "provenance": summarize_provenance(bundle).model_dump(mode="json"),
        }


def build_content_inventory(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Flatten the bundle into JSON-able rows for read-only browsing (the 设定档案 page).

    Also exposes graph node refs so pickers (impact targets, bark speakers) can offer real
    ids instead of free-text fields. Pure read; zero LLM cost.
    """
    with _project(content_root, sqlite_path) as project:
        bundle = project.bundle
        entities = [
            {
                "id": e.id,
                "name": e.name,
                "type": e.type.value,
                "description": e.description,
                "tags": ", ".join(e.tags),
                "origin": e.origin.value,
                "review_status": e.review_status.value,
                # character sheets and other rich payloads ride in metadata; the
                # frontend maintenance UI reads/writes them through here
                "metadata": e.metadata,
            }
            for e in bundle.entities.values()
        ]
        quests = [
            {
                "id": q.id,
                "title": q.title,
                "giver_npc": q.giver_npc or "",
                "location": q.location or "",
                "objective": q.objective,
                "stages": len(q.stages),
                "timeline_order": q.timeline_order,
                "origin": q.origin.value,
                "review_status": q.review_status.value,
            }
            for q in bundle.quests.values()
        ]
        regions = [
            {
                "id": r.id,
                "name": r.name,
                "level_min": r.level_min,
                "level_max": r.level_max,
                "themes": ", ".join(r.themes),
                "banned_content": ", ".join(r.banned_content),
            }
            for r in bundle.regions.values()
        ]
        pois = [
            {
                "id": p.id,
                "name": p.name,
                "region_id": p.region_id or "",
                "purpose": p.purpose,
                "controlling_faction": p.controlling_faction or "",
            }
            for p in bundle.pois.values()
        ]
        terms = [
            {
                "id": t.id,
                "canonical": t.canonical,
                "aliases": ", ".join(t.aliases),
                "forbidden": ", ".join(t.forbidden),
                "description": t.description,
            }
            for t in bundle.terms.values()
        ]
        dialogues = [
            {
                "id": d.id,
                "text_key": d.text_key,
                "speaker_id": d.speaker_id or "",
                "quest_id": d.quest_id or "",
                "text": d.text or "",
            }
            for d in bundle.dialogues.values()
        ]
        relations = [
            {"source": rel.source, "kind": rel.kind, "target": rel.target}
            for rel in bundle.relations
        ]
        dialogue_trees = [
            {
                "id": tree.id,
                "title": tree.title,
                "quest_id": tree.quest_id or "",
                "participants": ", ".join(tree.participants),
                "nodes": len(tree.nodes),
            }
            for tree in bundle.dialogue_trees.values()
        ]
        dialogue_tree_payloads = {
            tree.id: tree.model_dump(mode="json", exclude_none=True)
            for tree in bundle.dialogue_trees.values()
        }
        style_guides = [
            {"id": s.id, "body": s.body, "rules": list(s.rules)}
            for s in bundle.style_guides.values()
        ]
        return {
            "entities": entities,
            "quests": quests,
            "regions": regions,
            "pois": pois,
            "terms": terms,
            "dialogues": dialogues,
            "dialogue_trees": dialogue_trees,
            "dialogue_tree_payloads": dialogue_tree_payloads,
            "relations": relations,
            "style_guides": style_guides,
            "localized_text_count": len(bundle.localized_texts),
            "graph_refs": sorted(project.graph.node_refs()),
            "cost_budget": _deterministic_cost_budget("content_inventory"),
        }


def build_issue_summary(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        issues = project.sqlite_store.list_issues()
        by_severity = Counter(issue.severity.value for issue in issues)
        by_status = Counter(issue.status.value for issue in issues)
        by_rule = Counter(issue.rule_code for issue in issues)
        return {
            "count": len(issues),
            "by_severity": dict(sorted(by_severity.items())),
            "by_status": dict(sorted(by_status.items())),
            "by_rule": dict(sorted(by_rule.items())),
            "cost_budget": _deterministic_cost_budget("list_issues"),
        }


# Display labels for the readiness kinds, shared by every UI consumer (the API responses and
# the markdown work order). The Vue board keeps its own copy so the frontend stays self-contained.
READINESS_KIND_LABELS: dict[str, str] = {
    "quest": "任务",
    "character": "角色",
    "faction": "势力",
    "region": "区域",
    "poi": "地点",
    "term": "词条",
    "dialogue_tree": "对话树",
}


def build_readiness_report(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
    only_incomplete: bool = False,
    kind: str | None = None,
) -> dict[str, Any]:
    """Score every content item against the production-ready standard (规范/标准化的策划管理).

    Pure read, zero LLM cost. Completeness, not correctness — see the audit for the latter.

    ``by_kind`` and the headline totals always describe the *whole* project; ``kind`` /
    ``only_incomplete`` narrow only the returned ``items`` list, so a caller can show the full
    summary alongside a filtered drill-down without a second call.
    """
    with _project(content_root, sqlite_path) as project:
        report = assess_readiness(project.bundle)
        payload = report.model_dump(mode="json")
        items = payload["items"]
        if kind:
            items = [it for it in items if it["kind"] == kind]
        if only_incomplete:
            items = [it for it in items if not it["ready"]]
        payload["items"] = items
        payload["cost_budget"] = _deterministic_cost_budget("readiness")
        return payload


def readiness_workorder_markdown(report: dict[str, Any]) -> str:
    """Render the not-yet-ready items as a Markdown work order (planning hand-off, not an audit).

    Pure formatting over a :func:`build_readiness_report` payload — groups the incomplete items by
    kind and lists, per item, exactly which checklist entries are still missing. Filters on each
    item's own ``ready`` flag, so it is correct whether the payload was pre-filtered or not.
    """
    pct = round(float(report.get("overall_score", 0.0)) * 100)
    lines = [
        "# 设计就绪度工作单",
        "",
        (
            f"标准 {report.get('standard_version', '?')} ｜ 总体就绪度 {pct}% ｜ "
            f"已就绪 {report.get('ready_items', 0)} / {report.get('total_items', 0)} 项"
        ),
        "",
    ]
    incomplete = [it for it in report.get("items", []) if not it.get("ready")]
    if not incomplete:
        lines.append("当前范围内的内容均已达到可量产标准。")
        return "\n".join(lines) + "\n"

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in incomplete:
        grouped.setdefault(item["kind"], []).append(item)
    for kind in sorted(grouped):
        label = READINESS_KIND_LABELS.get(kind, kind)
        lines.append(f"## {label}（待补 {len(grouped[kind])} 项）")
        lines.append("")
        for item in grouped[kind]:
            missing = "、".join(item.get("missing", [])) or "—"
            lines.append(f"- **{item['name']}**（`{item['ref']}`）：尚缺 {missing}")
        lines.append("")
    return "\n".join(lines) + "\n"


def build_timeline_view_model(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Chronology of quests + events on one dense-ranked axis, with audit violations hung on.

    Pure read, zero LLM cost. Consumes the deterministic timeline audit — it does not re-judge
    correctness (see :mod:`owcopilot.graph.timeline_view`).
    """
    with _project(content_root, sqlite_path) as project:
        payload = build_timeline_view(project.bundle).model_dump(mode="json")
        payload["cost_budget"] = _deterministic_cost_budget("timeline")
        return payload


def build_graph_view_model(
    content_root: str | Path,
    *,
    focus_ref: str | None,
    radius: int = 1,
    kinds: set[str] | None = None,
    impact: bool = False,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Relationship subgraph for the SVG renderer: ego graph around ``focus_ref``, or — when
    ``focus_ref`` is empty — the whole-world clustered overview.

    Pure read, zero LLM cost. ``impact`` overlays the deterministic ripple (see
    :mod:`owcopilot.graph.graph_view`).
    """
    with _project(content_root, sqlite_path) as project:
        if focus_ref:
            view = build_graph_view(
                project.bundle, focus_ref=focus_ref, radius=radius, kinds=kinds, impact=impact
            )
        else:
            view = build_graph_overview(project.bundle)
        payload = view.model_dump(mode="json")
        payload["cost_budget"] = _deterministic_cost_budget("graph")
        return payload


def relation_kinds_view_model() -> dict[str, Any]:
    """The pre-provided relationship-kind catalog the editor offers (custom kinds still allowed)."""
    return {"kinds": [kind.model_dump(mode="json") for kind in relation_kind_catalog()]}


def build_quest_view_model(
    content_root: str | Path,
    *,
    quest_id: str,
    sqlite_path: str | None = None,
) -> dict[str, Any] | None:
    """The full quest (objective/prereqs/stages not in the timeline payload) for the editor. $0."""
    with _project(content_root, sqlite_path) as project:
        quest = project.bundle.quests.get(quest_id)
        return quest.model_dump(mode="json") if quest is not None else None


def build_dialogue_list_view_model(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """List the branching dialogue trees in the world (id/title/participants/node count). $0."""
    with _project(content_root, sqlite_path) as project:
        names = {e.id: e.name for e in project.bundle.entities.values()}
        trees = [
            {
                "id": tree.id,
                "title": tree.title or tree.id,
                "participants": [names.get(pid, pid) for pid in tree.participants],
                "node_count": len(tree.nodes),
            }
            for tree in sorted(project.bundle.dialogue_trees.values(), key=lambda t: t.id)
        ]
        return {"trees": trees, "cost_budget": _deterministic_cost_budget("dialogue_list")}


def build_dialogue_flow_view_model(
    content_root: str | Path,
    *,
    tree_id: str,
    sqlite_path: str | None = None,
) -> dict[str, Any] | None:
    """Laid-out flow graph for one dialogue tree, or None if it does not exist. $0."""
    with _project(content_root, sqlite_path) as project:
        tree = project.bundle.dialogue_trees.get(tree_id)
        if tree is None:
            return None
        names = {e.id: e.name for e in project.bundle.entities.values()}
        payload = build_dialogue_flow(tree, speaker_names=names).model_dump(mode="json")
        payload["cost_budget"] = _deterministic_cost_budget("dialogue_flow")
        return payload


def build_dialogue_tree_view_model(
    content_root: str | Path,
    *,
    tree_id: str,
    sqlite_path: str | None = None,
) -> dict[str, Any] | None:
    """The FULL structural dialogue tree (untruncated text/speakers/choices) for the editor. $0."""
    with _project(content_root, sqlite_path) as project:
        tree = project.bundle.dialogue_trees.get(tree_id)
        return tree.model_dump(mode="json") if tree is not None else None


def build_snapshots_view_model(content_root: str | Path) -> dict[str, Any]:
    """List canon snapshots (newest first). $0."""
    store = ContentStore(content_root)
    return {"snapshots": [meta.model_dump(mode="json") for meta in list_snapshots(store)]}


def create_world_snapshot(content_root: str | Path, *, label: str = "") -> dict[str, Any]:
    """Take a labelled snapshot of the current world; returns its metadata."""
    store = ContentStore(content_root)
    return write_snapshot(store, label=label).model_dump(mode="json")


def build_diff_view_model(content_root: str | Path, *, from_id: str) -> dict[str, Any] | None:
    """Diff a snapshot against the current world, or None if the snapshot does not exist. $0."""
    store = ContentStore(content_root)
    old = load_snapshot(store, from_id)
    if old is None:
        return None
    payload = bundle_diff(old, store.load()).model_dump(mode="json")
    payload["from_id"] = from_id
    return payload


def build_context_pack_preview(
    content_root: str | Path,
    *,
    query: str,
    sqlite_path: str | None = None,
    budget_tokens: int = 800,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        pack = project.context_builder.build(query, budget_tokens=budget_tokens)
        return {
            "query": pack.query,
            "budget_tokens": pack.budget_tokens,
            "refs": pack.refs,
            "hits": [hit.model_dump(mode="json") for hit in pack.hits],
            "cost_budget": _deterministic_cost_budget("build_context_pack"),
        }


def build_export_summary(
    *,
    output_dir: str | Path,
    target_engine: EngineTarget | str = EngineTarget.GENERIC,
) -> dict[str, Any]:
    engine = EngineTarget(target_engine)
    export_dir = Path(output_dir) / engine.value
    manifest_path = export_dir / "manifest.json"
    manifest = load_export_manifest(manifest_path) if manifest_path.exists() else None
    return {
        "target_engine": engine.value,
        "output_dir": str(export_dir),
        "manifest_exists": manifest is not None,
        "manifest": manifest.model_dump(mode="json") if manifest is not None else None,
        "cost_budget": _deterministic_cost_budget("export_summary"),
    }
