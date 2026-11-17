"""Real local MCP consumer with an allowlisted, typed trust boundary."""

from .agent_route import registry_with_mcp_audit
from .consumer import (
    AUDIT_PROJECT_CONTRACT,
    CANON_RESOURCE_URI,
    CONSISTENCY_REVIEW_PROMPT,
    OPEN_ISSUES_RESOURCE_URI,
    LocalStdioMCPClient,
    decode_tool_result,
    default_local_config,
)
from .skill_adapter import (
    MCPInvocationError,
    MCPInvocationTrace,
    MCPToolClient,
    build_mcp_audit_skill,
)
from .types import (
    LocalStdioConfig,
    MCPCallError,
    MCPCallOutcome,
    MCPErrorCode,
    MCPObservation,
    MCPPhase,
    ResultLimits,
    ToolContract,
)

__all__ = [
    "AUDIT_PROJECT_CONTRACT",
    "CANON_RESOURCE_URI",
    "CONSISTENCY_REVIEW_PROMPT",
    "LocalStdioConfig",
    "LocalStdioMCPClient",
    "MCPCallError",
    "MCPCallOutcome",
    "MCPErrorCode",
    "MCPObservation",
    "MCPPhase",
    "OPEN_ISSUES_RESOURCE_URI",
    "MCPInvocationError",
    "MCPInvocationTrace",
    "MCPToolClient",
    "ResultLimits",
    "ToolContract",
    "decode_tool_result",
    "default_local_config",
    "build_mcp_audit_skill",
    "registry_with_mcp_audit",
]
