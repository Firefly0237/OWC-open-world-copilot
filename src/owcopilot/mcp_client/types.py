"""Typed contracts at the local MCP consumer trust boundary.

The MCP server is a separate process, even when it is launched from this repository.  Its
advertised schema and returned content therefore remain untrusted until the consumer checks them.
These small SDK-independent types let callers branch on failures without parsing exception text or
placing raw tool output into an instruction message.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal


class MCPErrorCode(str, Enum):
    """Stable failure modes returned by :class:`LocalStdioMCPClient`."""

    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    INVALID_CONFIG = "INVALID_CONFIG"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
    CONNECTION_TIMEOUT = "CONNECTION_TIMEOUT"
    CALL_TIMEOUT = "CALL_TIMEOUT"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    PROTOCOL_DRIFT = "PROTOCOL_DRIFT"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"
    REMOTE_ERROR = "REMOTE_ERROR"
    RESULT_TOO_LARGE = "RESULT_TOO_LARGE"
    RESULT_TYPE = "RESULT_TYPE"
    RESULT_UNSAFE = "RESULT_UNSAFE"


class MCPPhase(str, Enum):
    """Protocol phase in which an error occurred."""

    CONFIGURE = "configure"
    CONNECT = "connect"
    INITIALIZE = "initialize"
    DISCOVER = "discover"
    CALL = "call"
    DECODE = "decode"


@dataclass(frozen=True)
class MCPCallError:
    """Machine-readable failure returned to an agent/tool host (never a free-text exception)."""

    code: MCPErrorCode
    phase: MCPPhase
    message: str
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)
    # Remote tool/error text is data from another process, never an instruction. Local
    # configuration errors keep the default false; protocol/server-originated errors set true.
    untrusted_input: bool = False
    risk_flags: tuple[str, ...] = ()
    ok: Literal[False] = False


@dataclass(frozen=True)
class MCPObservation:
    """A checked JSON-object observation from an MCP tool.

    ``untrusted_input`` is deliberately a literal ``True``.  Passing schema checks makes the value
    structurally usable; it does not promote a different process's strings into instructions.
    ``risk_flags`` carries best-effort prompt-injection signals while preserving legitimate game
    prose for a higher-level policy or human review.
    """

    tool_name: str
    data: Mapping[str, Any]
    protocol_version: str
    server_name: str
    schema_fingerprint: str
    content_bytes: int
    risk_flags: tuple[str, ...] = ()
    untrusted_input: Literal[True] = True
    ok: Literal[True] = True


MCPCallOutcome = MCPObservation | MCPCallError


@dataclass(frozen=True)
class ToolContract:
    """The compatible input/output surface a consumer expects from one discovered tool.

    Optional server-side inputs may be added without breaking this consumer.  Removing or changing
    a known input, changing the required set, changing the side-effect annotation, or omitting a
    required runtime output is a breaking schema drift and fails before the call is trusted.
    """

    name: str
    input_types: Mapping[str, frozenset[str]]
    required_inputs: frozenset[str]
    input_defaults: Mapping[str, Any]
    caller_arguments: frozenset[str]
    output_types: Mapping[str, frozenset[str]]
    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool


@dataclass(frozen=True)
class ResultLimits:
    """Resource and shape limits applied before a result reaches an agent."""

    max_result_bytes: int = 512_000
    max_string_chars: int = 32_000
    max_depth: int = 16
    max_nodes: int = 20_000
    max_collection_items: int = 5_000


@dataclass(frozen=True)
class LocalStdioConfig:
    """Launch and policy configuration for the fixed local owcopilot stdio server.

    The executable may be selected explicitly so a venv can host the optional MCP dependency, but
    the module, transport, server arguments, and tool surface are not caller-controlled shell text.
    ``source_root`` is only needed when running from an uninstalled source checkout.
    """

    content_root: Path
    sqlite_path: Path
    contracts: Mapping[str, ToolContract]
    python_executable: Path
    # MCP primitives are independently allowlisted; discovering a server resource/prompt never
    # grants the caller permission to consume it. The local product enables only the two bounded
    # read resources and one review prompt published by its own server.
    allowed_resource_uris: frozenset[str] = frozenset()
    allowed_prompts: frozenset[str] = frozenset()
    source_root: Path | None = None
    working_directory: Path | None = None
    expected_server_name: str = "owcopilot-local-consumer"
    accepted_protocol_versions: frozenset[str] = frozenset()
    # A fresh Windows stdio server may spend several seconds importing the optional
    # semantic/torch stack before it can answer MCP initialize.  Keep this distinct
    # from the shorter per-call budget so a healthy cold start is not misclassified
    # as a transport failure.
    connect_timeout_seconds: float = 30.0
    call_timeout_seconds: float = 15.0
    max_discovery_pages: int = 16
    result_limits: ResultLimits = field(default_factory=ResultLimits)
    stderr_path: Path | None = None
