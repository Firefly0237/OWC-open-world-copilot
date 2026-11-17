# MCP 工具处理器：不硬依赖 MCP 传输 SDK。
# 刻意只暴露安全子集（诊断/提议），与 skills 面镜像；canon 写入不在此。
"""MCP tool handlers without a hard dependency on an MCP transport SDK."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from ..config import DEFAULT_CHEAP_MODEL, DEFAULT_LLM_MODE
from ..content.hash import content_hash
from ..exporters import EngineTarget, export_content_bundle
from ..impact import Change, ChangeSet, ChangeType, ImpactAnalyzer, ImpactLevel
from ..llm.cache import NoOpCache
from ..llm.gateway import LLMGateway, require_offline_llm_allowed
from ..llm.resilience import build_real_provider
from ..llm.router import StaticRouter
from ..llm.telemetry import TelemetryCollector
from ..logic.xref import build_flag_xref
from ..pipeline.audit import run_full_audit
from ..pipeline.export_gate import assert_export_ready
from ..pipeline.harness import run_quality_harness
from ..pipeline.patches import find_issue, suggest_for_issue
from ..pipeline.project import ProjectContext
from ..qa.offline import OfflineQAProvider
from ..qa.service import LoreQAService
from ..storage import SQLiteStore
from ..telemetry import deterministic_step, llm_step, summarize_workflow
from ..trust.security import PathSecurityError, resolve_under_root
from ..util import load_dotenv


class AuditProjectPayload(TypedDict):
    """Stable MCP output contract for ``audit_project`` structured results."""

    content_hash: str
    audit_run: dict[str, Any]
    issues: list[dict[str, Any]]
    open_errors: int
    cost_budget: dict[str, Any]
    total: int
    truncation_notice: str | None


@dataclass(frozen=True)
class ToolSafety:
    """MCP-annotation facts for one tool, kept SDK-free (plain booleans; transport.py maps these to
    ``mcp.types.ToolAnnotations``). ``read_only`` follows the literal MCP definition — a tool that
    persists runtime-DB rows or writes files is NOT read-only, regardless of the agent-side
    "never writes canon" intent axis. MCP defines ``destructive=False`` as *only additive*, so a
    tool that replaces rows, changes status, or overwrites exports is marked destructive even when
    the data can be regenerated. ``idempotent`` is true only where repeated effects are proven;
    ``open_world`` True when the tool can reach an external system (the real-model LLM path)."""

    read_only: bool
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False


#: Safety facts per registered MCP tool (keyed by handler name). Read tools are read-only; the four
#: that persist rows / write files are not. Every current writer can replace/update state rather
#: than being strictly additive, and none is idempotent (fresh run ids / timestamps make repeats
#: differ), so clients must not silently replay an outcome-unknown call.
TOOL_SAFETY: dict[str, ToolSafety] = {
    "list_issues": ToolSafety(read_only=True),
    "build_context_pack": ToolSafety(read_only=True),
    "search_references": ToolSafety(read_only=True),
    "flag_xref": ToolSafety(read_only=True),
    "impact_of": ToolSafety(read_only=True),
    "ask_lore": ToolSafety(read_only=True, open_world=True),  # real mode hits the external LLM API
    "audit_project": ToolSafety(read_only=False, destructive=True),
    "propose_fix": ToolSafety(read_only=False, destructive=True),
    "quality_harness": ToolSafety(read_only=False, destructive=True),
    "export_project": ToolSafety(read_only=False, destructive=True),
}

# Session-level shared ProjectContext injection.
#
# The shared context is passed *out of band* via this ContextVar rather than as a handler
# parameter. That keeps every handler signature clean for FastMCP: ``func_metadata`` builds a
# pydantic model from the signature with ``arbitrary_types_allowed=True`` and calls
# ``model_json_schema()`` at ``server.tool()`` registration — a ``project: ProjectContext | None``
# parameter would make pydantic recurse into ProjectContext's arbitrary-typed fields and raise,
# breaking registration of all tools. A ContextVar is invisible to the signature, so the MCP
# server registers cleanly, while still letting a session owner (e.g. the CLI agent commands)
# share one open context across many in-task tool calls.
#
# When unset (default), every helper below behaves exactly as before: open a fresh context per
# call and close it. When set, the helpers reuse the shared context without opening or closing it
# (the owner that set the var owns its lifecycle). The current callers are single-threaded and
# synchronous (the multi-agent workers run sequentially through one registry), so the var is
# visible to every tool call in the task. If a future caller runs a worker on a *new* thread the
# ContextVar simply will not propagate there, so that worker falls back to the self-managed open
# path — still correct, only missing the reuse optimisation.
_shared_project: ContextVar[ProjectContext | None] = ContextVar("owc_shared_project", default=None)


def audit_project(
    *,
    content_root: str,
    sqlite_path: str | None = None,
    persist: bool = True,
    limit: int | None = None,
) -> AuditProjectPayload:
    """Run the default deterministic audit rules for a project.

    ``limit=None`` (default) returns every issue. ``total`` is always the pre-page count and
    ``truncation_notice`` is null unless a non-None ``limit`` omits results, so the structured MCP
    output has one stable schema and a bounded page is never a silent drop."""
    with _project(content_root, sqlite_path) as project:
        result = run_full_audit(project, persist=persist)
        issues = list(result.issues)
        payload: AuditProjectPayload = {
            "content_hash": content_hash(project.bundle),
            "audit_run": result.run.model_dump(mode="json"),
            "issues": [issue.model_dump(mode="json") for issue in issues],
            "open_errors": len(result.open_errors),
            "cost_budget": _deterministic_cost_budget("audit_project"),
            "total": len(issues),
            "truncation_notice": None,
        }
        if limit is not None and len(issues) > limit:
            payload["issues"] = payload["issues"][:limit]
            payload["truncation_notice"] = (
                f"showing {limit} of {len(issues)} issues; use list_issues with "
                f"limit/offset to page the rest"
            )
        return payload


def list_issues(
    *,
    content_root: str,
    sqlite_path: str | None = None,
    severity: str | None = None,
    rule_code: str | None = None,
    status: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """List persisted audit issues for a project.

    Thin path: this tool only reads the ``issues`` table, so when no shared :class:`ProjectContext`
    is set it opens just a :class:`SQLiteStore` on the runtime DB — no content load, no graph
    build, no :class:`VectorRetriever` reindex. The ``issues`` table schema is created in
    ``SQLiteStore.initialize`` (i.e. on connect), so querying an as-yet-unpopulated runtime DB is
    safe and returns an empty list rather than failing. When a shared ctx *is* set the call reuses
    its already-open store (so issues another tool just persisted are visible).

    ``limit=None`` (default) returns every match with the original ``{count, issues, cost_budget}``
    keys. A non-None ``limit`` pages the results (SQL ``LIMIT/OFFSET``) and adds ``total`` plus,
    when the page omits later rows, a ``truncation_notice`` with the ``next_offset`` — the caller
    is always told how to reach the rest, never silently cut off.
    """
    with _issues_store(content_root, sqlite_path) as store:
        # Treat an empty-string filter as "no filter" — a tool-calling model commonly passes ""
        # to mean "unset" (real DeepSeek did exactly this), which would otherwise match no rows.
        filters = {
            "severity": severity or None,
            "rule_code": rule_code or None,
            "status": status or None,
        }
        issues = store.list_issues(**filters, limit=limit, offset=offset)
        payload: dict[str, Any] = {
            "count": len(issues),
            "issues": [issue.model_dump(mode="json") for issue in issues],
            "cost_budget": _deterministic_cost_budget("list_issues"),
        }
        if limit is not None:
            total = store.count_issues(**filters)
            payload["total"] = total
            if offset + len(issues) < total:
                payload["next_offset"] = offset + len(issues)
                payload["truncation_notice"] = (
                    f"showing {len(issues)} of {total} issues from offset {offset}; "
                    f"call again with offset={offset + len(issues)} for the next page"
                )
        return payload


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


def search_references(
    *,
    content_root: str,
    query: str,
    sqlite_path: str | None = None,
    budget_tokens: int = 1000,
) -> dict[str, Any]:
    """Retrieval lookup over the inspiration reference library (grounding material for drafting —
    never canon; the agent-facing counterpart of ``build_context_pack``, over the reference store
    instead of the content graph). No new retrieval logic here: the body calls the same
    ``ReferenceContextBuilder.build`` the CLI ``reference search`` command and the REST
    ``:search`` endpoint call.
    """
    with _project(content_root, sqlite_path) as project:
        pack = project.reference_context_builder.build(query, budget_tokens=budget_tokens)
        return {
            "query": query,
            "refs": pack.refs,
            "hits": [hit.model_dump(mode="json") for hit in pack.hits],
            "cost_budget": _deterministic_cost_budget("search_references"),
        }


def _qa_gateway() -> tuple[LLMGateway, TelemetryCollector]:
    """QA gateway honouring the deployment-level LLM mode (same convention as CLI/REST/app).

    MCP tools carry no per-call llm flags — the *calling agent* must never choose the provider —
    so the mode comes from the server's environment: ``OWCOPILOT_LLM_MODE=real`` serves real
    answers via the configured provider; anything else is the offline double, which stays
    fail-closed behind ``OWCOPILOT_ALLOW_OFFLINE_LLM`` exactly like every other runtime surface.
    """
    telemetry = TelemetryCollector()
    real = os.getenv("OWCOPILOT_LLM_MODE", DEFAULT_LLM_MODE) == "real"
    if real:
        load_dotenv()  # pick up provider keys from .env; shell env wins
        provider: Any = build_real_provider(
            model=os.getenv("OWCOPILOT_CHEAP_MODEL", DEFAULT_CHEAP_MODEL)
        )
    else:
        require_offline_llm_allowed()  # offline fakes are a test/CI fixture, not a product mode
        provider = OfflineQAProvider()
    gateway = LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={"qa_answer": "cheap", "qa_expand": "cheap"}),
        cache=NoOpCache(),
        telemetry=telemetry,
        max_retries=1 if real else 0,
        retry_backoff_seconds=1.0 if real else 0.0,
    )
    return gateway, telemetry


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
        gateway, telemetry = _qa_gateway()
        answer = LoreQAService(
            gateway=gateway,
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


def flag_xref(
    *,
    content_root: str,
    sqlite_path: str | None = None,
    flag: str | None = None,
) -> dict[str, Any]:
    """Cross-reference world state flags: who declares, writes and reads each one (no LLM).

    This is the "who touched this flag?" lookup a narrative editor ships as a where-used report.
    ``flag`` narrows the answer to one variable; omitted, every flag is returned. ``verdicts`` per
    flag names the decidable problems (conflicting declarations / a gate nothing can open / a write
    nothing reads); ``reads_complete=false`` means some guard failed to parse, so "nobody reads it"
    is withheld world-wide rather than guessed.
    """
    with _project(content_root, sqlite_path) as project:
        payload = build_flag_xref(project.bundle).report(flag)
        payload["cost_budget"] = _deterministic_cost_budget("flag_xref")
        return payload


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


def quality_harness(
    *,
    content_root: str,
    sqlite_path: str | None = None,
    propose_fixes: bool = True,
    max_issues: int = 5,
    max_candidates_per_issue: int = 1,
) -> dict[str, Any]:
    """Run the MCP-safe quality loop: audit, gates, readiness, proposals, next tool calls.

    This is the harness entrypoint an external agent should call before editing or exporting. It
    may persist audit rows and proposed patches, but it never writes canon content.
    """
    with _project(content_root, sqlite_path) as project:
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
    row_struct_map: dict[str, dict[str, str]] | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Export project content to engine-friendly JSON files.

    ``output_dir`` is confined to an export root — ``$OWCOPILOT_EXPORT_ROOT`` if set, else
    ``<content_root>/.owcopilot/exports`` — via :func:`resolve_under_root`, so a model/client
    supplying a traversal or absolute sibling path cannot write outside the sandbox (the same guard
    the REST export uses). An escape raises a guided ``ValueError``, never a silent write.

    ``row_struct_map`` (UNREAL target only) renames DataTable columns to the target project's own
    USTRUCT field names, keyed by table (``{"<table>": {"<CanonicalField>": "<ExportName>"}}``).
    Omit it to keep the default field names — the export is then byte-for-byte as before."""
    with _project(content_root, sqlite_path) as project:
        engine = EngineTarget(target_engine)
        export_root = os.getenv("OWCOPILOT_EXPORT_ROOT") or str(
            Path(content_root) / ".owcopilot" / "exports"
        )
        try:
            confined = resolve_under_root(export_root, output_dir)
        except PathSecurityError as exc:
            raise ValueError(
                f"export output_dir {output_dir!r} escapes the allowed export root "
                f"{export_root!r}; pass a path inside it, or set OWCOPILOT_EXPORT_ROOT to widen it."
            ) from exc
        actual_output = confined / engine.value
        assert_export_ready(project)
        manifest = export_content_bundle(
            project.bundle,
            actual_output,
            target_engine=engine,
            row_struct_map=row_struct_map,
        )
        return {
            "output_dir": str(actual_output),
            "manifest": manifest.model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("export_project"),
        }


@contextmanager
def _project(content_root: str, sqlite_path: str | None) -> Iterator[ProjectContext]:
    """Yield a :class:`ProjectContext` for a tool handler.

    Two modes, selected by the :data:`_shared_project` ContextVar (set out of band by a session
    owner — never a handler parameter, so the signature stays clean for FastMCP schema-gen):

    * unset (default, unchanged behaviour): open a fresh context for this single call and close it
      on exit. This is what the CLI single-shot commands, the unit tests and the ``service`` paths
      rely on, so leaving the var unset is byte-for-byte identical to before.
    * set to an already-open context: yield it as-is and do NOT open or close it. The owner of that
      context (e.g. an agent session) is responsible for its lifecycle. This lets every tool call
      within one task reuse the same context — one parse/graph/vector build per task instead of one
      per ReAct step — and makes writes immediately visible to later tools (same live
      ``SQLiteStore`` connection).
    """
    shared = _shared_project.get()
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
def _issues_store(content_root: str, sqlite_path: str | None) -> Iterator[SQLiteStore]:
    """Yield just a :class:`SQLiteStore` for issue-table reads (the ``list_issues`` thin path).

    When a shared context is set (see :data:`_shared_project`), reuse its already-open store (so
    issues persisted by a prior tool in the same session are visible). Otherwise open *only* a
    ``SQLiteStore`` on the runtime DB — skipping the content load / graph build / vector reindex
    that a full :class:`ProjectContext` would do — and close it on exit. ``runtime.sqlite`` is
    rebuildable runtime state; the ``issues`` table is created on connect
    (``SQLiteStore.initialize``), so a not-yet-populated DB simply yields an empty result rather
    than an error. Correctness is unaffected: this tool reads no content/graph/vector data, only
    the ``issues`` table.
    """
    shared = _shared_project.get()
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
