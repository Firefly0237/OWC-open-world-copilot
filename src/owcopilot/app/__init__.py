"""UI-agnostic workbench helpers, resolved from their owning modules on demand."""

from __future__ import annotations

from importlib import import_module as _import_module
from typing import Any as _Any

_EXPORT_MODULES = {
    "add_reference_action": "actions",
    "decide_review_action": "actions",
    "list_patches_action": "actions",
    "list_project_issues_action": "actions",
    "list_references_action": "actions",
    "list_review_items_action": "actions",
    "run_apply_action": "actions",
    "run_ask_action": "actions",
    "run_barks_action": "actions",
    "run_draft_action": "actions",
    "run_impact_action": "actions",
    "run_project_audit_action": "actions",
    "run_project_export_action": "actions",
    "run_rollback_action": "actions",
    "run_suggest_action": "actions",
    "run_world_seed_action": "actions",
    "search_references_action": "actions",
    "build_content_inventory": "view_models",
    "build_context_pack_preview": "view_models",
    "build_export_summary": "view_models",
    "build_issue_summary": "view_models",
    "build_project_overview": "view_models",
}
_SUBMODULES = frozenset(
    ["_common", "actions", "analytics", "engine_sync", "view_models", "workspaces"]
)

__all__ = [
    "build_content_inventory",
    "build_context_pack_preview",
    "build_export_summary",
    "build_issue_summary",
    "build_project_overview",
    "add_reference_action",
    "decide_review_action",
    "list_references_action",
    "list_patches_action",
    "list_project_issues_action",
    "list_review_items_action",
    "run_apply_action",
    "run_ask_action",
    "run_barks_action",
    "run_draft_action",
    "run_impact_action",
    "run_project_audit_action",
    "run_project_export_action",
    "run_rollback_action",
    "run_suggest_action",
    "run_world_seed_action",
    "search_references_action",
]


def __getattr__(name: str) -> _Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is not None:
        value = getattr(_import_module(f".{module_name}", __name__), name)
    elif name in _SUBMODULES:
        value = _import_module(f".{name}", __name__)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORT_MODULES) | _SUBMODULES)
