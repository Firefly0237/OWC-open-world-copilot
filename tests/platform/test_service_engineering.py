"""FastAPI contract tests for the read-only/propose-only engineering workbench."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.content.hash import content_hash  # noqa: E402
from owcopilot.content.models import (  # noqa: E402
    Branch,
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Effect,
    Entity,
    EntityType,
    LogicVar,
    LogicVarType,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
    StyleElementKind,
    StyleRubric,
    StyleRubricElement,
)
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.llm.telemetry import CallRecord, TelemetryCollector  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402
from owcopilot.storage import SQLiteStore  # noqa: E402


def _world() -> ContentBundle:
    quest = Quest(
        id="q_logic",
        title="旗标之路",
        giver_npc="npc_missing",
        stages=[QuestStage(id="s0", summary="作出选择")],
        logic=QuestLogic(
            variables=[LogicVar(id="shared", type=LogicVarType.BOOL, default=False)],
            stage_logic=[
                StageLogic(
                    stage_id="s0",
                    effects_on_complete=[Effect(var="shared", op="set", value=True)],
                )
            ],
            branches=[
                Branch(
                    id="b_finish",
                    from_stage="s0",
                    condition="shared == true",
                    outcome="completed",
                )
            ],
        ),
    )
    tree = DialogueTree(
        id="dlg_gate",
        title="门前问答",
        root_node="n0",
        nodes={
            "n0": DialogueNode(
                id="n0",
                text="是否继续？",
                choices=[DialogueChoice(text="继续", next_node=None)],
            )
        },
    )
    rubric = StyleRubric(
        id="wafu",
        label="和风清单",
        version="1",
        elements=[
            StyleRubricElement(
                id="katana",
                text="出现日本刀剑器物",
                kind=StyleElementKind.LEXICAL,
                terms=["武士刀"],
            )
        ],
    )
    return ContentBundle(
        entities={
            "npc_samurai": Entity(
                id="npc_samurai",
                name="剑客",
                type=EntityType.NPC,
                description="腰间挂着一把武士刀。",
            )
        },
        quests={quest.id: quest},
        dialogue_trees={tree.id: tree},
        style_rubrics={rubric.id: rubric},
    )


@pytest.fixture()
def engineering_client(tmp_path, monkeypatch) -> tuple[TestClient, object]:
    root = tmp_path / "content"
    ContentStore(root).save(_world())
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON",
        json.dumps({"demo": str(root).replace("\\", "/")}),
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.setenv("OWCOPILOT_RATE_LIMIT_PER_MIN", "100000")
    return TestClient(create_app()), root


def test_catalog_quest_dialogue_xref_and_style_are_real_deterministic_routes(
    engineering_client,
) -> None:
    client, _root = engineering_client

    catalog = client.get("/projects/demo/engineering/catalog")
    assert catalog.status_code == 200, catalog.text
    assert catalog.json()["quest_ids"] == ["q_logic"]
    assert catalog.json()["dialogue_tree_ids"] == ["dlg_gate"]
    assert catalog.json()["style_rubrics"][0]["id"] == "wafu"

    explored = client.post(
        "/projects/demo/engineering/quests/q_logic:explore",
        json={"max_states": 100, "max_depth": 20},
    )
    assert explored.status_code == 200, explored.text
    quest_report = explored.json()
    assert quest_report["execution_mode"] == "deterministic_bounded_search"
    assert quest_report["report"]["outcomes"][0]["reachable"] is True
    assert quest_report["report"]["outcomes"][0]["witness"] == ["b_finish"]
    assert quest_report["cost_budget"]["used_usd"] == 0.0

    walked = client.post(
        "/projects/demo/engineering/dialogues/dlg_gate:walk",
        json={"mode": "walk", "choices": ["n0:0"], "initial_state": {}},
    )
    assert walked.status_code == 200, walked.text
    assert walked.json()["result"]["status"] == "completed"
    assert walked.json()["result"]["path"] == ["n0"]

    enumerated = client.post(
        "/projects/demo/engineering/dialogues/dlg_gate:walk",
        json={"mode": "explore", "max_endings": 20},
    )
    assert enumerated.status_code == 200, enumerated.text
    assert enumerated.json()["result"]["covered_choices"] == ["n0:0"]

    xref = client.get("/projects/demo/engineering/flags:xref?flag=shared")
    assert xref.status_code == 200, xref.text
    assert xref.json()["flags"][0]["flag"] == "shared"
    assert xref.json()["flags"][0]["writers"]
    assert xref.json()["flags"][0]["readers"]

    swept = client.post(
        "/projects/demo/engineering/styles/wafu:sweep",
        json={"semantic_threshold": 0.5},
    )
    assert swept.status_code == 200, swept.text
    sweep = swept.json()
    assert sweep["llm_used"] is False
    assert sweep["judge_mode"] == "disabled"
    assert sweep["judge_limit"] == 400
    assert sweep["telemetry"]["calls"] == 0
    assert any(hit["ref"] == "entity:npc_samurai" for hit in sweep["hits"])


def test_repair_and_quality_harness_never_write_canon(engineering_client) -> None:
    client, root = engineering_client
    before = content_hash(ContentStore(root).load())

    planned = client.post(
        "/projects/demo/engineering/repairs:plan",
        json={"max_iterations": 25, "max_depth": 4, "seed": 7},
    )
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    assert plan["candidate_source"] == "deterministic_fixers"
    assert plan["used_llm"] is False
    assert plan["llm_mode"] == "offline"
    assert plan["telemetry"]["calls"] == 0
    assert plan["writes_canon"] is False
    assert plan["plan"]["initial_open_errors"] >= plan["plan"]["final_open_errors"]

    harness_response = client.post(
        "/projects/demo/engineering/quality:run",
        json={"propose_fixes": True, "max_issues": 5, "max_candidates_per_issue": 1},
    )
    assert harness_response.status_code == 200, harness_response.text
    harness = harness_response.json()
    assert harness["phase"] == "repair"
    assert harness["writes_canon"] is False
    assert harness["top_issues"]
    assert harness["next_tool_calls"][0]["tool"] == "list_issues"
    assert content_hash(ContentStore(root).load()) == before


def test_temporal_spec_translation_submission_and_review_are_a_two_phase_write(
    engineering_client,
) -> None:
    client, root = engineering_client
    store = ContentStore(root)
    before = content_hash(store.load())
    intent = "「shared」应当可达"

    translated_response = client.post(
        "/projects/demo/engineering/quests/q_logic/specs:translate",
        json={"intent": intent},
    )
    assert translated_response.status_code == 200, translated_response.text
    translated = translated_response.json()
    assert translated["translator_mode"] == "deterministic"
    assert translated["used_llm"] is False
    assert translated["degraded"] is False
    assert translated["telemetry"]["calls"] == 0
    assert translated["outcome"]["status"] == "needs_confirmation"
    assert translated["outcome"]["spec"]["pattern"] == "EF"
    assert translated["outcome"]["spec"]["expression"] == "shared"
    assert translated["writes_canon"] is False
    assert content_hash(store.load()) == before

    submitted_response = client.post(
        "/projects/demo/engineering/quests/q_logic/specs:submit",
        json={"intent": intent, "draft": translated["outcome"]["draft"]},
    )
    assert submitted_response.status_code == 200, submitted_response.text
    submitted = submitted_response.json()
    assert submitted["review_item"]["item_type"] == "temporal_spec_draft"
    assert submitted["review_item_id"] == submitted["review_item"]["id"]
    assert submitted["writes_canon"] is False
    # Queue persistence is allowed, but canon remains byte-for-byte unchanged until a human accepts.
    assert content_hash(store.load()) == before
    assert "temporal_specs" not in store.load().quests["q_logic"].metadata

    accepted_response = client.post(
        f"/projects/demo/review_items/{submitted['review_item_id']}:decide",
        json={"decision": "accepted", "operator": "spec-reviewer"},
    )
    assert accepted_response.status_code == 200, accepted_response.text
    assert accepted_response.json()["written_ref"] == "quest:q_logic"
    stored_specs = store.load().quests["q_logic"].metadata["temporal_specs"]
    assert stored_specs == [translated["outcome"]["spec"]]

    # The same stable candidate may be reviewed again, but acceptance never appends a duplicate id.
    duplicate_submit = client.post(
        "/projects/demo/engineering/quests/q_logic/specs:submit",
        json={"intent": intent, "draft": translated["outcome"]["draft"]},
    )
    assert duplicate_submit.status_code == 200, duplicate_submit.text
    duplicate_accept = client.post(
        f"/projects/demo/review_items/{duplicate_submit.json()['review_item_id']}:decide",
        json={"decision": "accepted", "operator": "spec-reviewer"},
    )
    assert duplicate_accept.status_code == 409
    assert len(store.load().quests["q_logic"].metadata["temporal_specs"]) == 1


def test_temporal_spec_submit_rejects_tampered_or_invalid_drafts(engineering_client) -> None:
    client, root = engineering_client
    before = content_hash(ContentStore(root).load())
    intent = "「shared」应当可达"

    translated = client.post(
        "/projects/demo/engineering/quests/q_logic/specs:translate",
        json={"intent": intent},
    ).json()
    tampered = dict(translated["outcome"]["draft"])
    tampered["operands"] = ["invented_flag"]
    rejected = client.post(
        "/projects/demo/engineering/quests/q_logic/specs:submit",
        json={"intent": intent, "draft": tampered},
    )
    assert rejected.status_code == 409
    assert "封闭世界" in rejected.json()["detail"]

    extra_field = dict(translated["outcome"]["draft"])
    extra_field["formula"] = "shared or invented_flag"
    invalid_shape = client.post(
        "/projects/demo/engineering/quests/q_logic/specs:submit",
        json={"intent": intent, "draft": extra_field},
    )
    assert invalid_shape.status_code == 422

    # Defense in depth at the write boundary: even a locally modified SQLite queue row is replayed
    # from intent+draft and must exactly match the server-assembled spec before it can land.
    valid_submit = client.post(
        "/projects/demo/engineering/quests/q_logic/specs:submit",
        json={"intent": intent, "draft": translated["outcome"]["draft"]},
    ).json()
    runtime = SQLiteStore(root / ".owcopilot" / "runtime.sqlite")
    try:
        row = runtime.get_review_item(valid_submit["review_item_id"])
        assert row is not None
        modified_payload = dict(row["payload"])
        modified_spec = dict(modified_payload["spec"])
        modified_spec["expression"] = "not shared"
        modified_payload["spec"] = modified_spec
        runtime.update_review_item(
            valid_submit["review_item_id"],
            status="pending_review",
            payload=modified_payload,
        )
    finally:
        runtime.close()
    tampered_queue_accept = client.post(
        f"/projects/demo/review_items/{valid_submit['review_item_id']}:decide",
        json={"decision": "accepted", "operator": "spec-reviewer"},
    )
    assert tampered_queue_accept.status_code == 409
    assert "服务器重建结果不一致" in tampered_queue_accept.json()["detail"]
    assert content_hash(ContentStore(root).load()) == before


def test_temporal_spec_accept_runs_the_deterministic_audit_before_saving(
    engineering_client,
) -> None:
    client, root = engineering_client
    store = ContentStore(root)
    before = content_hash(store.load())
    # `shared` is false in the initial reachable state, so AG shared is a real violation. The
    # candidate may be translated and reviewed, but the accept write gate must reject it.
    intent = "「shared」始终成立"
    translated = client.post(
        "/projects/demo/engineering/quests/q_logic/specs:translate",
        json={"intent": intent},
    ).json()
    assert translated["outcome"]["spec"]["pattern"] == "AG"
    submitted = client.post(
        "/projects/demo/engineering/quests/q_logic/specs:submit",
        json={"intent": intent, "draft": translated["outcome"]["draft"]},
    ).json()
    rejected = client.post(
        f"/projects/demo/review_items/{submitted['review_item_id']}:decide",
        json={"decision": "accepted", "operator": "spec-reviewer"},
    )
    assert rejected.status_code == 409
    assert "确定性审计" in rejected.json()["detail"]
    assert content_hash(store.load()) == before
    assert "temporal_specs" not in store.load().quests["q_logic"].metadata


def test_temporal_spec_action_reports_real_model_fallback_honestly(
    tmp_path, monkeypatch
) -> None:
    import owcopilot.app.actions.engineering as engineering_actions

    root = tmp_path / "spec-action-content"
    ContentStore(root).save(_world())
    telemetry = TelemetryCollector()

    class InvalidReplyGateway:
        def complete(self, **_kwargs) -> str:
            telemetry.record(
                CallRecord(
                    task="spec_translate",
                    tier="cheap",
                    input_tokens=8,
                    output_tokens=3,
                )
            )
            return "not structured JSON"

    monkeypatch.setattr(
        engineering_actions,
        "_gateway",
        lambda **_kwargs: (InvalidReplyGateway(), telemetry),
    )
    result = engineering_actions.translate_temporal_spec_action(
        root,
        quest_id="q_logic",
        intent="「shared」应当可达",
        use_llm=True,
        llm_mode="real",
        llm_model="test-model",
        max_cost_usd=0.01,
    )
    assert result["translator_mode"] == "real_model_fallback"
    assert result["used_llm"] is True
    assert result["degraded"] is True
    assert result["degradation_reasons"][0].startswith("invalid_structured_output:")
    assert result["outcome"]["status"] == "needs_confirmation"
    assert result["telemetry"]["calls"] == 1
    assert result["cost_budget"]["budget_usd"] == 0.01
    assert result["writes_canon"] is False


def _save_run(db_path, run_id: str, created_at: str) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(db_path)
    try:
        store.save_agent_run(
            run_id=run_id,
            goal=f"goal-{run_id}",
            stop_reason="finished",
            step_count=1,
            cost_usd=0.0,
            latency_ms=10.0,
            agent_name="diagnoser",
            transcript_json=json.dumps({"steps": [{"action": "audit_project"}]}),
            created_at=created_at,
        )
    finally:
        store.close()


def test_agent_history_uses_stable_server_cursor_and_detail(engineering_client) -> None:
    client, root = engineering_client
    db_path = root / ".owcopilot" / "runtime.sqlite"
    for number in range(1, 4):
        _save_run(db_path, f"run-{number}", f"2026-01-0{number}T00:00:00+00:00")

    first = client.get("/projects/demo/engineering/agent-runs?limit=2")
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["total"] == 3
    assert [run["id"] for run in first_body["runs"]] == ["run-3", "run-2"]
    assert first_body["next_cursor"]

    second = client.get(
        "/projects/demo/engineering/agent-runs",
        params={"limit": 2, "cursor": first_body["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    assert [run["id"] for run in second.json()["runs"]] == ["run-1"]
    assert second.json()["next_cursor"] is None

    detail = client.get("/projects/demo/engineering/agent-runs/run-2")
    assert detail.status_code == 200, detail.text
    assert detail.json()["run"]["transcript"]["steps"][0]["action"] == "audit_project"

    invalid = client.get(
        "/projects/demo/engineering/agent-runs",
        params={"limit": 2, "cursor": "not-a-cursor"},
    )
    assert invalid.status_code == 422

    _save_run(db_path, "run-4", "2026-01-04T00:00:00+00:00")
    stale = client.get(
        "/projects/demo/engineering/agent-runs",
        params={"limit": 2, "cursor": first_body["next_cursor"]},
    )
    assert stale.status_code == 409


def test_engineering_llm_options_are_bounded_and_offline_repair_is_deterministic(
    engineering_client,
) -> None:
    client, _root = engineering_client
    offline_plan = client.post(
        "/projects/demo/engineering/repairs:plan",
        json={"llm_candidates": True, "llm_mode": "offline", "max_llm_calls": 1},
    )
    assert offline_plan.status_code == 200, offline_plan.text
    assert offline_plan.json()["candidate_source"] == "deterministic_fixers"
    assert offline_plan.json()["used_llm"] is False

    offline_sweep = client.post(
        "/projects/demo/engineering/styles/wafu:sweep",
        json={"use_llm": True, "llm_mode": "offline", "max_judge": 1, "max_cost_usd": 0},
    )
    assert offline_sweep.status_code == 200, offline_sweep.text
    assert offline_sweep.json()["judge_mode"] == "offline_double"
    assert offline_sweep.json()["judge_limit"] == 1
    assert offline_sweep.json()["cost_budget"]["budget_usd"] == 0

    for payload in (
        {"max_llm_calls": 0},
        {"max_cost_usd": "NaN"},
        {"not_a_control": True},
    ):
        response = client.post("/projects/demo/engineering/repairs:plan", json=payload)
        assert response.status_code == 422
    assert (
        client.post(
            "/projects/demo/engineering/styles/wafu:sweep", json={"max_judge": 0}
        ).status_code
        == 422
    )


def test_engineering_real_model_options_use_existing_admission_gate(
    engineering_client, monkeypatch
) -> None:
    client, _root = engineering_client
    import owcopilot.service.api as api_module

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(api_module, "load_dotenv", lambda: None)
    repair = client.post(
        "/projects/demo/engineering/repairs:plan",
        json={"llm_candidates": True, "llm_mode": "real"},
    )
    style = client.post(
        "/projects/demo/engineering/styles/wafu:sweep",
        json={"use_llm": True, "llm_mode": "real"},
    )
    spec = client.post(
        "/projects/demo/engineering/quests/q_logic/specs:translate",
        json={"intent": "「shared」应当可达", "use_llm": True, "llm_mode": "real"},
    )
    assert repair.status_code == 503
    assert style.status_code == 503
    assert spec.status_code == 503


def test_engineering_requests_reject_unknown_options_and_missing_ids(engineering_client) -> None:
    client, _root = engineering_client
    assert (
        client.post(
            "/projects/demo/engineering/quests/no-such:explore",
            json={},
        ).status_code
        == 404
    )
    assert client.get("/projects/demo/engineering/flags:xref?flag=no-such").status_code == 409
    assert client.get("/projects/demo/engineering/agent-runs/no-such").status_code == 404


def test_repair_plan_action_reuses_model_candidate_provider_and_reports_actual_calls(
    tmp_path, monkeypatch
) -> None:
    import owcopilot.app.actions.engineering as engineering_actions

    root = tmp_path / "action-content"
    ContentStore(root).save(_world())
    telemetry = TelemetryCollector()

    class FakeGateway:
        def complete(self, **_kwargs) -> str:
            telemetry.record(
                CallRecord(
                    task="patch_suggest",
                    tier="cheap",
                    input_tokens=10,
                    output_tokens=2,
                )
            )
            return "[]"

    monkeypatch.setattr(
        engineering_actions,
        "_gateway",
        lambda **_kwargs: (FakeGateway(), telemetry),
    )
    result = engineering_actions.repair_plan_action(
        root,
        max_iterations=2,
        llm_candidates=True,
        max_llm_calls=1,
        llm_mode="real",
        llm_model="test-model",
        max_cost_usd=0.01,
    )
    assert result["candidate_source"] == "deterministic_plus_model"
    assert result["used_llm"] is True
    assert result["telemetry"]["calls"] == 1
    assert result["cost_budget"]["budget_usd"] == 0.01
    assert result["writes_canon"] is False


def test_style_sweep_action_wires_bounded_judge_and_reports_mode(tmp_path, monkeypatch) -> None:
    import owcopilot.app.actions.engineering as engineering_actions

    root = tmp_path / "style-action-content"
    ContentStore(root).save(_world())
    telemetry = TelemetryCollector()
    observed: dict[str, object] = {}

    class FakeGateway:
        pass

    class FakeRubricSweepService:
        def __init__(self, *, bundle, gateway, embedder) -> None:
            observed["gateway"] = gateway

        def sweep(self, rubric, **kwargs):
            observed.update(kwargs)
            telemetry.record(
                CallRecord(
                    task="theme_sweep",
                    tier="cheap",
                    input_tokens=20,
                    output_tokens=4,
                )
            )
            return SimpleNamespace(
                rubric_id=rubric.id,
                rubric_label=rubric.label,
                rubric_version=rubric.version,
                rubric_fingerprint="fingerprint",
                abstract_tag=rubric.abstract_tag,
                scanned_total=1,
                scanned_by_kind={"entity": 1},
                scanned_fields=2,
                semantic_used=False,
                semantic_threshold=0.0,
                judged_count=1,
                judge_skipped=0,
                dismissed_count=0,
                l1_total=1,
                l2_total=0,
                l1_l2_overlap=0,
                hits=[],
                review_suggested=[],
                elements=[],
            )

    monkeypatch.setattr(
        engineering_actions,
        "_gateway",
        lambda **_kwargs: (FakeGateway(), telemetry),
    )
    monkeypatch.setattr(engineering_actions, "RubricSweepService", FakeRubricSweepService)
    monkeypatch.setattr(engineering_actions, "render_rubric_markdown", lambda _report: "report")

    result = engineering_actions.style_sweep_action(
        root,
        rubric_id="wafu",
        use_llm=True,
        max_judge=7,
        llm_mode="real",
        llm_model="test-model",
        max_cost_usd=0.02,
    )
    assert observed["use_llm"] is True
    assert observed["max_judge"] == 7
    assert result["judge_mode"] == "real_model"
    assert result["llm_used"] is True
    assert result["telemetry"]["calls"] == 1
    assert result["cost_budget"]["budget_usd"] == 0.02
