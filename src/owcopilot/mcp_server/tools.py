"""MCP tool handlers without a hard dependency on an MCP transport SDK."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..content.hash import content_hash
from ..exporters import EngineTarget, export_content_bundle
from ..impact import Change, ChangeSet, ChangeType, ImpactAnalyzer, ImpactLevel
from ..llm.cache import NoOpCache
from ..llm.gateway import LLMGateway
from ..llm.router import StaticRouter
from ..llm.telemetry import TelemetryCollector
from ..pipeline.audit import run_full_audit
from ..pipeline.patches import find_issue, suggest_for_issue
from ..pipeline.project import ProjectContext
from ..qa.offline import OfflineQAProvider
from ..qa.service import LoreQAService
from ..telemetry import deterministic_step, llm_step, summarize_workflow


def audit_project(
    *,
    content_root: str,
    sqlite_path: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Run the default deterministic audit rules for a project."""
    with _project(content_root, sqlite_path) as project:
        result = run_full_audit(project, persist=persist)
        return {
            "content_hash": content_hash(project.bundle),
            "audit_run": result.run.model_dump(mode="json"),
            "issues": [issue.model_dump(mode="json") for issue in result.issues],
            "open_errors": len(result.open_errors),
            "cost_budget": _deterministic_cost_budget("audit_project"),
        }


def list_issues(
    *,
    content_root: str,
    sqlite_path: str | None = None,
    severity: str | None = None,
    rule_code: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """List persisted audit issues for a project."""
    with _project(content_root, sqlite_path) as project:
        issues = project.sqlite_store.list_issues(
            severity=severity,
            rule_code=rule_code,
            status=status,
        )
        return {
            "count": len(issues),
            "issues": [issue.model_dump(mode="json") for issue in issues],
            "cost_budget": _deterministic_cost_budget("list_issues"),
        }


def build_context_pack(
    *,
    content_root: str,
    query: str,
    sqlite_path: str | None = None,
    budget_tokens: int = 800,
) -> dict[str, Any]:
    """Build a retrieval context pack for a lore query."""
    with _project(content_root, sqlite_path) as project:
        pack = project.context_builder.build(query, budget_tokens=budget_tokens)
        return {
            "query": pack.query,
            "budget_tokens": pack.budget_tokens,
            "refs": pack.refs,
            "hits": [hit.model_dump(mode="json") for hit in pack.hits],
            "cost_budget": _deterministic_cost_budget("build_context_pack"),
        }


def ask_lore(
    *,
    content_root: str,
    query: str,
    sqlite_path: str | None = None,
    budget_tokens: int = 800,
    max_cost_usd: float | None = None,
) -> dict[str, Any]:
    """Answer a lore question with grounded citations."""
    with _project(content_root, sqlite_path) as project:
        telemetry = TelemetryCollector()
        answer = LoreQAService(
            gateway=LLMGateway(
                providers={"cheap": OfflineQAProvider()},
                router=StaticRouter(mapping={"qa_answer": "cheap"}),
                cache=NoOpCache(),
                telemetry=telemetry,
            ),
            context_builder=project.context_builder,
            bundle=project.bundle,
        ).ask(query, budget_tokens=budget_tokens)
        telemetry_summary = telemetry.summary()
        cost_budget = summarize_workflow(
            [llm_step("ask_lore", telemetry_summary)],
            budget_usd=max_cost_usd,
        ).budget
        return {
            "answer": answer.model_dump(mode="json"),
            "telemetry": telemetry_summary,
            "cost_budget": cost_budget.model_dump(mode="json"),
        }


def impact_of(
    *,
    content_root: str,
    changes: list[dict[str, str]],
    sqlite_path: str | None = None,
    max_depth: int = 2,
) -> dict[str, Any]:
    """Preview which content a planned change would touch (pure graph traversal, no LLM).

    Each change is {"change_type": "...", "target_ref": "..."}; change types:
    entity_rename, entity_delete, entity_field_change, relation_change, content_change.
    """
    with _project(content_root, sqlite_path) as project:
        parsed: list[Change] = []
        for spec in changes:
            change_type = ChangeType(str(spec["change_type"]))
            parsed.append(Change(change_type=change_type, target_ref=str(spec["target_ref"])))
        result = ImpactAnalyzer(project.graph).analyze(
            ChangeSet(changes=parsed), max_depth=max_depth
        )
        return {
            "must_change": [
                item.model_dump(mode="json") for item in result.by_level(ImpactLevel.MUST_CHANGE)
            ],
            "suggest_check": [
                item.model_dump(mode="json") for item in result.by_level(ImpactLevel.SUGGEST_CHECK)
            ],
            "total": len(result.items),
            "cost_budget": _deterministic_cost_budget("impact_of"),
        }


def propose_fix(
    *,
    content_root: str,
    issue_id: str,
    sqlite_path: str | None = None,
    max_candidates: int = 3,
) -> dict[str, Any]:
    """Propose shadow-validated fix candidates for a persisted audit issue.

    Read-only with respect to content files: candidates are stored as *proposed* patches in the
    runtime DB. Applying them is deliberately NOT an MCP tool — the human write path stays in
    the CLI/UI.
    """
    with _project(content_root, sqlite_path) as project:
        issue = find_issue(project, issue_id)
        result = suggest_for_issue(project, issue, max_candidates=max_candidates)
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
            "apply_hint": "apply via CLI: owcopilot apply --patch-id <id> --operator <name>",
            "cost_budget": _deterministic_cost_budget("propose_fix"),
        }


def export_project(
    *,
    content_root: str,
    output_dir: str,
    target_engine: str = EngineTarget.GENERIC.value,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Export project content to engine-friendly JSON files."""
    with _project(content_root, sqlite_path) as project:
        engine = EngineTarget(target_engine)
        actual_output = Path(output_dir) / engine.value
        manifest = export_content_bundle(project.bundle, actual_output, target_engine=engine)
        return {
            "output_dir": str(actual_output),
            "manifest": manifest.model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("export_project"),
        }


@contextmanager
def _project(content_root: str, sqlite_path: str | None) -> Iterator[ProjectContext]:
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
