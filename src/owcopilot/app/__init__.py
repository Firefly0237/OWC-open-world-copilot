"""UI-agnostic workbench helpers.

Only view-models and actions are exported. The FastAPI service (`service/api.py`) and the CLI
reuse these, so the whole workbench behaviour is unit-testable in core CI without any UI framework.
"""

from .actions import (
    add_reference_action,
    decide_review_action,
    list_patches_action,
    list_project_issues_action,
    list_references_action,
    list_review_items_action,
    run_apply_action,
    run_ask_action,
    run_barks_action,
    run_draft_action,
    run_impact_action,
    run_project_audit_action,
    run_project_export_action,
    run_rollback_action,
    run_suggest_action,
    run_world_seed_action,
    search_references_action,
)
from .view_models import (
    build_content_inventory,
    build_context_pack_preview,
    build_export_summary,
    build_issue_summary,
    build_project_overview,
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
