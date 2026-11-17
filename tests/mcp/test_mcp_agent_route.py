from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from owcopilot.agent import ReActAgent
from owcopilot.content.models import ContentBundle, Quest
from owcopilot.content.store import ContentStore
from owcopilot.fakes import scripted_chat_model
from owcopilot.mcp_client import (
    LocalStdioConfig,
    MCPCallError,
    MCPErrorCode,
    MCPInvocationTrace,
    MCPObservation,
    MCPPhase,
    default_local_config,
)
from owcopilot.mcp_client.agent_route import registry_with_mcp_audit
from owcopilot.skills import default_skill_registry

MCP_INSTALLED = importlib.util.find_spec("mcp") is not None
REPO_ROOT = Path(__file__).resolve().parents[2]


class _RecordingMCPClient:
    def __init__(self, outcome: MCPObservation | MCPCallError) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, Any] | None = None
    ) -> MCPObservation | MCPCallError:
        self.calls.append((tool_name, dict(arguments or {})))
        return self.outcome


def _config(content_root: Path) -> LocalStdioConfig:
    return default_local_config(
        content_root=content_root,
        sqlite_path=content_root.parent / "runtime.sqlite",
    )


def _observation() -> MCPObservation:
    return MCPObservation(
        tool_name="audit_project",
        data={
            "content_hash": "a" * 64,
            "audit_run": {"id": "remote-run"},
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


def test_route_replaces_only_audit_and_preserves_registry_governance(tmp_path: Path) -> None:
    content_root = tmp_path / "content"
    content_root.mkdir()
    native = default_skill_registry(content_root=str(content_root))
    client = _RecordingMCPClient(_observation())

    routed = registry_with_mcp_audit(native, _config(content_root), client=client)

    assert routed.names() == native.names()
    for name in native.names():
        if name != "audit_project":
            assert routed.get(name) is native.get(name)
    assert routed.get("audit_project") is not native.get("audit_project")
    assert routed.get("audit_project").semantic_projection_name == (
        native.get("audit_project").semantic_projection_name
    )
    assert routed.get("audit_project").state_marker_policy == (
        native.get("audit_project").state_marker_policy
    )
    assert [parameter.name for parameter in routed.get("audit_project").parameters] == ["limit"]


def test_react_agent_uses_mcp_handler_and_feeds_checked_result_back(tmp_path: Path) -> None:
    content_root = tmp_path / "content"
    content_root.mkdir()
    client = _RecordingMCPClient(_observation())
    traces: list[MCPInvocationTrace] = []
    routed = registry_with_mcp_audit(
        default_skill_registry(content_root=str(content_root)),
        _config(content_root),
        client=client,
        trace_sink=traces.append,
    )
    agent = ReActAgent(
        model=scripted_chat_model([("audit_project", {}), "MCP audit complete."]),
        registry=routed,
        max_steps=3,
    )

    result = agent.run("Audit through the configured transport.")

    assert result.stop_reason == "finished"
    assert client.calls == [("audit_project", {"limit": 50})]
    assert result.steps[0].result is not None
    assert result.steps[0].result["_mcp"]["transport"] == "stdio"
    assert len(traces) == 1
    assert traces[0].outcome == "ok"


@pytest.mark.skipif(not MCP_INSTALLED, reason="optional MCP SDK ([mcp]) not installed")
def test_react_agent_crosses_real_stdio_process_and_persists_audit(tmp_path: Path) -> None:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(
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
    sqlite_path = tmp_path / "runtime.sqlite"
    config = default_local_config(
        content_root=content_root,
        sqlite_path=sqlite_path,
        python_executable=sys.executable,
        source_root=REPO_ROOT / "src",
        working_directory=REPO_ROOT,
    )
    traces: list[MCPInvocationTrace] = []
    routed = registry_with_mcp_audit(
        default_skill_registry(content_root=str(content_root), sqlite_path=str(sqlite_path)),
        config,
        trace_sink=traces.append,
    )
    result = ReActAgent(
        model=scripted_chat_model([("audit_project", {}), "Cross-process audit complete."]),
        registry=routed,
        max_steps=3,
    ).run("Run the real MCP audit route.")

    assert result.stop_reason == "finished"
    assert result.steps[0].is_error is False
    assert result.steps[0].result is not None
    assert result.steps[0].result["open_errors"] >= 1
    assert result.steps[0].result["_mcp"]["server_name"] == "owcopilot-local-consumer"
    assert traces[0].outcome == "ok"
    with sqlite3.connect(sqlite_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM issues").fetchone()[0] >= 1


def test_mcp_failure_stays_on_the_routed_path_without_native_fallback(tmp_path: Path) -> None:
    content_root = tmp_path / "content"
    content_root.mkdir()
    client = _RecordingMCPClient(
        MCPCallError(
            code=MCPErrorCode.CALL_TIMEOUT,
            phase=MCPPhase.CALL,
            message="untrusted remote detail",
            retryable=False,
            details={"outcome": "unknown"},
            untrusted_input=True,
        )
    )
    routed = registry_with_mcp_audit(
        default_skill_registry(content_root=str(content_root)),
        _config(content_root),
        client=client,
    )
    agent = ReActAgent(
        model=scripted_chat_model([("audit_project", {}), "Report the typed failure."]),
        registry=routed,
        max_steps=3,
    )

    result = agent.run("Exercise the MCP failure boundary.")

    assert client.calls == [("audit_project", {"limit": 50})]
    assert result.steps[0].is_error is True
    assert "untrusted remote detail" not in result.steps[0].observation
    envelope = json.loads(result.steps[0].observation)
    assert envelope["error_code"] == "OUTCOME_UNKNOWN"
    assert envelope["retryable"] is False
    assert envelope["outcome_unknown"] is True
