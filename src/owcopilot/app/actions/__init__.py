# UI 无关的动作层（按域拆分为包）：为任意前端执行项目工作流步骤。
"""UI-agnostic actions that execute project workflow steps for any front-end.

Each action opens the project, delegates to the same ``pipeline/*`` workflows the CLI and REST
layers use, and returns a plain JSON-able dict. Formerly one ``actions.py``; now split into
domain modules. This package re-exports every action so ``from ..app.actions import X`` keeps
working unchanged — the domain split is an internal refactor only.
"""

from __future__ import annotations

# _merge_preview / _task_timeout_sec are private helpers imported from this package by other
# modules and by tests; the redundant aliases re-export them without widening the public __all__.
from ._shared import _merge_preview as _merge_preview
from ._shared import _task_timeout_sec as _task_timeout_sec
from .agent import (
    AGENT_HITL_DECISIONS,
    AGENT_PROGRESS_STAGES,
    build_agent_chat_model,
    hitl_decisions,
    resolve_agent_thread_id,
    run_agent_diagnose_action,
)
from .analytics import world_analytics_action
from .assets import asset_attach_action, asset_detach_action, asset_list_action
from .assist import (
    detect_contradictions_action,
    dismiss_sweep_finding_action,
    fill_extraction_gaps_action,
    run_barks_action,
    run_character_action,
    run_dialogue_tree_action,
    run_draft_action,
    run_extraction_action,
    run_flavor_action,
    run_theme_sweep_action,
    submit_extraction_action,
)
from .audit import list_project_issues_action, run_impact_action, run_project_audit_action
from .collab import assign_action, collab_state_action, comment_action, lock_action
from .compliance import (
    compliance_report_action,
    rescan_case_action,
    run_compliance_scan_action,
    transition_case_action,
)
from .connection import probe_llm_connection_action
from .content_editing import (
    add_relation_action,
    apply_rename_action,
    create_entity_action,
    delete_object_action,
    draft_quest_logic_action,
    plan_rename_action,
    remove_relation_action,
    restore_snapshot_action,
    search_all_action,
    set_object_position_action,
    simulate_quest_action,
    update_dialogue_tree_action,
    update_entity_action,
    update_quest_action,
    update_quest_logic_action,
    update_style_guide_action,
)
from .engine_sync import (
    delete_mapping_template_action,
    import_from_engine_action,
    list_mapping_templates_action,
    recognize_apply_plan_action,
    recognize_content_action,
    recognize_import_action,
    run_ingest_action,
    save_mapping_template_action,
    sync_back_action,
)
from .engineering import (
    dialogue_walk_action,
    engineering_catalog_action,
    flag_xref_action,
    get_agent_run_action,
    list_agent_runs_action,
    quality_harness_action,
    quest_explore_action,
    repair_plan_action,
    style_sweep_action,
    submit_temporal_spec_action,
    translate_temporal_spec_action,
)
from .export import run_lorebook_export_action, run_project_export_action, run_prose_check_action
from .localization import loc_assign_action, loc_transition_action, localization_overview_action
from .multi_agent import run_multi_agent_action
from .patches import list_patches_action, run_apply_action, run_rollback_action, run_suggest_action
from .references import (
    add_reference_action,
    approve_reference_action,
    list_references_action,
    remove_reference_action,
    search_references_action,
    upload_reference_action,
)
from .retrieval import run_ask_action, run_build_overview_action
from .review import (
    decide_review_action,
    get_review_item_context_action,
    list_lessons_action,
    list_review_items_action,
    reviewer_calibration_action,
    revise_draft_action,
    revoke_lesson_action,
)
from .templates import instantiate_template_action, list_templates_action
from .worldgen import run_world_expand_action, run_world_seed_action

__all__ = [
    "AGENT_HITL_DECISIONS",
    "AGENT_PROGRESS_STAGES",
    "add_reference_action",
    "approve_reference_action",
    "add_relation_action",
    "apply_rename_action",
    "asset_attach_action",
    "asset_detach_action",
    "asset_list_action",
    "assign_action",
    "build_agent_chat_model",
    "collab_state_action",
    "comment_action",
    "compliance_report_action",
    "create_entity_action",
    "decide_review_action",
    "delete_mapping_template_action",
    "delete_object_action",
    "detect_contradictions_action",
    "dismiss_sweep_finding_action",
    "dialogue_walk_action",
    "draft_quest_logic_action",
    "fill_extraction_gaps_action",
    "engineering_catalog_action",
    "flag_xref_action",
    "get_agent_run_action",
    "get_review_item_context_action",
    "hitl_decisions",
    "import_from_engine_action",
    "instantiate_template_action",
    "list_mapping_templates_action",
    "list_agent_runs_action",
    "list_patches_action",
    "list_project_issues_action",
    "list_references_action",
    "list_review_items_action",
    "list_templates_action",
    "loc_assign_action",
    "loc_transition_action",
    "localization_overview_action",
    "lock_action",
    "plan_rename_action",
    "probe_llm_connection_action",
    "quality_harness_action",
    "recognize_apply_plan_action",
    "recognize_content_action",
    "recognize_import_action",
    "remove_reference_action",
    "remove_relation_action",
    "repair_plan_action",
    "rescan_case_action",
    "resolve_agent_thread_id",
    "restore_snapshot_action",
    "list_lessons_action",
    "reviewer_calibration_action",
    "revise_draft_action",
    "revoke_lesson_action",
    "run_agent_diagnose_action",
    "run_apply_action",
    "run_ask_action",
    "run_barks_action",
    "run_build_overview_action",
    "run_character_action",
    "run_compliance_scan_action",
    "run_dialogue_tree_action",
    "run_draft_action",
    "run_extraction_action",
    "run_flavor_action",
    "run_impact_action",
    "run_ingest_action",
    "run_lorebook_export_action",
    "run_multi_agent_action",
    "run_project_audit_action",
    "run_project_export_action",
    "run_prose_check_action",
    "run_rollback_action",
    "run_suggest_action",
    "run_theme_sweep_action",
    "run_world_expand_action",
    "run_world_seed_action",
    "save_mapping_template_action",
    "search_all_action",
    "search_references_action",
    "set_object_position_action",
    "quest_explore_action",
    "simulate_quest_action",
    "submit_extraction_action",
    "style_sweep_action",
    "submit_temporal_spec_action",
    "sync_back_action",
    "transition_case_action",
    "translate_temporal_spec_action",
    "update_dialogue_tree_action",
    "update_entity_action",
    "update_quest_action",
    "upload_reference_action",
    "update_quest_logic_action",
    "update_style_guide_action",
    "world_analytics_action",
]
