"""REST tests for the agent diagnosis job (E1): the seventh job kind, offline and $0.

Everything below drives the REAL production chain — ``POST /projects/{p}/jobs`` → ``JobManager``
→ :func:`~owcopilot.app.actions.run_agent_diagnose_action` → ``ReActAgent`` → the built-in skill
registry — and only the reasoning model is a double. Covered:

* a plain offline run reports a grounded diagnosis and streams stage progress through the SAME
  submit/poll/SSE contract every other job kind uses (no second async mechanism);
* the agent's REST-visible action surface never contains a canon-writing skill (the human review
  queue stays the only writer, on this surface exactly as on the CLI);
* HITL form a over jobs: a run pauses at the human gate and ends the job with
  ``status="interrupted"``; the human's **approve** is the next job (``resume``+``decision``) and
  it runs the gated tool; **reject** skips it and feeds the model the rejection;
* misuse (blank goal, a decision without a resume, unknown params) produces Chinese guidance.
"""

from __future__ import annotations

import json
import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.app.actions import agent as agent_action  # noqa: E402
from owcopilot.content.models import (  # noqa: E402
    ContentBundle,
    Entity,
    EntityType,
    Quest,
    QuestStage,
)
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.fakes import scripted_chat_model  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402
from owcopilot.skills import SideEffect, default_skill_registry  # noqa: E402

_PROPOSE_TOOL = "propose_fix"


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    """A registered project with one dangling quest ref, so the audit has something to find."""
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            entities={
                "npc_aldric": Entity(
                    id="npc_aldric", name="奥德里克", type=EntityType.NPC, description="商队首领。"
                )
            },
            quests={
                "q_missing": Quest(
                    id="q_missing",
                    title="失落的信标",
                    summary="点亮枯叶林的信标。",
                    giver_ref="entity:npc_ghost",  # dangling on purpose
                    stages=[QuestStage(id="s1", summary="找到信标")],
                )
            },
        )
    )
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root).replace("\\", "/")})
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    # A diagnosis run is the longest job kind, so polling it to completion would trip the default
    # 60-requests/min limiter before the run finishes. Raised here only (the limiter itself has its
    # own tests); the frontend tails the SSE stream instead of polling.
    monkeypatch.setenv("OWCOPILOT_RATE_LIMIT_PER_MIN", "100000")
    return TestClient(create_app())


def _run_job(client: TestClient, kind: str, params: dict, *, attempts: int = 600) -> dict:
    created = client.post("/projects/demo/jobs", json={"kind": kind, "params": params})
    assert created.status_code == 202, created.text
    job_id = created.json()["job_id"]
    for _ in range(attempts):
        body = client.get(f"/jobs/{job_id}").json()
        if body.get("status") in {"done", "failed"}:
            return body
        time.sleep(0.1)
    raise AssertionError("agent job did not finish in time")


def _run_agent_job(client: TestClient, params: dict, *, attempts: int = 600) -> dict:
    return _run_job(client, "agent_diagnose", params, attempts=attempts)


def test_agent_job_diagnoses_offline_and_streams_stage_progress(client: TestClient) -> None:
    body = _run_agent_job(client, {"goal": "检查任务链一致性", "llm_mode": "offline"})
    assert body["status"] == "done", body
    result = body["result"]
    assert result["status"] == "completed"
    assert result["stop_reason"] == "finished"
    # Every observation came from a real deterministic tool, so the report is grounded.
    assert "audit_project" in [step["action"] for step in result["steps"]]
    assert result["final_answer"].strip()
    assert result["thread_id"]  # the checkpoint thread a resume would continue
    assert result["cost_budget"]["used_usd"] == 0.0  # offline double is $0
    # progress rode the ordinary job event buffer — same contract as every other kind
    stages = [e["data"]["stage"] for e in body["events"] if e["type"] == "stage"]
    assert stages == list(agent_action.AGENT_PROGRESS_STAGES)
    assert [e["type"] for e in body["events"]][-1] == "done"


def test_agent_job_action_surface_excludes_canon_writers(client: TestClient) -> None:
    # The propose-only guarantee, asserted at the REST boundary: whatever the agent was allowed to
    # execute, none of it writes canon — approving a gate can only ever produce a proposal.
    body = _run_agent_job(client, {"goal": "看看有什么问题", "llm_mode": "offline"})
    exposed = body["result"]["skills"]
    assert exposed
    registry = default_skill_registry(content_root="unused")
    assert not [
        name for name in exposed if registry.get(name).side_effect is SideEffect.WRITES_CANON
    ]


def test_agent_job_reports_unknown_skill_names(client: TestClient) -> None:
    body = _run_agent_job(
        client,
        {
            "goal": "只用审计",
            "llm_mode": "offline",
            "skills": ["audit_project", "totally_fake_skill_xyz"],
        },
    )
    result = body["result"]
    assert result["unknown_skills"] == ["totally_fake_skill_xyz"]
    assert "audit_project" in result["known_skills"]
    assert result["skills"] == ["audit_project"]  # the run's real executable surface


def test_agent_job_accepts_and_enforces_independent_tool_budget(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(
        agent_action,
        "build_agent_chat_model",
        lambda **_kw: scripted_chat_model(
            [("audit_project", {}), ("list_issues", {}), "budget handled"]
        ),
    )
    body = _run_agent_job(
        client,
        {
            "goal": "bounded audit",
            "llm_mode": "offline",
            "max_steps": 4,
            "max_tool_calls": 1,
        },
    )

    assert body["status"] == "done", body
    steps = body["result"]["steps"]
    assert steps[0]["is_error"] is False
    assert json.loads(steps[1]["observation"])["error_code"] == "BUDGET_EXCEEDED"


def test_agent_job_accepts_and_enforces_total_model_budget(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        agent_action,
        "build_agent_chat_model",
        lambda **_kw: scripted_chat_model([("audit_project", {}), "must not run"]),
    )
    body = _run_agent_job(
        client,
        {
            "goal": "one total model dispatch",
            "llm_mode": "offline",
            "max_steps": 4,
            "max_model_calls": 1,
            "max_tool_calls": 4,
        },
    )

    assert body["status"] == "done", body
    result = body["result"]
    assert result["stop_reason"] == "max_model_calls"
    assert result["run_budget"]["total_model_call_limit"] == 1
    assert result["run_budget"]["total_model_calls_reserved"] == 1


def test_agent_job_accepts_and_enforces_zero_settled_cost_gate(client: TestClient) -> None:
    body = _run_agent_job(
        client,
        {"goal": "do not spend", "llm_mode": "offline", "max_cost_usd": 0.0},
    )

    assert body["status"] == "done", body
    result = body["result"]
    assert result["stop_reason"] == "max_cost"
    assert json.loads(result["final_answer"])["error_code"] == "BUDGET_EXCEEDED"
    assert result["run_budget"]["max_cost_usd"] == 0.0
    assert result["run_budget"]["total_model_calls_reserved"] == 0
    assert result["run_budget"]["cost_gate_semantics"] == "settled_telemetry_pre_dispatch"


def test_agent_job_hitl_pauses_then_approve_runs_the_gated_tool(
    client: TestClient, monkeypatch
) -> None:
    # The canonical offline double never proposes a patch, so the gate needs a model that does:
    # the standard LangChain fake, scripted per invocation (fresh run first, then the resume).
    issue_id = _persist_one_issue_id(client)
    scripts = iter(
        [
            [(_PROPOSE_TOOL, {"issue_id": issue_id}), "x"],
            ["已按批准生成修复提案，等待人工审阅。"],
        ]
    )
    monkeypatch.setattr(
        agent_action, "build_agent_chat_model", lambda **_kw: scripted_chat_model(next(scripts))
    )

    paused = _run_agent_job(
        client, {"goal": "提一个修复方案", "llm_mode": "offline", "hitl": True}
    )["result"]
    assert paused["status"] == "interrupted"
    assert paused["interrupted"] is True
    assert paused["final_answer"] == ""  # no answer yet — it is waiting on a human
    assert paused["interrupt_request"]["action_requests"][0]["name"] == _PROPOSE_TOOL
    # the gated tool has NOT run: the pending call produced no step
    assert _PROPOSE_TOOL not in [step["action"] for step in paused["steps"]]

    resumed = _run_agent_job(
        client,
        {
            "llm_mode": "offline",
            "thread_id": paused["thread_id"],
            "resume": True,
            "decision": "approve",
        },
    )["result"]
    assert resumed["status"] == "completed"
    assert resumed["stop_reason"] == "finished"
    gated = next(step for step in resumed["steps"] if step["action"] == _PROPOSE_TOOL)
    assert gated["is_error"] is False  # the real propose_fix skill ran on a real issue
    assert gated["result"]["candidates"]


def test_agent_job_hitl_reject_skips_the_gated_tool(client: TestClient, monkeypatch) -> None:
    scripts = iter([[(_PROPOSE_TOOL, {"issue_id": "iss-x"}), "x"], ["收到，不再提议。"]])
    monkeypatch.setattr(
        agent_action, "build_agent_chat_model", lambda **_kw: scripted_chat_model(next(scripts))
    )

    paused = _run_agent_job(
        client, {"goal": "提一个修复方案", "llm_mode": "offline", "hitl": True}
    )["result"]
    assert paused["status"] == "interrupted"

    resumed = _run_agent_job(
        client,
        {
            "llm_mode": "offline",
            "thread_id": paused["thread_id"],
            "resume": True,
            "decision": "reject",
            "decision_message": "这条先不动",
        },
    )["result"]
    assert resumed["status"] == "completed"
    gated = next(step for step in resumed["steps"] if step["action"] == _PROPOSE_TOOL)
    assert gated["is_error"] is True
    assert "这条先不动" in gated["observation"]


def test_agent_job_misuse_fails_with_chinese_guidance(client: TestClient) -> None:
    blank = _run_agent_job(client, {"goal": "   ", "llm_mode": "offline"})
    assert blank["status"] == "failed"
    assert "目标" in blank["error"]

    stray = _run_agent_job(client, {"goal": "看看", "llm_mode": "offline", "decision": "approve"})
    assert stray["status"] == "failed"
    assert "续跑" in stray["error"] or "恢复" in stray["error"]

    missing_thread = _run_agent_job(
        client, {"llm_mode": "offline", "resume": True, "thread_id": "nope"}
    )
    assert missing_thread["status"] == "failed"
    assert "找不到" in missing_thread["error"]


def test_agent_job_rejects_unknown_params(client: TestClient) -> None:
    bad = client.post(
        "/projects/demo/jobs",
        json={"kind": "agent_diagnose", "params": {"goal": "x", "content_root": "/etc"}},
    )
    assert bad.status_code == 400
    # filesystem paths are not a REST-settable parameter — the project registry decides the root
    assert "content_root" in bad.json()["detail"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_steps", ""),
        ("max_model_calls", 0),
        ("max_tool_calls", 101),
        ("max_cost_usd", "NaN"),
        ("early_stopping", "wander"),
        ("tool_transport", "silent-fallback"),
    ],
)
def test_agent_job_rejects_invalid_typed_controls_before_queueing(
    client: TestClient, field: str, value: object
) -> None:
    response = client.post(
        "/projects/demo/jobs",
        json={"kind": "agent_diagnose", "params": {"goal": "x", field: value}},
    )
    assert response.status_code == 422, response.text


def test_multi_agent_job_runs_orchestrator_workers_and_verifier(client: TestClient) -> None:
    """FastAPI exposes the real attributed multi-agent flow, not a presentation-only mock."""
    body = _run_job(
        client,
        "multi_agent",
        {
            "goal": "检查任务链，并让世界达到可导出状态",
            "llm_mode": "offline",
            "max_tool_calls": 3,
            "max_total_tool_calls": 16,
            "max_model_calls": 32,
            "max_cost_usd": 0.01,
        },
    )

    assert body["status"] == "done", body
    result = body["result"]
    assert {"orchestrator", "diag_01", "repair_01", "verifier_01"} <= set(result["participants"])
    flow = result["blackboard_flow"]
    assert {row["msg_type"] for row in flow} >= {
        "task_assign",
        "task_result",
        "verify_request",
        "verify_result",
        "synthesize",
    }
    assert all(row["from_agent"] and row["to_agent"] for row in flow)
    assert result["worker_summaries"]
    assert result["verifier_verdicts"]
    assert "Multi-Agent Session Report" in result["synthesis"]
    assert result["budget_scope"]["per_agent_max_tool_calls"] == 3
    session_budget = result["budget_scope"]["session_global_model_budget"]
    assert session_budget["model_calls_limit"] == 32
    assert 1 <= session_budget["model_calls_used"] <= 32
    assert session_budget["settled_cost_limit_usd"] == 0.01
    tool_budget = result["budget_scope"]["session_global_tool_budget"]
    assert tool_budget["tool_calls_limit"] == 16
    assert 1 <= tool_budget["tool_calls_used"] <= 16
    assert result["budget_scope"]["max_cost_usd"] == "settled_cost_blocks_next_dispatch"
    assert result["cost_budget"]["used_usd"] == 0.0
    stages = [event["data"]["stage"] for event in body["events"] if event["type"] == "stage"]
    assert stages == ["accepted", "orchestrate", "complete"]


@pytest.mark.parametrize(
    "params",
    [
        {"goal": "   "},
        {"goal": "检查世界", "max_tool_calls": ""},
        {"goal": "检查世界", "max_tool_calls": 0},
        {"goal": "检查世界", "max_total_tool_calls": 0},
        {"goal": "检查世界", "max_total_tool_calls": 4097},
        {"goal": "检查世界", "max_model_calls": 0},
        {"goal": "检查世界", "max_model_calls": 513},
        {"goal": "检查世界", "max_cost_usd": -0.01},
        {"goal": "检查世界", "unexpected": True},
    ],
)
def test_multi_agent_job_rejects_invalid_typed_params_before_queue(
    client: TestClient, params: dict
) -> None:
    response = client.post("/projects/demo/jobs", json={"kind": "multi_agent", "params": params})
    assert response.status_code == 422


def _persist_one_issue_id(client: TestClient) -> str:
    """Run the real audit through REST so a genuine issue id exists for the gated proposal."""
    audit = client.post("/projects/demo/audits", json={"persist": True})
    assert audit.status_code == 200, audit.text
    issues = audit.json()["issues"]
    assert issues, "the seeded project must produce at least one audit issue"
    return str(issues[0]["id"])
