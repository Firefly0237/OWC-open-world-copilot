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
from ..pipeline.export_gate import assert_export_ready
from ..pipeline.harness import run_quality_harness
from ..pipeline.patches import find_issue, suggest_for_issue
from ..pipeline.project import ProjectContext
from ..qa.offline import OfflineQAProvider
from ..qa.service import LoreQAService
from ..storage import SQLiteStore
from ..telemetry import deterministic_step, llm_step, summarize_workflow


def audit_project(
    *,
    content_root: str,
    sqlite_path: str | None = None,
    persist: bool = True,
    project: ProjectContext | None = None,
) -> dict[str, Any]:
    """Run the default deterministic audit rules for a project."""
    with _project(content_root, sqlite_path, shared=project) as project:
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
    project: ProjectContext | None = None,
) -> dict[str, Any]:
    """List persisted audit issues for a project.

    Thin path: this tool only reads the ``issues`` table, so when no shared :class:`ProjectContext`
    is injected it opens just a :class:`SQLiteStore` on the runtime DB — no content load, no graph
    build, no :class:`VectorRetriever` reindex. The ``issues`` table schema is created in
    ``SQLiteStore.initialize`` (i.e. on connect), so querying an as-yet-unpopulated runtime DB is
    safe and returns an empty list rather than failing. When a shared ctx *is* injected the call
    reuses its already-open store (so issues another tool just persisted are visible).
    """
    with _issues_store(content_root, sqlite_path, shared=project) as store:
        # Treat an empty-string filter as "no filter" — a tool-calling model commonly passes ""
        # to mean "unset" (real DeepSeek did exactly this), which would otherwise match no rows.
        issues = store.list_issues(
            severity=severity or None,
            rule_code=rule_code or None,
            status=status or None,
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
    project: ProjectContext | None = None,
) -> dict[str, Any]:
    """Build a retrieval context pack for a lore query."""
    with _project(content_root, sqlite_path, shared=project) as project:
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
    project: ProjectContext | None = None,
) -> dict[str, Any]:
    """Answer a lore question with grounded citations."""
    with _project(content_root, sqlite_path, shared=project) as project:
        telemetry = TelemetryCollector()
        answer = LoreQAService(
            gateway=LLMGateway(
                providers={"cheap": OfflineQAProvider()},
                router=StaticRouter(mapping={"qa_answer": "cheap", "qa_expand": "cheap"}),
                cache=NoOpCache(),
                telemetry=telemetry,
            ),
            context_builder=project.qa_context_builder(),
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
    project: ProjectContext | None = None,
) -> dict[str, Any]:
    """Preview which content a planned change would touch (pure graph traversal, no LLM).

    Each change is {"change_type": "...", "target_ref": "..."}; change types:
    entity_rename, entity_delete, entity_field_change, relation_change, content_change.
    """
    with _project(content_root, sqlite_path, shared=project) as project:
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
    project: ProjectContext | None = None,
) -> dict[str, Any]:
    """Propose shadow-validated fix candidates for a persisted audit issue.

    Read-only with respect to content files: candidates are stored as *proposed* patches in the
    runtime DB. Applying them is deliberately NOT an MCP tool — the human write path stays in
    the CLI/UI.
    """
    with _project(content_root, sqlite_path, shared=project) as project:
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


def quality_harness(
    *,
    content_root: str,
    sqlite_path: str | None = None,
    propose_fixes: bool = True,
    max_issues: int = 5,
    max_candidates_per_issue: int = 1,
    project: ProjectContext | None = None,
) -> dict[str, Any]:
    """Run the MCP-safe quality loop: audit, gates, readiness, proposals, next tool calls.

    This is the harness entrypoint an external agent should call before editing or exporting. It
    may persist audit rows and proposed patches, but it never writes canon content.
    """
    with _project(content_root, sqlite_path, shared=project) as project:
        report = run_quality_harness(
            project,
            propose_fixes=propose_fixes,
            max_issues=max_issues,
            max_candidates_per_issue=max_candidates_per_issue,
        )
        return {
            **report.model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("quality_harness"),
        }


def export_project(
    *,
    content_root: str,
    output_dir: str,
    target_engine: str = EngineTarget.GENERIC.value,
    sqlite_path: str | None = None,
    project: ProjectContext | None = None,
) -> dict[str, Any]:
    """Export project content to engine-friendly JSON files."""
    with _project(content_root, sqlite_path, shared=project) as project:
        engine = EngineTarget(target_engine)
        actual_output = Path(output_dir) / engine.value
        assert_export_ready(project)
        manifest = export_content_bundle(project.bundle, actual_output, target_engine=engine)
        return {
            "output_dir": str(actual_output),
            "manifest": manifest.model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("export_project"),
        }


@contextmanager
def _project(
    content_root: str,
    sqlite_path: str | None,
    *,
    shared: ProjectContext | None = None,
) -> Iterator[ProjectContext]:
    """Yield a :class:`ProjectContext` for a tool handler.

    Two modes, selected by the (non-model-facing) ``shared`` argument:

    * ``shared is None`` (default, unchanged behaviour): open a fresh context for this single call
      and close it on exit. This is what the CLI single-shot commands, the unit tests and the
      ``service`` paths rely on, so leaving ``shared`` unset is byte-for-byte identical to before.
    * ``shared`` is an already-open context: yield it as-is and do NOT open or close it. The owner
      of that context (e.g. an agent session) is responsible for its lifecycle. This lets every
      tool call within one task reuse the same context — one parse/graph/vector build per task
      instead of one per ReAct step — and makes writes immediately visible to later tools (same
      live ``SQLiteStore`` connection).
    """
    if shared is not None:
        yield shared
        return
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


@contextmanager
def _issues_store(
    content_root: str,
    sqlite_path: str | None,
    *,
    shared: ProjectContext | None = None,
) -> Iterator[SQLiteStore]:
    """Yield just a :class:`SQLiteStore` for issue-table reads (the ``list_issues`` thin path).

    When a shared context is injected, reuse its already-open store (so issues persisted by a
    prior tool in the same session are visible). Otherwise open *only* a ``SQLiteStore`` on the
    runtime DB — skipping the content load / graph build / vector reindex that a full
    :class:`ProjectContext` would do — and close it on exit. ``runtime.sqlite`` is rebuildable
    runtime state; the ``issues`` table is created on connect (``SQLiteStore.initialize``), so a
    not-yet-populated DB simply yields an empty result rather than an error. Correctness is
    unaffected: this tool reads no content/graph/vector data, only the ``issues`` table.
    """
    if shared is not None:
        yield shared.sqlite_store
        return
    root = Path(content_root)
    if not root.exists():
        raise FileNotFoundError(f"content root does not exist: {root}")
    runtime_path = Path(sqlite_path) if sqlite_path else root / ".owcopilot" / "runtime.sqlite"
    if str(runtime_path) != ":memory:":
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(runtime_path)
    try:
        yield store
    finally:
        store.close()


def _deterministic_cost_budget(step_name: str) -> dict[str, Any]:
    return summarize_workflow([deterministic_step(step_name)]).budget.model_dump(mode="json")
