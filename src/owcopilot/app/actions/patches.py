# 补丁工作流：修复候选 / 列出 / 应用 / 回滚。
# Patch workflow: suggest, list, apply, rollback.

"""patches actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...llm.telemetry import TelemetryCollector
from ...pipeline.patches import (
    apply_patch_workflow,
    find_issue,
    rollback_patch_workflow,
    suggest_for_issue,
)
from ...storage.world_lock import serialized_world_write
from ...telemetry import deterministic_step, llm_step, summarize_workflow
from .._common import deterministic_cost_budget as _deterministic_cost_budget
from .._common import open_project as _project
from ._shared import _gateway


def run_suggest_action(
    content_root: str | Path,
    *,
    issue_id: str,
    sqlite_path: str | None = None,
    max_candidates: int = 3,
    budget_tokens: int = 600,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        issue = find_issue(project, issue_id)
        gateway = None
        telemetry = TelemetryCollector()
        if llm_mode == "real":
            gateway, telemetry = _gateway(
                task="patch_suggest",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=None,
            )
        result = suggest_for_issue(
            project,
            issue,
            gateway=gateway,
            max_candidates=max_candidates,
            budget_tokens=budget_tokens,
        )
        telemetry_summary = telemetry.summary()
        return {
            "issue_id": issue_id,
            "candidates": [
                {
                    "patch_id": ranked.candidate.id,
                    "source": ranked.source,
                    "target_resolved": ranked.target_resolved,
                    "resolved_error_count": len(ranked.resolved_errors),
                    "ops": [op.model_dump(mode="json") for op in ranked.candidate.ops],
                    "rationale": ranked.candidate.rationale,
                }
                for ranked in result.candidates
            ],
            "rejected_count": result.rejected_count,
            "parse_failed": result.parse_failed,
            "used_llm": result.used_llm,
            "telemetry": telemetry_summary,
            "cost_budget": (
                summarize_workflow([llm_step("patch_suggest", telemetry_summary)]).budget
                if result.used_llm
                else summarize_workflow([deterministic_step("patch_suggest")]).budget
            ).model_dump(mode="json"),
        }


def list_patches_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        patches = project.sqlite_store.list_patches(status=status)
        return {
            "count": len(patches),
            "patches": patches,
            "cost_budget": _deterministic_cost_budget("list_patches"),
        }


@serialized_world_write
def run_apply_action(
    content_root: str | Path,
    *,
    patch_id: str,
    operator: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        outcome = apply_patch_workflow(project, patch_id, operator=operator)
        return {
            "applied": outcome.applied,
            "patch_id": outcome.patch_id,
            "reason": outcome.reason,
            "introduced_errors": outcome.introduced_errors,
            "resolved_errors": outcome.resolved_errors,
            "post_audit_open_errors": outcome.post_audit_open_errors,
            "cost_budget": _deterministic_cost_budget("apply_patch"),
        }


@serialized_world_write
def run_rollback_action(
    content_root: str | Path,
    *,
    patch_id: str,
    operator: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        outcome = rollback_patch_workflow(project, patch_id, operator=operator)
        return {
            "rolled_back": outcome.rolled_back,
            "patch_id": outcome.patch_id,
            "post_audit_open_errors": outcome.post_audit_open_errors,
            "cost_budget": _deterministic_cost_budget("rollback_patch"),
        }
