from __future__ import annotations

import json
import logging

from owcopilot.agent import ReActAgent, persist_agent_run
from owcopilot.agent.offline import OfflineReactProvider
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.skills import default_skill_registry
from owcopilot.storage.sqlite import SQLiteStore

GOAL = "Get this world ready to export."


def _dirty_project(content_root) -> None:
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


def _gateway(provider) -> LLMGateway:
    return LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={"agent_react": "cheap"}),
        cache=NoOpCache(),
    )


def _real_result(content_root):
    """Drive the REAL ReAct agent (offline double) over a dirty world -> a genuine AgentResult."""
    registry = default_skill_registry(content_root=str(content_root))
    return ReActAgent(gateway=_gateway(OfflineReactProvider()), registry=registry, max_steps=6).run(
        GOAL
    )


# --------------------------------------------------------------------------- store layer
def test_save_list_get_agent_run_roundtrip() -> None:
    store = SQLiteStore(":memory:")
    transcript_json = '{"goal":"g","steps":[]}'
    store.save_agent_run(
        run_id="r1",
        goal="g",
        stop_reason="finished",
        step_count=3,
        cost_usd=0.0,
        latency_ms=1.5,
        agent_name="world-consistency",
        transcript_json=transcript_json,
    )

    listed = store.list_agent_runs()
    assert len(listed) == 1
    row = listed[0]
    assert row["id"] == "r1"
    assert row["goal"] == "g"
    assert row["stop_reason"] == "finished"
    assert row["step_count"] == 3
    assert row["cost_usd"] == 0.0
    assert row["latency_ms"] == 1.5
    assert row["agent_name"] == "world-consistency"
    # The list row is lightweight: it must NOT carry the heavy transcript.
    assert "transcript_json" not in row
    assert "transcript" not in row

    got = store.get_agent_run("r1")
    assert got is not None
    assert got["transcript"] == json.loads(transcript_json)
    assert store.get_agent_run("nope") is None


def test_save_agent_run_idempotent_on_run_id() -> None:
    store = SQLiteStore(":memory:")
    common = dict(
        goal="g",
        stop_reason="finished",
        cost_usd=0.0,
        latency_ms=0.0,
        agent_name="world-consistency",
        transcript_json="{}",
    )
    store.save_agent_run(run_id="dup", step_count=1, **common)
    store.save_agent_run(run_id="dup", step_count=9, **common)

    listed = store.list_agent_runs()
    assert len(listed) == 1
    assert listed[0]["step_count"] == 9  # second (latest) write wins


def test_list_agent_runs_newest_first_and_limit() -> None:
    store = SQLiteStore(":memory:")
    common = dict(
        goal="g",
        stop_reason="finished",
        step_count=0,
        cost_usd=0.0,
        latency_ms=0.0,
        agent_name="world-consistency",
        transcript_json="{}",
    )
    store.save_agent_run(run_id="a", created_at="2020-01-01T00:00:00+00:00", **common)
    store.save_agent_run(run_id="b", created_at="2020-01-02T00:00:00+00:00", **common)
    store.save_agent_run(run_id="c", created_at="2020-01-03T00:00:00+00:00", **common)

    assert [row["id"] for row in store.list_agent_runs()] == ["c", "b", "a"]
    assert len(store.list_agent_runs(limit=2)) == 2


def test_agent_runs_table_is_additive() -> None:
    store = SQLiteStore(":memory:")
    tables = {
        str(row["name"])
        for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "agent_runs" in tables
    # The migration is additive: the pre-existing runtime tables are still present.
    for existing in ("audit_runs", "issues", "telemetry", "agent_messages"):
        assert existing in tables


# --------------------------------------------------------------------------- helper (real agent)
def test_persist_agent_run_persists_real_result(tmp_path) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    result = _real_result(content_root)

    store = SQLiteStore(":memory:")
    run_id = persist_agent_run(store, result)
    assert run_id is not None

    stored = store.get_agent_run(run_id)
    assert stored is not None
    assert stored["goal"] == result.goal
    assert stored["stop_reason"] == result.stop_reason
    assert stored["step_count"] == result.step_count
    assert len(stored["transcript"]["steps"]) == len(result.steps)
    assert stored["transcript"]["steps"][0]["action"] == result.steps[0].action


def test_persist_is_side_effect_free_on_result(tmp_path) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    result = _real_result(content_root)

    before = result.model_dump(mode="json")
    persist_agent_run(SQLiteStore(":memory:"), result)
    # AC④: persistence must never mutate the agent result — byte-identical dump before/after.
    assert result.model_dump(mode="json") == before


def test_persist_returns_none_on_unwritable_store_and_logs_warning(tmp_path, caplog) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    result = _real_result(content_root)

    store = SQLiteStore(":memory:")
    store.close()  # a real store whose connection is now unusable
    with caplog.at_level(logging.WARNING, logger="owcopilot.agent.history"):
        rv = persist_agent_run(store, result)
    assert rv is None  # the inner failure is caught, not raised
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


# --------------------------------------------------------------------------- CLI
def test_cli_agent_persists_then_history_lists_and_shows(tmp_path, capsys) -> None:
    from owcopilot.cli.main import main

    content_root = tmp_path / "content"
    _dirty_project(content_root)

    # 1) An agent run persists its transcript and surfaces the id in the payload.
    code = main(
        ["agent", "--content-root", str(content_root), "--goal", GOAL, "--llm-mode", "offline"]
    )
    assert code == 0
    agent_payload = json.loads(capsys.readouterr().out)
    run_id = agent_payload["agent_run_id"]
    assert isinstance(run_id, str) and run_id

    # 2) agent-history lists the run (default sqlite path is shared across both main() calls).
    code = main(["agent-history", "--content-root", str(content_root)])
    assert code == 0
    list_payload = json.loads(capsys.readouterr().out)
    assert list_payload["count"] == 1
    assert list_payload["runs"][0]["goal"] == GOAL
    assert list_payload["runs"][0]["stop_reason"] == "finished"
    assert list_payload["runs"][0]["id"] == run_id
    # The listing row stays lightweight (no transcript).
    assert "transcript" not in list_payload["runs"][0]

    # 3) --run-id shows the full transcript of that run.
    code = main(["agent-history", "--content-root", str(content_root), "--run-id", run_id])
    assert code == 0
    show_payload = json.loads(capsys.readouterr().out)
    assert show_payload["run"]["goal"] == GOAL
    assert show_payload["run"]["step_count"] == len(show_payload["run"]["transcript"]["steps"])
    assert [step["action"] for step in show_payload["run"]["transcript"]["steps"]] == [
        "audit_project",
        "build_context_pack",
        "quality_harness",
    ]


def test_cli_agent_history_unknown_run_id_is_friendly_error(tmp_path) -> None:
    from owcopilot.cli.main import main

    content_root = tmp_path / "content"
    _dirty_project(content_root)
    # A missing run id raises a guided ValueError -> main() prints JSON error, exits 2.
    code = main(
        ["agent-history", "--content-root", str(content_root), "--run-id", "does-not-exist"]
    )
    assert code == 2
