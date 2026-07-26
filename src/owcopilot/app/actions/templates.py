# 模板库：内置档案库 + 按模板实例化。
# Template library + instantiate.

"""templates actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...assist.review_queue import ReviewQueue
from .._common import deterministic_cost_budget as _deterministic_cost_budget
from .._common import open_project as _project


def list_templates_action() -> dict[str, Any]:
    """WS-G · the built-in template / archetype library."""
    from ...templates import list_templates

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
    from ...templates import instantiate

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
