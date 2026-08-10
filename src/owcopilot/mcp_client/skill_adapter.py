"""Bridge the checked local MCP consumer into the synchronous Agent skill surface."""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import anyio

from ..skills import CostTier, SideEffect, Skill, SkillParameter
from ..skills.errors import ErrorCode, ExternalSkillError
from .consumer import LocalStdioMCPClient
from .types import (
    LocalStdioConfig,
    MCPCallError,
    MCPCallOutcome,
    MCPErrorCode,
    MCPObservation,
    MCPPhase,
)

logger = logging.getLogger(__name__)


class MCPToolClient(Protocol):
    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, Any] | None = None
    ) -> MCPCallOutcome: ...


@dataclass(frozen=True)
class MCPInvocationTrace:
    """Content-free host trace for one MCP-backed skill call."""

    tool_name: str
    transport: str
    argument_summary: Mapping[str, Any]
    latency_ms: float
    outcome: str
    protocol_version: str | None = None
    server_name: str | None = None
    schema_fingerprint: str | None = None
    content_bytes: int | None = None
    error_code: str | None = None
    phase: str | None = None
    retryable: bool = False
    outcome_unknown: bool = False
    risk_flags: tuple[str, ...] = ()
    remote_message_sha256: str | None = None


_MCP_AGENT_ERROR_CODES: dict[MCPErrorCode, ErrorCode] = {
    MCPErrorCode.DEPENDENCY_UNAVAILABLE: ErrorCode.EXTERNAL_UNAVAILABLE,
    MCPErrorCode.INVALID_CONFIG: ErrorCode.INVALID_ARG,
    MCPErrorCode.INVALID_ARGUMENT: ErrorCode.INVALID_ARG,
    MCPErrorCode.TOOL_NOT_ALLOWED: ErrorCode.CONFLICT,
    MCPErrorCode.CONNECTION_TIMEOUT: ErrorCode.EXTERNAL_TIMEOUT,
    MCPErrorCode.CALL_TIMEOUT: ErrorCode.EXTERNAL_TIMEOUT,
    MCPErrorCode.TRANSPORT_ERROR: ErrorCode.EXTERNAL_UNAVAILABLE,
    MCPErrorCode.PROTOCOL_DRIFT: ErrorCode.EXTERNAL_PROTOCOL,
    MCPErrorCode.TOOL_NOT_FOUND: ErrorCode.NOT_FOUND,
    MCPErrorCode.SCHEMA_DRIFT: ErrorCode.EXTERNAL_PROTOCOL,
    MCPErrorCode.REMOTE_ERROR: ErrorCode.EXTERNAL_UNAVAILABLE,
    MCPErrorCode.RESULT_TOO_LARGE: ErrorCode.EXTERNAL_PROTOCOL,
    MCPErrorCode.RESULT_TYPE: ErrorCode.EXTERNAL_PROTOCOL,
    MCPErrorCode.RESULT_UNSAFE: ErrorCode.EXTERNAL_PROTOCOL,
}


class MCPInvocationError(ExternalSkillError):
    """Typed Agent-boundary failure without echoing untrusted remote text."""

    def __init__(self, error: MCPCallError) -> None:
        outcome_unknown = error.details.get("outcome") == "unknown" or (
            error.phase is MCPPhase.CALL and error.code is MCPErrorCode.TRANSPORT_ERROR
        )
        agent_error_code = (
            ErrorCode.OUTCOME_UNKNOWN
            if outcome_unknown
            else _MCP_AGENT_ERROR_CODES.get(error.code, ErrorCode.INTERNAL)
        )
        super().__init__(
            (
                f"MCP tool failed with {error.code.value} during {error.phase.value}; "
                f"retryable={str(error.retryable).lower()}; "
                f"outcome_unknown={str(outcome_unknown).lower()}"
            ),
            error_code=agent_error_code,
            retryable=error.retryable,
            outcome_unknown=outcome_unknown,
        )
        self.code = error.code
        self.phase = error.phase
        self.risk_flags = error.risk_flags
        self.remote_message_sha256 = hashlib.sha256(error.message.encode("utf-8")).hexdigest()


def build_mcp_audit_skill(
    config: LocalStdioConfig,
    *,
    client: MCPToolClient | None = None,
    trace_sink: Callable[[MCPInvocationTrace], None] | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> Skill:
    """Return an ``audit_project`` Skill whose handler crosses the real MCP stdio boundary.

    ``content_root``, ``sqlite_path`` and ``persist=True`` remain host-owned in ``config`` and the
    consumer. The model can only choose a bounded result page size. The agent-side ``READ_ONLY``
    label means "does not write canon"; MCP's separate risk contract correctly marks this runtime-
    DB-writing tool non-read-only, destructive and non-idempotent.
    """
    tool_client = client or LocalStdioMCPClient(config)

    def emit(trace: MCPInvocationTrace) -> None:
        if trace_sink is None:
            return
        try:
            trace_sink(trace)
        except Exception:
            logger.warning("MCP trace sink failed", exc_info=True)

    def audit_project(*, limit: int = 50) -> dict[str, Any]:
        started = clock()
        outcome = anyio.run(tool_client.call_tool, "audit_project", {"limit": limit})
        latency_ms = max(0.0, (clock() - started) * 1000.0)
        argument_summary = {"limit": limit}
        if isinstance(outcome, MCPCallError):
            failure = MCPInvocationError(outcome)
            emit(
                MCPInvocationTrace(
                    tool_name="audit_project",
                    transport="stdio",
                    argument_summary=argument_summary,
                    latency_ms=latency_ms,
                    outcome="error",
                    error_code=outcome.code.value,
                    phase=outcome.phase.value,
                    retryable=failure.retryable,
                    outcome_unknown=failure.outcome_unknown,
                    risk_flags=outcome.risk_flags,
                    remote_message_sha256=failure.remote_message_sha256,
                )
            )
            raise failure

        assert isinstance(outcome, MCPObservation)
        emit(
            MCPInvocationTrace(
                tool_name=outcome.tool_name,
                transport="stdio",
                argument_summary=argument_summary,
                latency_ms=latency_ms,
                outcome="ok",
                protocol_version=outcome.protocol_version,
                server_name=outcome.server_name,
                schema_fingerprint=outcome.schema_fingerprint,
                content_bytes=outcome.content_bytes,
                risk_flags=outcome.risk_flags,
            )
        )
        return {
            **dict(outcome.data),
            "_mcp": {
                "transport": "stdio",
                "protocol_version": outcome.protocol_version,
                "server_name": outcome.server_name,
                "schema_fingerprint": outcome.schema_fingerprint,
                "content_bytes": outcome.content_bytes,
                "risk_flags": list(outcome.risk_flags),
                "untrusted_input": outcome.untrusted_input,
                "latency_ms": latency_ms,
            },
        }

    return Skill(
        name="audit_project",
        description=(
            "Run the deterministic consistency audit through an allowlisted local MCP stdio "
            "server. Persists runtime audit rows but never writes canon."
        ),
        cost_tier=CostTier.DETERMINISTIC,
        side_effect=SideEffect.READ_ONLY,
        handler=audit_project,
        parameters=(
            SkillParameter(
                "limit",
                "integer",
                "Maximum returned issue rows (1-100; default 50).",
                default=50,
            ),
        ),
    )


__all__ = [
    "MCPInvocationError",
    "MCPInvocationTrace",
    "MCPToolClient",
    "build_mcp_audit_skill",
]
