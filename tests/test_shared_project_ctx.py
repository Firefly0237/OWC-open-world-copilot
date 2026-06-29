"""SCALE-P0 #2b: shared ProjectContext injection + list_issues thin path.

These tests pin the two halves of 2b:

* The skill registry / tool handlers reuse ONE injected ProjectContext for a whole session
  (one ``ProjectContext.open`` per task instead of one per tool call), while leaving the
  default (no injection) path byte-for-byte unchanged — every call opens and closes its own.
* ``list_issues`` runs a thin path that touches only the ``issues`` table: no content load,
  no graph build, no VectorRetriever — yet returns correct rows, including the shared-ctx
  write-after-visible guarantee (audit persists issues → a later list_issues reads them).
"""

from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore
from owcopilot.core.skills import default_skill_registry
from owcopilot.mcp_server import tools
from owcopilot.pipeline.project import ProjectContext


def _dirty_project(content_root) -> None:
    """A world with one dangling quest reference, so an audit reports >=1 open error."""
    ContentStore(content_root).save(
        ContentBundle(
            entities={
                "npc_aldric": Entity(
                    id="npc_aldric",
                    name="Aldric",
                    type=EntityType.NPC,
                    description="Caravan master",
                )
            },
            quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_missing")},
        )
    )


# --------------------------------------------------------------------------- shared ctx reuse
def test_shared_ctx_is_reused_across_tool_calls_opening_once(tmp_path, monkeypatch) -> None:
    """Injecting an open shared ctx ⇒ ProjectContext.open is called exactly ONCE for the whole
    session (the manual open), never per tool call."""
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    sqlite_path = str(tmp_path / "runtime.sqlite")

    open_calls: list[str] = []
    real_open = ProjectContext.open

    def counting_open(cls, *args, **kwargs):  # type: ignore[no-untyped-def]
        open_calls.append("open")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(ProjectContext, "open", classmethod(counting_open))

    # One session-level open by the owner (mirrors the CLI _cmd_agent wiring).
    project = ProjectContext.open(content_root, sqlite_path=sqlite_path)
    try:
        registry = default_skill_registry(
            content_root=str(content_root), sqlite_path=sqlite_path, project=project
        )
        # Several tool calls in one "session". With a shared ctx, none of them should open again.
        registry.run("audit_project", {})
        registry.run("list_issues", {})
        registry.run("build_context_pack", {"query": "Aldric"})
        registry.run("quality_harness", {})
    finally:
        project.close()

    # Exactly the single owner-level open — proof the tools reused the injected context.
    assert open_calls == ["open"]


def test_no_injection_opens_and_closes_per_call(tmp_path, monkeypatch) -> None:
    """Backward compat: without an injected ctx, each tool call opens AND closes its own context."""
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    sqlite_path = str(tmp_path / "runtime.sqlite")

    open_calls: list[str] = []
    close_calls: list[str] = []
    real_open = ProjectContext.open
    real_close = ProjectContext.close

    def counting_open(cls, *args, **kwargs):  # type: ignore[no-untyped-def]
        open_calls.append("open")
        return real_open(*args, **kwargs)

    def counting_close(self):  # type: ignore[no-untyped-def]
        close_calls.append("close")
        return real_close(self)

    monkeypatch.setattr(ProjectContext, "open", classmethod(counting_open))
    monkeypatch.setattr(ProjectContext, "close", counting_close)

    # No project= argument ⇒ historical behaviour: a fresh open+close per call.
    registry = default_skill_registry(content_root=str(content_root), sqlite_path=sqlite_path)
    registry.run("audit_project", {})
    registry.run("build_context_pack", {"query": "Aldric"})

    # audit_project and build_context_pack each go through the full _project open+close.
    # (list_issues is deliberately excluded here — it takes the thin path, never ProjectContext.)
    assert open_calls == ["open", "open"]
    assert close_calls == ["close", "close"]


def test_shared_ctx_write_then_visible(tmp_path) -> None:
    """Write-after-visible: audit_project persists issues into the shared ctx's SQLiteStore, and a
    later list_issues (reusing that same ctx) reads them back — same live connection."""
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    sqlite_path = str(tmp_path / "runtime.sqlite")

    project = ProjectContext.open(content_root, sqlite_path=sqlite_path)
    try:
        registry = default_skill_registry(
            content_root=str(content_root), sqlite_path=sqlite_path, project=project
        )
        # Before any audit, no issues persisted yet.
        assert registry.run("list_issues", {})["count"] == 0

        audit = registry.run("audit_project", {})
        assert audit["open_errors"] >= 1

        # The very next list_issues call (same shared ctx) must see what audit just wrote.
        listed = registry.run("list_issues", {})
        assert listed["count"] == audit["open_errors"]
        assert listed["count"] >= 1
        # The rows are the same issues the audit returned.
        audit_ids = {issue["id"] for issue in audit["issues"]}
        listed_ids = {issue["id"] for issue in listed["issues"]}
        assert audit_ids == listed_ids
    finally:
        project.close()


# --------------------------------------------------------------------------- thin path
def test_list_issues_thin_path_skips_bundle_graph_vector(tmp_path, monkeypatch) -> None:
    """The list_issues thin path opens only a SQLiteStore: no ContentStore.load, no
    build_content_graph, no VectorRetriever construction."""
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    sqlite_path = str(tmp_path / "runtime.sqlite")

    # First, populate the runtime DB with persisted issues via a full audit (the legitimate way
    # issues get written). This uses a real ProjectContext and is NOT what we are asserting about.
    project = ProjectContext.open(content_root, sqlite_path=sqlite_path)
    try:
        audit = tools.audit_project(
            content_root=str(content_root), sqlite_path=sqlite_path, project=project
        )
    finally:
        project.close()
    assert audit["open_errors"] >= 1

    # Now arm tripwires on the heavy construction paths and call the thin list_issues (no ctx).
    import owcopilot.mcp_server.tools as tools_mod

    heavy: list[str] = []

    def trip_open(cls, *args, **kwargs):  # type: ignore[no-untyped-def]
        heavy.append("ProjectContext.open")
        raise AssertionError("thin path must not open a ProjectContext")

    monkeypatch.setattr(ProjectContext, "open", classmethod(trip_open))

    # Also trip the bundle/graph/vector builders at their import sites in the project module, so
    # even an alternative full-open route would be caught.
    import owcopilot.pipeline.project as project_mod

    def trip_graph(*args, **kwargs):  # type: ignore[no-untyped-def]
        heavy.append("build_content_graph")
        raise AssertionError("thin path must not build the content graph")

    monkeypatch.setattr(project_mod, "build_content_graph", trip_graph)

    class _TripVector:
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            heavy.append("VectorRetriever")
            raise AssertionError("thin path must not construct a VectorRetriever")

    monkeypatch.setattr(project_mod, "VectorRetriever", _TripVector)

    result = tools_mod.list_issues(content_root=str(content_root), sqlite_path=sqlite_path)

    assert heavy == []  # none of the heavy paths fired
    # ...and the thin path returns the correct, persisted rows.
    assert result["count"] == audit["open_errors"]
    assert {i["id"] for i in result["issues"]} == {i["id"] for i in audit["issues"]}


def test_list_issues_thin_path_on_unpopulated_db_returns_empty(tmp_path) -> None:
    """Boundary: a runtime DB never filled by any full open is a *legal empty* — the thin path
    returns an empty list (issues table exists from SQLiteStore.initialize), not an error."""
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    sqlite_path = str(tmp_path / "fresh-runtime.sqlite")

    # No audit, no full open has ever written to this DB.
    result = tools.list_issues(content_root=str(content_root), sqlite_path=sqlite_path)
    assert result["count"] == 0
    assert result["issues"] == []


def test_list_issues_thin_path_filters(tmp_path) -> None:
    """The thin path honours the severity/status filters (and empty-string-means-unset)."""
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    sqlite_path = str(tmp_path / "runtime.sqlite")

    project = ProjectContext.open(content_root, sqlite_path=sqlite_path)
    try:
        tools.audit_project(
            content_root=str(content_root), sqlite_path=sqlite_path, project=project
        )
    finally:
        project.close()

    everything = tools.list_issues(content_root=str(content_root), sqlite_path=sqlite_path)
    # Empty-string filters are treated as "unset" (matches the existing tool contract).
    unset = tools.list_issues(
        content_root=str(content_root), sqlite_path=sqlite_path, severity="", status=""
    )
    assert unset["count"] == everything["count"] >= 1
    # A status that no issue has ⇒ zero rows (proves the WHERE clause really runs).
    none_match = tools.list_issues(
        content_root=str(content_root), sqlite_path=sqlite_path, status="resolved"
    )
    assert none_match["count"] == 0
