"""View-model builders for the optional UI layer.

These functions intentionally avoid importing Streamlit. The dashboard can call them later, while
core CI can test UI-facing data without installing or running a web app.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..content.hash import content_hash
from ..exporters import EngineTarget, load_export_manifest
from ..pipeline.project import ProjectContext
from ..telemetry import deterministic_step, summarize_workflow
from ..trust import summarize_provenance


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
