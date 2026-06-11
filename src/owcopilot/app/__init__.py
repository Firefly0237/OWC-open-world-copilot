"""Optional app/UI helpers.

Only view-models and actions are exported — importing this package never pulls in Streamlit.
The dashboard (`dashboard.py`) is launched explicitly via `streamlit run`.
"""

from .actions import (
    decide_review_action,
    list_patches_action,
    list_project_issues_action,
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
)
from .view_models import (
    build_context_pack_preview,
    build_export_summary,
    build_issue_summary,
    build_project_overview,
)

__all__ = [
    "build_context_pack_preview",
    "build_export_summary",
    "build_issue_summary",
    "build_project_overview",
    "decide_review_action",
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
]
