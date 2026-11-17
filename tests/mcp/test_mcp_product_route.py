from __future__ import annotations

import importlib.util
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

import owcopilot.app.actions as actions_package
from owcopilot.app.actions.agent import run_agent_diagnose_action
from owcopilot.cli.main import main
from owcopilot.content.models import ContentBundle, Quest
from owcopilot.content.store import ContentStore
from owcopilot.fakes import scripted_chat_model
from owcopilot.mcp_client import (
    MCPCallError,
    MCPErrorCode,
    MCPObservation,
    MCPPhase,
)


class _RecordingMCPClient:
    def __init__(self, outcome: MCPObservation | MCPCallError) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, Any] | None = None
    ) -> MCPObservation | MCPCallError:
        self.calls.append((tool_name, dict(arguments or {})))
        return self.outcome


def _seed_project(root: Path) -> None:
    ContentStore(root).save(
        ContentBundle(
            quests={
                "q_missing": Quest(
                    id="q_missing",
                    title="Missing giver",
                    giver_npc="npc_absent",
                )
            }
        )
    )


def _mcp_observation() -> MCPObservation:
    return MCPObservation(
        tool_name="audit_project",
        data={
            "content_hash": "a" * 64,
            "audit_run": {"id": "mcp-run"},
            "issues": [],
            "open_errors": 0,
            "cost_budget": {},
            "total": 0,
            "truncation_notice": None,
        },
        protocol_version="2025-11-25",
        server_name="owcopilot-local-consumer",
        schema_fingerprint="b" * 64,
        content_bytes=256,
    )


def _run_action(
    content_root: Path,
    *,
    tool_transport: Any = "native",
    mcp_client: _RecordingMCPClient | None = None,
) -> dict[str, Any]:
    return run_agent_diagnose_action(
        content_root,
        goal="Audit the project through the selected transport.",
        max_steps=3,
        skills=["audit_project"],
        recall=False,
        checkpoint_path=str(content_root.parent / f"{tool_transport}-checkpoints.sqlite"),
        sqlite_path=str(content_root.parent / "runtime.sqlite"),
        llm_mode="offline",
        chat_model=scripted_chat_model([("audit_project", {}), "Audit complete."]),
        tool_transport=cast(Any, tool_transport),
        mcp_client=mcp_client,
    )


def test_action_defaults_to_native_and_emits_an_explicit_empty_transport_trace(
    tmp_path: Path,
) -> None:
    content_root = tmp_path / "content"
    _seed_project(content_root)

    payload = _run_action(content_root)

    assert payload["status"] == "completed"
    assert payload["tool_transport"] == "native"
    assert payload["mcp_traces"] == []


def test_action_mcp_route_uses_injected_consumer_and_emits_json_safe_trace(
    tmp_path: Path,
) -> None:
    content_root = tmp_path / "content"
    _seed_project(content_root)
    client = _RecordingMCPClient(_mcp_observation())

    payload = _run_action(content_root, tool_transport="mcp", mcp_client=client)

    assert payload["status"] == "completed"
    assert payload["tool_transport"] == "mcp"
    assert client.calls == [("audit_project", {"limit": 50})]
    assert payload["steps"][0]["result"]["_mcp"]["transport"] == "stdio"
    assert payload["mcp_traces"] == [
        {
            "tool_name": "audit_project",
            "transport": "stdio",
            "argument_summary": {"limit": 50},
            "latency_ms": payload["mcp_traces"][0]["latency_ms"],
            "outcome": "ok",
            "protocol_version": "2025-11-25",
            "server_name": "owcopilot-local-consumer",
            "schema_fingerprint": "b" * 64,
            "content_bytes": 256,
            "error_code": None,
            "phase": None,
            "retryable": False,
            "outcome_unknown": False,
            "risk_flags": [],
            "remote_message_sha256": None,
        }
    ]
    json.dumps(payload, ensure_ascii=False)


@pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="optional MCP SDK ([mcp]) not installed",
)
def test_action_mcp_route_crosses_the_real_local_stdio_boundary(tmp_path: Path) -> None:
    content_root = tmp_path / "content"
    _seed_project(content_root)

    payload = _run_action(content_root, tool_transport="mcp")

    assert payload["status"] == "completed"
    assert payload["steps"][0]["is_error"] is False
    assert payload["steps"][0]["result"]["open_errors"] >= 1
    assert payload["mcp_traces"][0]["outcome"] == "ok"
    assert payload["mcp_traces"][0]["server_name"] == "owcopilot-local-consumer"


def test_action_mcp_failure_is_traced_without_a_native_audit_fallback(tmp_path: Path) -> None:
    content_root = tmp_path / "content"
    _seed_project(content_root)
    client = _RecordingMCPClient(
        MCPCallError(
            code=MCPErrorCode.CALL_TIMEOUT,
            phase=MCPPhase.CALL,
            message="untrusted remote timeout detail",
            retryable=False,
            details={"outcome": "unknown"},
            untrusted_input=True,
        )
    )

    payload = _run_action(content_root, tool_transport="mcp", mcp_client=client)

    assert client.calls == [("audit_project", {"limit": 50})]
    assert payload["steps"][0]["is_error"] is True
    assert "untrusted remote timeout detail" not in payload["steps"][0]["observation"]
    assert payload["mcp_traces"][0]["outcome"] == "error"
    assert payload["mcp_traces"][0]["error_code"] == "CALL_TIMEOUT"
    assert payload["mcp_traces"][0]["outcome_unknown"] is True
    with sqlite3.connect(tmp_path / "runtime.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0] == 0


def test_action_rejects_unknown_tool_transport_before_execution(tmp_path: Path) -> None:
    content_root = tmp_path / "content"
    _seed_project(content_root)

    with pytest.raises(ValueError, match="tool_transport.*native.*mcp"):
        _run_action(content_root, tool_transport="http")


def _start_interrupted_mcp_run(content_root: Path, checkpoint_path: Path) -> dict[str, Any]:
    return run_agent_diagnose_action(
        content_root,
        goal="Propose a reviewed fix.",
        max_steps=3,
        skills=["propose_fix"],
        recall=False,
        hitl=True,
        checkpoint_path=str(checkpoint_path),
        sqlite_path=str(content_root.parent / "runtime.sqlite"),
        llm_mode="offline",
        chat_model=scripted_chat_model([("propose_fix", {"issue_id": "issue-for-contract"})]),
        tool_transport="mcp",
        mcp_client=_RecordingMCPClient(_mcp_observation()),
    )


def test_resume_omitted_transport_restores_mcp_and_explicit_change_is_rejected(
    tmp_path: Path,
) -> None:
    content_root = tmp_path / "content"
    _seed_project(content_root)

    restore_checkpoint = tmp_path / "restore.sqlite"
    paused = _start_interrupted_mcp_run(content_root, restore_checkpoint)
    assert paused["status"] == "interrupted"
    assert paused["tool_transport"] == "mcp"
    resumed = run_agent_diagnose_action(
        content_root,
        goal="",
        skills=["propose_fix"],
        recall=False,
        resume=True,
        thread_id=paused["thread_id"],
        decision="reject",
        checkpoint_path=str(restore_checkpoint),
        sqlite_path=str(tmp_path / "runtime.sqlite"),
        llm_mode="offline",
        chat_model=scripted_chat_model(["Stopped after human rejection."]),
        tool_transport=None,
    )
    assert resumed["status"] == "completed"
    assert resumed["tool_transport"] == "mcp"

    mismatch_checkpoint = tmp_path / "mismatch.sqlite"
    mismatch_paused = _start_interrupted_mcp_run(content_root, mismatch_checkpoint)
    with pytest.raises(ValueError, match="RUN_CONTRACT_MISMATCH.*tool_transport"):
        run_agent_diagnose_action(
            content_root,
            goal="",
            skills=["propose_fix"],
            recall=False,
            resume=True,
            thread_id=mismatch_paused["thread_id"],
            decision="reject",
            checkpoint_path=str(mismatch_checkpoint),
            sqlite_path=str(tmp_path / "runtime.sqlite"),
            llm_mode="offline",
            chat_model=scripted_chat_model(["must not run"]),
            tool_transport="native",
        )


def test_cli_passes_explicit_tool_transport_and_parser_rejects_unknown_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    content_root = tmp_path / "content"
    content_root.mkdir()
    captured: dict[str, Any] = {}

    def fake_action(_root: str | Path, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "status": "completed",
            "tool_transport": kwargs["tool_transport"],
            "mcp_traces": [],
        }

    monkeypatch.setattr(actions_package, "run_agent_diagnose_action", fake_action)
    code = main(
        [
            "agent",
            "--content-root",
            str(content_root),
            "--goal",
            "audit",
            "--llm-mode",
            "offline",
            "--tool-transport",
            "mcp",
        ]
    )

    assert code == 0
    assert captured["tool_transport"] == "mcp"
    assert json.loads(capsys.readouterr().out)["tool_transport"] == "mcp"

    with pytest.raises(SystemExit):
        main(
            [
                "agent",
                "--content-root",
                str(content_root),
                "--goal",
                "audit",
                "--tool-transport",
                "http",
            ]
        )


def test_rest_request_schema_accepts_declared_routes_and_rejects_unknown_route() -> None:
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from owcopilot.service.schemas import JobCreateRequest

    request = JobCreateRequest.model_validate(
        {
            "kind": "agent_diagnose",
            "params": {"goal": "audit", "tool_transport": "mcp"},
        }
    )
    assert request.params["tool_transport"] == "mcp"

    with pytest.raises(ValidationError) as exc_info:
        JobCreateRequest.model_validate(
            {
                "kind": "agent_diagnose",
                "params": {"goal": "audit", "tool_transport": "http"},
            }
        )
    error_message = str(exc_info.value)
    assert "tool_transport" in error_message
    assert "native" in error_message
    assert "mcp" in error_message
