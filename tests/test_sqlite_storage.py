from __future__ import annotations

from owcopilot.audit.models import AuditRun, Category, Evidence, Issue, Severity
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest, Relation
from owcopilot.graph.index import build_content_graph
from owcopilot.storage import SQLiteStore


def test_sqlite_store_initializes_runtime_tables() -> None:
    store = SQLiteStore()
    try:
        tables = {
            row["name"]
            for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
            )
        }

        assert "audit_runs" in tables
        assert "issues" in tables
        assert "patches" in tables
        assert "content_index" in tables
        assert "graph_edges" in tables
        assert "telemetry" in tables
    finally:
        store.close()


def test_sqlite_store_round_trips_audit_run_and_issue() -> None:
    store = SQLiteStore()
    try:
        run = AuditRun(id="run_1", content_hash="abc", totals={"error": 1})
        store.save_audit_run(run)
        issue = store.save_issue(
            Issue(
                rule_code="UNKNOWN_ENTITY",
                severity=Severity.ERROR,
                category=Category.REFERENCE,
                target_ref="quest:q1",
                message="Unknown entity",
                evidence=[Evidence(kind="field_path", path="giver_npc")],
                audit_run_id="run_1",
            )
        )

        loaded_run = store.get_audit_run("run_1")
        issues = store.list_issues(severity="error")

        assert loaded_run is not None
        assert loaded_run.content_hash == "abc"
        assert issue.id is not None
        assert issues[0].rule_code == "UNKNOWN_ENTITY"
        assert issues[0].evidence[0].path == "giver_npc"
    finally:
        store.close()


def test_sqlite_store_replaces_content_index_and_searches_fts() -> None:
    store = SQLiteStore()
    try:
        bundle = ContentBundle(
            entities={
                "npc_aldric": Entity(
                    id="npc_aldric",
                    name="Aldric",
                    type=EntityType.NPC,
                    description="Caravan master",
                )
            },
            quests={
                "quest_missing_caravan": Quest(
                    id="quest_missing_caravan",
                    title="Missing Caravan",
                    objective="Find the lost supply caravan",
                )
            },
        )

        store.replace_content_index(bundle)
        results = store.search_content("caravan")

        assert {row["ref"] for row in results} == {
            "entity:npc_aldric",
            "quest:quest_missing_caravan",
        }
    finally:
        store.close()


def test_sqlite_store_replaces_graph_edges() -> None:
    store = SQLiteStore()
    try:
        bundle = ContentBundle(
            entities={
                "npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC),
                "faction_iron_guard": Entity(
                    id="faction_iron_guard",
                    name="Iron Guard",
                    type=EntityType.FACTION,
                ),
            },
            relations=[
                Relation(source="npc_aldric", target="faction_iron_guard", kind="member_of")
            ],
        )
        graph = build_content_graph(bundle)

        store.replace_graph_edges(graph)
        rows = store.conn.execute("SELECT source, target, kind FROM graph_edges").fetchall()

        assert [(row["source"], row["target"], row["kind"]) for row in rows] == [
            ("entity:npc_aldric", "entity:faction_iron_guard", "member_of")
        ]
    finally:
        store.close()
