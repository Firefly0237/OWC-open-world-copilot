"""Allowlisted consumer for the real local owcopilot MCP stdio server.

One call follows the real wire path, not a handler shortcut::

    spawn ``python -m owcopilot.mcp_server``
      -> MCP initialize
      -> paginated tool discovery
      -> protocol/schema/annotation compatibility checks
      -> tool call
      -> bounded, typed, explicitly-untrusted observation

The one-shot lifecycle is intentional for the minimal host integration: ownership and shutdown are
unambiguous, and a timed-out process cannot leak into the next job.  A long-lived pool can reuse the
same validators later; it is not required to prove the consumer protocol or game-audit scenario.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import unicodedata
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any, NoReturn, TextIO

import anyio

from ..content.injection import scan_for_injection
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

AUDIT_PROJECT_CONTRACT = ToolContract(
    name="audit_project",
    input_types={
        "content_root": frozenset({"string"}),
        "sqlite_path": frozenset({"string", "null"}),
        "persist": frozenset({"boolean"}),
        "limit": frozenset({"integer", "null"}),
    },
    required_inputs=frozenset({"content_root"}),
    input_defaults={"persist": True, "sqlite_path": None, "limit": None},
    # Persistence is a host policy, not a model choice. A separate preview tool can expose the
    # read-only/non-persisting contract later without changing this tool's retry/approval semantics.
    caller_arguments=frozenset({"limit"}),
    output_types={
        "content_hash": frozenset({"string"}),
        "audit_run": frozenset({"object"}),
        "issues": frozenset({"array"}),
        "open_errors": frozenset({"integer"}),
        "cost_budget": frozenset({"object"}),
        "total": frozenset({"integer"}),
        "truncation_notice": frozenset({"string", "null"}),
    },
    # audit_project persists audit/issue rows in the runtime DB; treating it as read-only would be
    # a real approval/idempotency bug even though it never writes canon.
    read_only=False,
    # MCP defines destructiveHint=False as *only additive*. A persisted audit can replace issue
    # rows and mark formerly-open issues fixed, so the conservative protocol truth is destructive.
    destructive=True,
    idempotent=False,
    open_world=False,
)

CANON_RESOURCE_URI = "owcopilot://canon"
OPEN_ISSUES_RESOURCE_URI = "owcopilot://issues/open"
CONSISTENCY_REVIEW_PROMPT = "consistency_review"


class _BoundaryFailure(Exception):
    def __init__(self, error: MCPCallError) -> None:
        super().__init__(error.message)
        self.error = error


class LocalStdioMCPClient:
    """Execute allowlisted tools through a fixed local stdio subprocess.

    The class deliberately exposes no arbitrary command/args field.  Trusted application
    configuration selects a Python executable; every untrusted task is confined to the contracts'
    tool names and caller arguments, while ``content_root`` and ``sqlite_path`` come from the host.
    """

    def __init__(self, config: LocalStdioConfig) -> None:
        self.config = config

    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, Any] | None = None
    ) -> MCPCallOutcome:
        """Initialize, discover, validate and call one allowlisted tool over real stdio."""
        config_error = _validate_config(self.config)
        if config_error is not None:
            return config_error

        if not isinstance(tool_name, str) or not tool_name:
            return MCPCallError(
                code=MCPErrorCode.INVALID_ARGUMENT,
                phase=MCPPhase.CONFIGURE,
                message="tool_name must be a non-empty string",
            )
        if arguments is not None and not isinstance(arguments, Mapping):
            return MCPCallError(
                code=MCPErrorCode.INVALID_ARGUMENT,
                phase=MCPPhase.CONFIGURE,
                message="tool arguments must be a mapping",
            )

        contract = self.config.contracts.get(tool_name)
        if contract is None:
            return MCPCallError(
                code=MCPErrorCode.TOOL_NOT_ALLOWED,
                phase=MCPPhase.CONFIGURE,
                message=f"tool {tool_name!r} is outside the configured MCP allowlist",
            )
        prepared = _prepare_arguments(self.config, contract, arguments or {})
        if isinstance(prepared, MCPCallError):
            return prepared

        sdk = _load_sdk()
        if isinstance(sdk, MCPCallError):
            return sdk
        ClientSession, StdioServerParameters, stdio_client, McpError = sdk

        params = StdioServerParameters(
            command=str(self.config.python_executable),
            args=[
                "-m",
                "owcopilot.mcp_server",
                "--content-root",
                str(self.config.content_root.resolve()),
                "--sqlite-path",
                str(self.config.sqlite_path.resolve()),
                "--transport",
                "stdio",
                "--name",
                self.config.expected_server_name,
            ],
            env=_subprocess_env(self.config),
            cwd=(
                self.config.working_directory.resolve() if self.config.working_directory else None
            ),
        )

        phase = MCPPhase.CONNECT
        try:
            with _stderr_file(self.config.stderr_path) as child_stderr:
                # The transport context owns an anyio cancel scope.  It must be entered and exited
                # while this outer fail_after is still the current scope (moving it through an
                # AsyncExitStack would violate anyio's LIFO cancel-scope invariant on Windows).
                with anyio.fail_after(self.config.connect_timeout_seconds) as connect_scope:
                    async with stdio_client(params, errlog=child_stderr) as streams:
                        read_stream, write_stream = streams
                        async with ClientSession(
                            read_stream,
                            write_stream,
                            read_timeout_seconds=timedelta(
                                seconds=self.config.connect_timeout_seconds
                            ),
                        ) as session:
                            phase = MCPPhase.INITIALIZE
                            initialized = await session.initialize()
                            _validate_initialize(initialized, self.config)

                            phase = MCPPhase.DISCOVER
                            tools = await _discover_tools(session, self.config.max_discovery_pages)
                            discovered = tools.get(tool_name)
                            if discovered is None:
                                raise _BoundaryFailure(
                                    MCPCallError(
                                        code=MCPErrorCode.TOOL_NOT_FOUND,
                                        phase=MCPPhase.DISCOVER,
                                        message=(
                                            f"allowlisted tool {tool_name!r} was not advertised"
                                        ),
                                    )
                                )
                            schema_fingerprint = _validate_tool_contract(discovered, contract)

                            # Handshake/discovery is complete. Disable only the outer deadline
                            # while preserving its lexical scope, then apply the call deadline.
                            connect_scope.deadline = math.inf
                            phase = MCPPhase.CALL
                            with anyio.fail_after(self.config.call_timeout_seconds):
                                result = await session.call_tool(
                                    tool_name,
                                    prepared,
                                    read_timeout_seconds=timedelta(
                                        seconds=self.config.call_timeout_seconds
                                    ),
                                )
                            return decode_tool_result(
                                result,
                                contract=contract,
                                protocol_version=str(initialized.protocolVersion),
                                server_name=str(initialized.serverInfo.name),
                                schema_fingerprint=schema_fingerprint,
                                limits=self.config.result_limits,
                            )
        except _BoundaryFailure as exc:
            return exc.error
        except BaseExceptionGroup as group:
            # ``stdio_client`` owns an anyio task group.  An exception raised by our protocol
            # checks inside that context can therefore emerge at teardown as an ExceptionGroup;
            # recover the deliberate boundary failure/timeout instead of erasing it behind a
            # generic "task group" transport error.
            boundary = _find_group_exception(group, _BoundaryFailure)
            if isinstance(boundary, _BoundaryFailure):
                return boundary.error
            timeout = _find_group_exception(group, TimeoutError)
            if isinstance(timeout, TimeoutError):
                return _timeout_error(self.config, phase, contract)
            protocol_error = _find_group_exception(group, McpError)
            if protocol_error is not None:
                return _mcp_protocol_error(protocol_error, phase)
            unsupported = _find_group_exception(group, RuntimeError)
            if isinstance(unsupported, RuntimeError) and "Unsupported protocol version" in str(
                unsupported
            ):
                return MCPCallError(
                    code=MCPErrorCode.PROTOCOL_DRIFT,
                    phase=MCPPhase.INITIALIZE,
                    message=_safe_message(unsupported),
                )
            cancelled = _find_group_exception(group, anyio.get_cancelled_exc_class())
            if cancelled is not None:
                raise
            return MCPCallError(
                code=MCPErrorCode.TRANSPORT_ERROR,
                phase=phase,
                message=_safe_exception_group(group),
                retryable=_transport_retryable(phase, contract),
            )
        except TimeoutError:
            return _timeout_error(self.config, phase, contract)
        except McpError as exc:
            return _mcp_protocol_error(exc, phase)
        except RuntimeError as exc:
            message = _safe_message(exc)
            if phase is MCPPhase.INITIALIZE and "Unsupported protocol version" in message:
                return MCPCallError(
                    code=MCPErrorCode.PROTOCOL_DRIFT,
                    phase=phase,
                    message=message,
                )
            return MCPCallError(
                code=MCPErrorCode.TRANSPORT_ERROR,
                phase=phase,
                message=message,
                retryable=_transport_retryable(phase, contract),
            )
        except (OSError, EOFError) as exc:
            return MCPCallError(
                code=MCPErrorCode.TRANSPORT_ERROR,
                phase=phase,
                message=_safe_message(exc),
                retryable=_transport_retryable(phase, contract),
            )
        except Exception as exc:  # SDK exceptions intentionally cannot escape as untyped failures.
            return MCPCallError(
                code=MCPErrorCode.TRANSPORT_ERROR,
                phase=phase,
                message=_safe_message(exc),
                retryable=_transport_retryable(phase, contract),
            )

    async def read_resource(self, uri: str) -> MCPCallOutcome:
        """Read one explicitly allowlisted JSON resource over the real MCP primitive."""
        if not isinstance(uri, str) or not uri:
            return MCPCallError(
                code=MCPErrorCode.INVALID_ARGUMENT,
                phase=MCPPhase.CONFIGURE,
                message="resource URI must be a non-empty string",
            )
        if uri not in self.config.allowed_resource_uris:
            return MCPCallError(
                code=MCPErrorCode.TOOL_NOT_ALLOWED,
                phase=MCPPhase.CONFIGURE,
                message=f"resource {uri!r} is outside the configured MCP allowlist",
            )
        return await self._call_primitive(kind="resource", name=uri, arguments={})

    async def get_prompt(
        self, name: str, arguments: Mapping[str, str] | None = None
    ) -> MCPCallOutcome:
        """Render one allowlisted prompt; returned text remains explicitly untrusted data."""
        if not isinstance(name, str) or not name:
            return MCPCallError(
                code=MCPErrorCode.INVALID_ARGUMENT,
                phase=MCPPhase.CONFIGURE,
                message="prompt name must be a non-empty string",
            )
        if name not in self.config.allowed_prompts:
            return MCPCallError(
                code=MCPErrorCode.TOOL_NOT_ALLOWED,
                phase=MCPPhase.CONFIGURE,
                message=f"prompt {name!r} is outside the configured MCP allowlist",
            )
        supplied = dict(arguments or {})
        if set(supplied) - {"focus"}:
            return MCPCallError(
                code=MCPErrorCode.INVALID_ARGUMENT,
                phase=MCPPhase.CONFIGURE,
                message="consistency_review accepts only the optional 'focus' argument",
            )
        if any(not isinstance(value, str) for value in supplied.values()):
            return MCPCallError(
                code=MCPErrorCode.INVALID_ARGUMENT,
                phase=MCPPhase.CONFIGURE,
                message="prompt arguments must be strings",
            )
        if len(supplied.get("focus", "")) > 400:
            return MCPCallError(
                code=MCPErrorCode.INVALID_ARGUMENT,
                phase=MCPPhase.CONFIGURE,
                message="prompt focus must contain at most 400 characters",
            )
        return await self._call_primitive(kind="prompt", name=name, arguments=supplied)

    async def _call_primitive(
        self,
        *,
        kind: str,
        name: str,
        arguments: Mapping[str, str],
    ) -> MCPCallOutcome:
        """Shared subprocess boundary for MCP Resources and Prompts."""
        config_error = _validate_config(self.config)
        if config_error is not None:
            return config_error
        sdk = _load_sdk()
        if isinstance(sdk, MCPCallError):
            return sdk
        ClientSession, StdioServerParameters, stdio_client, McpError = sdk
        params = StdioServerParameters(
            command=str(self.config.python_executable),
            args=[
                "-m",
                "owcopilot.mcp_server",
                "--content-root",
                str(self.config.content_root.resolve()),
                "--sqlite-path",
                str(self.config.sqlite_path.resolve()),
                "--transport",
                "stdio",
                "--name",
                self.config.expected_server_name,
            ],
            env=_subprocess_env(self.config),
            cwd=(
                self.config.working_directory.resolve() if self.config.working_directory else None
            ),
        )
        phase = MCPPhase.CONNECT
        try:
            with _stderr_file(self.config.stderr_path) as child_stderr:
                with anyio.fail_after(self.config.connect_timeout_seconds) as connect_scope:
                    async with stdio_client(params, errlog=child_stderr) as streams:
                        read_stream, write_stream = streams
                        async with ClientSession(
                            read_stream,
                            write_stream,
                            read_timeout_seconds=timedelta(
                                seconds=self.config.connect_timeout_seconds
                            ),
                        ) as session:
                            phase = MCPPhase.INITIALIZE
                            initialized = await session.initialize()
                            _validate_initialize(initialized, self.config)
                            phase = MCPPhase.DISCOVER
                            discovered = await _discover_primitives(
                                session, kind=kind, max_pages=self.config.max_discovery_pages
                            )
                            advertised = discovered.get(name)
                            if advertised is None:
                                raise _BoundaryFailure(
                                    MCPCallError(
                                        code=MCPErrorCode.TOOL_NOT_FOUND,
                                        phase=MCPPhase.DISCOVER,
                                        message=f"allowlisted {kind} {name!r} was not advertised",
                                    )
                                )
                            fingerprint = _validate_primitive_contract(
                                kind=kind, name=name, item=advertised
                            )
                            connect_scope.deadline = math.inf
                            phase = MCPPhase.CALL
                            with anyio.fail_after(self.config.call_timeout_seconds):
                                if kind == "resource":
                                    result = await session.read_resource(name)
                                else:
                                    result = await session.get_prompt(name, dict(arguments))
                            return _decode_primitive_result(
                                result,
                                kind=kind,
                                name=name,
                                protocol_version=str(initialized.protocolVersion),
                                server_name=str(initialized.serverInfo.name),
                                schema_fingerprint=fingerprint,
                                limits=self.config.result_limits,
                            )
        except _BoundaryFailure as exc:
            return exc.error
        except BaseExceptionGroup as group:
            boundary = _find_group_exception(group, _BoundaryFailure)
            if isinstance(boundary, _BoundaryFailure):
                return boundary.error
            timeout = _find_group_exception(group, TimeoutError)
            if isinstance(timeout, TimeoutError):
                return _primitive_timeout_error(self.config, phase, kind)
            protocol_error = _find_group_exception(group, McpError)
            if protocol_error is not None:
                return _mcp_protocol_error(protocol_error, phase)
            cancelled = _find_group_exception(group, anyio.get_cancelled_exc_class())
            if cancelled is not None:
                raise
            return MCPCallError(
                code=MCPErrorCode.TRANSPORT_ERROR,
                phase=phase,
                message=_safe_exception_group(group),
                retryable=True,
            )
        except TimeoutError:
            return _primitive_timeout_error(self.config, phase, kind)
        except McpError as exc:
            return _mcp_protocol_error(exc, phase)
        except (OSError, EOFError, RuntimeError) as exc:
            return MCPCallError(
                code=MCPErrorCode.TRANSPORT_ERROR,
                phase=phase,
                message=_safe_message(exc),
                retryable=True,
            )
        except Exception as exc:
            return MCPCallError(
                code=MCPErrorCode.TRANSPORT_ERROR,
                phase=phase,
                message=_safe_message(exc),
                retryable=True,
            )


def default_local_config(
    *,
    content_root: str | Path,
    sqlite_path: str | Path,
    source_root: str | Path | None = None,
    working_directory: str | Path | None = None,
    python_executable: str | Path | None = None,
) -> LocalStdioConfig:
    """Build the minimal audit-only local consumer configuration."""
    return LocalStdioConfig(
        content_root=Path(content_root),
        sqlite_path=Path(sqlite_path),
        contracts={AUDIT_PROJECT_CONTRACT.name: AUDIT_PROJECT_CONTRACT},
        python_executable=Path(python_executable or sys.executable),
        allowed_resource_uris=frozenset({CANON_RESOURCE_URI, OPEN_ISSUES_RESOURCE_URI}),
        allowed_prompts=frozenset({CONSISTENCY_REVIEW_PROMPT}),
        source_root=Path(source_root) if source_root is not None else None,
        working_directory=Path(working_directory) if working_directory is not None else None,
        accepted_protocol_versions=_sdk_protocol_versions(),
    )


def decode_tool_result(
    result: Any,
    *,
    contract: ToolContract,
    protocol_version: str,
    server_name: str,
    schema_fingerprint: str,
    limits: ResultLimits,
) -> MCPCallOutcome:
    """Validate an SDK result and turn it into typed data or a typed boundary error."""
    content = getattr(result, "content", None)
    if not isinstance(content, list) or len(content) != 1:
        return _decode_error(
            MCPErrorCode.RESULT_TYPE,
            "tool result must contain exactly one text content block",
        )
    block = content[0]
    if getattr(block, "type", None) != "text" or not isinstance(getattr(block, "text", None), str):
        return _decode_error(
            MCPErrorCode.RESULT_TYPE,
            "tool result content must be JSON text; image/audio/resource blocks are not accepted",
        )
    raw_text: str = block.text
    structured = getattr(result, "structuredContent", None)
    try:
        structured_json = _canonical_json(structured) if structured is not None else ""
        structured_bytes = len(structured_json.encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        return _decode_error(
            MCPErrorCode.RESULT_TYPE,
            "structured tool content is not a JSON value",
        )
    content_bytes = len(raw_text.encode("utf-8")) + structured_bytes
    if content_bytes > limits.max_result_bytes:
        return MCPCallError(
            code=MCPErrorCode.RESULT_TOO_LARGE,
            phase=MCPPhase.DECODE,
            message=(
                f"tool result is {content_bytes} bytes; limit is {limits.max_result_bytes} bytes"
            ),
            details={"content_bytes": content_bytes, "limit_bytes": limits.max_result_bytes},
        )

    if bool(getattr(result, "isError", False)):
        return MCPCallError(
            code=MCPErrorCode.REMOTE_ERROR,
            phase=MCPPhase.CALL,
            message=_safe_text(raw_text),
            retryable=False,
            untrusted_input=True,
            risk_flags=("prompt_injection_pattern",) if scan_for_injection(raw_text) else (),
        )

    try:
        payload = json.loads(
            raw_text,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: _reject_json_constant(value),
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        return _decode_error(MCPErrorCode.RESULT_TYPE, f"tool result is not strict JSON: {exc}")
    if not isinstance(payload, dict):
        return _decode_error(MCPErrorCode.RESULT_TYPE, "tool result JSON must be an object")
    if structured is None or not isinstance(structured, dict):
        return _decode_error(
            MCPErrorCode.RESULT_TYPE,
            "tool result must include structuredContent as a JSON object",
        )
    try:
        payload_json = _canonical_json(payload)
    except (TypeError, ValueError, RecursionError) as exc:
        return _decode_error(
            MCPErrorCode.RESULT_TYPE,
            f"tool result cannot be canonicalized: {exc}",
        )
    if payload_json != structured_json:
        return _decode_error(
            MCPErrorCode.RESULT_TYPE,
            "text content and structuredContent disagree",
        )

    unsafe = _validate_json_tree(payload, limits)
    if unsafe is not None:
        return _decode_error(MCPErrorCode.RESULT_UNSAFE, unsafe)
    output_error = _validate_output_contract(payload, contract)
    if output_error is not None:
        return _decode_error(MCPErrorCode.SCHEMA_DRIFT, output_error)
    if contract.name == "audit_project":
        audit_error = _validate_audit_payload(payload)
        if audit_error is not None:
            return _decode_error(MCPErrorCode.SCHEMA_DRIFT, audit_error)

    risk_flags = _prompt_injection_flags(payload)
    return MCPObservation(
        tool_name=contract.name,
        data=payload,
        protocol_version=protocol_version,
        server_name=server_name,
        schema_fingerprint=schema_fingerprint,
        content_bytes=content_bytes,
        risk_flags=risk_flags,
    )


def _load_sdk() -> tuple[Any, Any, Any, Any] | MCPCallError:
    try:
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp.shared.exceptions import McpError

        from mcp import ClientSession
    except ImportError:
        return MCPCallError(
            code=MCPErrorCode.DEPENDENCY_UNAVAILABLE,
            phase=MCPPhase.CONFIGURE,
            message="MCP client support requires the optional dependency: pip install -e .[mcp]",
        )
    return ClientSession, StdioServerParameters, stdio_client, McpError


def _sdk_protocol_versions() -> frozenset[str]:
    try:
        from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
    except ImportError:
        return frozenset()
    return frozenset(str(value) for value in SUPPORTED_PROTOCOL_VERSIONS)


def _validate_config(config: LocalStdioConfig) -> MCPCallError | None:
    numeric = {
        "connect_timeout_seconds": config.connect_timeout_seconds,
        "call_timeout_seconds": config.call_timeout_seconds,
        "max_result_bytes": config.result_limits.max_result_bytes,
        "max_string_chars": config.result_limits.max_string_chars,
        "max_depth": config.result_limits.max_depth,
        "max_nodes": config.result_limits.max_nodes,
        "max_collection_items": config.result_limits.max_collection_items,
        "max_discovery_pages": config.max_discovery_pages,
    }
    invalid = [name for name, value in numeric.items() if value <= 0]
    if invalid:
        return MCPCallError(
            code=MCPErrorCode.INVALID_CONFIG,
            phase=MCPPhase.CONFIGURE,
            message="configuration values must be positive: " + ", ".join(sorted(invalid)),
        )
    if not config.content_root.is_dir():
        return MCPCallError(
            code=MCPErrorCode.INVALID_CONFIG,
            phase=MCPPhase.CONFIGURE,
            message=f"content root does not exist or is not a directory: {config.content_root}",
        )
    if not config.python_executable.is_file():
        return MCPCallError(
            code=MCPErrorCode.INVALID_CONFIG,
            phase=MCPPhase.CONFIGURE,
            message=f"Python executable does not exist: {config.python_executable}",
        )
    if config.source_root is not None and not config.source_root.is_dir():
        return MCPCallError(
            code=MCPErrorCode.INVALID_CONFIG,
            phase=MCPPhase.CONFIGURE,
            message=f"source root does not exist: {config.source_root}",
        )
    if not config.contracts:
        return MCPCallError(
            code=MCPErrorCode.INVALID_CONFIG,
            phase=MCPPhase.CONFIGURE,
            message="at least one MCP tool contract is required",
        )
    return None


def _prepare_arguments(
    config: LocalStdioConfig,
    contract: ToolContract,
    arguments: Mapping[str, Any],
) -> dict[str, Any] | MCPCallError:
    reserved = {"content_root", "sqlite_path", "persist"} & set(arguments)
    if reserved:
        return MCPCallError(
            code=MCPErrorCode.INVALID_ARGUMENT,
            phase=MCPPhase.CONFIGURE,
            message=(
                "content_root/sqlite_path/persist are host-owned and cannot be supplied "
                "by a tool task"
            ),
            details={"reserved_arguments": sorted(reserved)},
        )
    unknown = set(arguments) - set(contract.caller_arguments)
    if unknown:
        return MCPCallError(
            code=MCPErrorCode.INVALID_ARGUMENT,
            phase=MCPPhase.CONFIGURE,
            message="arguments outside the tool contract: " + ", ".join(sorted(unknown)),
        )
    for name, value in arguments.items():
        expected = contract.input_types[name]
        actual = _json_type(value)
        if actual not in expected:
            return MCPCallError(
                code=MCPErrorCode.INVALID_ARGUMENT,
                phase=MCPPhase.CONFIGURE,
                message=f"argument {name!r} has type {actual}; expected {sorted(expected)}",
            )
    if contract.name == "audit_project":
        limit = arguments.get("limit")
        if limit is not None and (isinstance(limit, bool) or not 1 <= limit <= 100):
            return MCPCallError(
                code=MCPErrorCode.INVALID_ARGUMENT,
                phase=MCPPhase.CONFIGURE,
                message="audit_project limit must be an integer from 1 through 100 or null",
            )
    prepared = {
        "content_root": str(config.content_root.resolve()),
        "persist": True,
        **dict(arguments),
    }
    # Bound the result before it crosses the subprocess pipe. The tool still reports the true
    # open_errors/total and a paging notice, so this is not a silent truncation.
    if contract.name == "audit_project" and prepared.get("limit") is None:
        prepared["limit"] = 50
    return prepared


def _subprocess_env(config: LocalStdioConfig) -> dict[str, str] | None:
    if config.source_root is None:
        return None
    # Do not inherit an arbitrary PYTHONPATH from the task.  The MCP SDK merges this small mapping
    # with its own safe OS allowlist (SYSTEMROOT/PATH/etc.) before launching the child.
    return {"PYTHONPATH": str(config.source_root.resolve())}


class _stderr_file:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.handle: TextIO | None = None

    def __enter__(self) -> TextIO:
        target = self.path if self.path is not None else Path(os.devnull)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = target.open("w", encoding="utf-8")
        return self.handle

    def __exit__(self, *_exc: object) -> None:
        if self.handle is not None:
            self.handle.close()


def _validate_initialize(initialized: Any, config: LocalStdioConfig) -> None:
    protocol = str(getattr(initialized, "protocolVersion", ""))
    accepted = config.accepted_protocol_versions or _sdk_protocol_versions()
    if not accepted or protocol not in accepted:
        raise _BoundaryFailure(
            MCPCallError(
                code=MCPErrorCode.PROTOCOL_DRIFT,
                phase=MCPPhase.INITIALIZE,
                message=f"server negotiated unsupported MCP protocol {protocol!r}",
                details={"accepted_protocol_versions": sorted(accepted)},
            )
        )
    server_info = getattr(initialized, "serverInfo", None)
    server_name = str(getattr(server_info, "name", ""))
    if server_name != config.expected_server_name:
        raise _BoundaryFailure(
            MCPCallError(
                code=MCPErrorCode.PROTOCOL_DRIFT,
                phase=MCPPhase.INITIALIZE,
                message=(
                    f"unexpected MCP server identity {server_name!r}; "
                    f"expected {config.expected_server_name!r}"
                ),
            )
        )
    capabilities = getattr(initialized, "capabilities", None)
    if capabilities is None or getattr(capabilities, "tools", None) is None:
        raise _BoundaryFailure(
            MCPCallError(
                code=MCPErrorCode.PROTOCOL_DRIFT,
                phase=MCPPhase.INITIALIZE,
                message="MCP server did not advertise the tools capability",
            )
        )


async def _discover_tools(session: Any, max_pages: int) -> dict[str, Any]:
    discovered: dict[str, Any] = {}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _ in range(max_pages):
        page = await session.list_tools(cursor=cursor)
        page_tools = getattr(page, "tools", None)
        if not isinstance(page_tools, list):
            raise _BoundaryFailure(
                MCPCallError(
                    code=MCPErrorCode.PROTOCOL_DRIFT,
                    phase=MCPPhase.DISCOVER,
                    message="list_tools response did not contain a tool list",
                )
            )
        for tool in page_tools:
            name = getattr(tool, "name", None)
            if not isinstance(name, str) or not name or name in discovered:
                raise _BoundaryFailure(
                    MCPCallError(
                        code=MCPErrorCode.PROTOCOL_DRIFT,
                        phase=MCPPhase.DISCOVER,
                        message="tool discovery returned a missing or duplicate tool name",
                    )
                )
            discovered[name] = tool
        next_cursor = getattr(page, "nextCursor", None)
        if next_cursor is None:
            return discovered
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
            raise _BoundaryFailure(
                MCPCallError(
                    code=MCPErrorCode.PROTOCOL_DRIFT,
                    phase=MCPPhase.DISCOVER,
                    message="tool discovery returned an invalid/cyclic pagination cursor",
                )
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise _BoundaryFailure(
        MCPCallError(
            code=MCPErrorCode.PROTOCOL_DRIFT,
            phase=MCPPhase.DISCOVER,
            message=f"tool discovery exceeded the {max_pages}-page safety limit",
        )
    )


async def _discover_primitives(session: Any, *, kind: str, max_pages: int) -> dict[str, Any]:
    """Bounded, duplicate-safe discovery for Resources or Prompts."""
    discovered: dict[str, Any] = {}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _ in range(max_pages):
        page = (
            await session.list_resources(cursor=cursor)
            if kind == "resource"
            else await session.list_prompts(cursor=cursor)
        )
        attribute = "resources" if kind == "resource" else "prompts"
        items = getattr(page, attribute, None)
        if not isinstance(items, list):
            raise _BoundaryFailure(
                MCPCallError(
                    code=MCPErrorCode.PROTOCOL_DRIFT,
                    phase=MCPPhase.DISCOVER,
                    message=f"list_{attribute} response did not contain a list",
                )
            )
        for item in items:
            raw_key = (
                getattr(item, "uri", None) if kind == "resource" else getattr(item, "name", None)
            )
            key = str(raw_key) if raw_key is not None else ""
            if not key or key in discovered:
                raise _BoundaryFailure(
                    MCPCallError(
                        code=MCPErrorCode.PROTOCOL_DRIFT,
                        phase=MCPPhase.DISCOVER,
                        message=f"{kind} discovery returned a missing or duplicate identity",
                    )
                )
            discovered[key] = item
        next_cursor = getattr(page, "nextCursor", None)
        if next_cursor is None:
            return discovered
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
            raise _BoundaryFailure(
                MCPCallError(
                    code=MCPErrorCode.PROTOCOL_DRIFT,
                    phase=MCPPhase.DISCOVER,
                    message=f"{kind} discovery returned an invalid/cyclic cursor",
                )
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise _BoundaryFailure(
        MCPCallError(
            code=MCPErrorCode.PROTOCOL_DRIFT,
            phase=MCPPhase.DISCOVER,
            message=f"{kind} discovery exceeded the {max_pages}-page safety limit",
        )
    )


def _validate_primitive_contract(*, kind: str, name: str, item: Any) -> str:
    """Fail closed when an allowlisted primitive's advertised contract drifts."""
    arguments = []
    for argument in getattr(item, "arguments", None) or []:
        arguments.append(
            {
                "name": str(getattr(argument, "name", "")),
                "required": bool(getattr(argument, "required", False)),
            }
        )
    if kind == "resource":
        advertised_uri = str(getattr(item, "uri", ""))
        mime_type = str(getattr(item, "mimeType", ""))
        if advertised_uri != name or mime_type != "application/json":
            raise _BoundaryFailure(
                MCPCallError(
                    code=MCPErrorCode.SCHEMA_DRIFT,
                    phase=MCPPhase.DISCOVER,
                    message=(
                        f"resource contract changed: uri={advertised_uri!r}, "
                        f"mime_type={mime_type!r}"
                    ),
                )
            )
    elif kind == "prompt":
        advertised_name = str(getattr(item, "name", ""))
        if advertised_name != name or arguments != [{"name": "focus", "required": False}]:
            raise _BoundaryFailure(
                MCPCallError(
                    code=MCPErrorCode.SCHEMA_DRIFT,
                    phase=MCPPhase.DISCOVER,
                    message=(
                        f"prompt contract changed: name={advertised_name!r}, "
                        f"arguments={arguments!r}"
                    ),
                )
            )
    else:  # Defensive guard: callers expose only the two explicit primitive kinds.
        raise _BoundaryFailure(
            MCPCallError(
                code=MCPErrorCode.INVALID_CONFIG,
                phase=MCPPhase.CONFIGURE,
                message=f"unsupported MCP primitive kind {kind!r}",
            )
        )
    identity = {
        "kind": kind,
        "name": str(getattr(item, "name", "")),
        "uri": str(getattr(item, "uri", "")),
        "mime_type": str(getattr(item, "mimeType", "")),
        "arguments": arguments,
    }
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def _decode_primitive_result(
    result: Any,
    *,
    kind: str,
    name: str,
    protocol_version: str,
    server_name: str,
    schema_fingerprint: str,
    limits: ResultLimits,
) -> MCPCallOutcome:
    if kind == "resource":
        contents = getattr(result, "contents", None)
        if not isinstance(contents, list) or len(contents) != 1:
            return _decode_error(
                MCPErrorCode.RESULT_TYPE,
                "resource result must contain exactly one text content block",
            )
        block = contents[0]
        text_value = getattr(block, "text", None)
        if not isinstance(text_value, str):
            return _decode_error(MCPErrorCode.RESULT_TYPE, "resource content must be text JSON")
        try:
            payload = json.loads(
                text_value,
                object_pairs_hook=_unique_object,
                parse_constant=lambda value: _reject_json_constant(value),
            )
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            return _decode_error(MCPErrorCode.RESULT_TYPE, f"resource is not strict JSON: {exc}")
        if not isinstance(payload, dict):
            return _decode_error(MCPErrorCode.RESULT_TYPE, "resource JSON must be an object")
        content_bytes = len(text_value.encode("utf-8"))
    else:
        messages = getattr(result, "messages", None)
        if not isinstance(messages, list) or not messages:
            return _decode_error(MCPErrorCode.RESULT_TYPE, "prompt result must contain messages")
        decoded_messages: list[dict[str, str]] = []
        for message in messages:
            content = getattr(message, "content", None)
            text_value = getattr(content, "text", None)
            if getattr(content, "type", None) != "text" or not isinstance(text_value, str):
                return _decode_error(
                    MCPErrorCode.RESULT_TYPE, "prompt messages must contain text only"
                )
            role_value = getattr(message, "role", "")
            role = getattr(role_value, "value", role_value)
            if role not in {"user", "assistant"}:
                return _decode_error(MCPErrorCode.RESULT_TYPE, "prompt message role is invalid")
            decoded_messages.append({"role": str(role), "text": text_value})
        payload = {"messages": decoded_messages}
        content_bytes = len(_canonical_json(payload).encode("utf-8"))

    if content_bytes > limits.max_result_bytes:
        return MCPCallError(
            code=MCPErrorCode.RESULT_TOO_LARGE,
            phase=MCPPhase.DECODE,
            message=(
                f"{kind} result is {content_bytes} bytes; limit is {limits.max_result_bytes} bytes"
            ),
            details={"content_bytes": content_bytes, "limit_bytes": limits.max_result_bytes},
        )
    unsafe = _validate_json_tree(payload, limits)
    if unsafe is not None:
        return _decode_error(MCPErrorCode.RESULT_UNSAFE, unsafe)
    return MCPObservation(
        tool_name=f"{kind}:{name}",
        data=payload,
        protocol_version=protocol_version,
        server_name=server_name,
        schema_fingerprint=schema_fingerprint,
        content_bytes=content_bytes,
        risk_flags=_prompt_injection_flags(payload),
    )


def _validate_tool_contract(tool: Any, contract: ToolContract) -> str:
    if getattr(tool, "name", None) != contract.name:
        _raise_schema(
            f"tool identity changed: server={getattr(tool, 'name', None)!r}, "
            f"expected={contract.name!r}"
        )
    schema = getattr(tool, "inputSchema", None)
    if not isinstance(schema, dict) or schema.get("type") != "object":
        _raise_schema("tool inputSchema must be an object schema")
    properties = schema.get("properties")
    required = schema.get("required", [])
    if (
        not isinstance(properties, dict)
        or not isinstance(required, list)
        or not all(isinstance(item, str) for item in required)
    ):
        _raise_schema("tool inputSchema has invalid properties/required fields")
    if set(required) != set(contract.required_inputs):
        _raise_schema(
            "required inputs changed: "
            f"server={sorted(required)}, expected={sorted(contract.required_inputs)}"
        )
    for name, expected_types in contract.input_types.items():
        prop = properties.get(name)
        if not isinstance(prop, dict):
            _raise_schema(f"expected input {name!r} is missing")
        actual_types = _schema_types(prop)
        if actual_types != expected_types:
            _raise_schema(
                f"input {name!r} types changed: "
                f"server={sorted(actual_types)}, expected={sorted(expected_types)}"
            )
    missing_default = object()
    for name, expected_default in contract.input_defaults.items():
        prop = properties.get(name)
        actual_default = (
            prop.get("default", missing_default) if isinstance(prop, dict) else missing_default
        )
        if (
            actual_default is missing_default
            or type(actual_default) is not type(expected_default)
            or actual_default != expected_default
        ):
            _raise_schema(
                f"input {name!r} default changed: "
                f"server={actual_default!r}, expected={expected_default!r}"
            )
    output_schema = getattr(tool, "outputSchema", None)
    if not isinstance(output_schema, dict) or output_schema.get("type") != "object":
        _raise_schema("tool outputSchema must advertise a JSON object")
    output_properties = output_schema.get("properties")
    output_required = output_schema.get("required", [])
    if not isinstance(output_properties, dict) or not isinstance(output_required, list):
        _raise_schema("tool outputSchema has invalid properties/required fields")
    for name, expected_types in contract.output_types.items():
        prop = output_properties.get(name)
        if not isinstance(prop, dict) or name not in output_required:
            _raise_schema(f"required output {name!r} is missing from outputSchema")
        actual_types = _schema_types(prop)
        if actual_types != expected_types:
            _raise_schema(
                f"output {name!r} types changed: "
                f"server={sorted(actual_types)}, expected={sorted(expected_types)}"
            )
    annotations = getattr(tool, "annotations", None)
    expected_annotations = {
        "readOnlyHint": contract.read_only,
        "destructiveHint": contract.destructive,
        "idempotentHint": contract.idempotent,
        "openWorldHint": contract.open_world,
    }
    actual_annotations = {name: getattr(annotations, name, None) for name in expected_annotations}
    for name, expected in expected_annotations.items():
        actual = actual_annotations[name]
        if actual is not expected:
            _raise_schema(f"{name} changed: server={actual!r}, expected={expected!r}")
    compatibility_surface = {
        "name": contract.name,
        "inputSchema": schema,
        "outputSchema": output_schema,
        "annotations": actual_annotations,
    }
    return hashlib.sha256(_canonical_json(compatibility_surface).encode("utf-8")).hexdigest()


def _raise_schema(message: str) -> NoReturn:
    raise _BoundaryFailure(
        MCPCallError(
            code=MCPErrorCode.SCHEMA_DRIFT,
            phase=MCPPhase.DISCOVER,
            message=message,
        )
    )


def _schema_types(schema: Mapping[str, Any]) -> frozenset[str]:
    direct = schema.get("type")
    if isinstance(direct, str):
        return frozenset({direct})
    variants = schema.get("anyOf")
    if not isinstance(variants, list):
        return frozenset()
    types: set[str] = set()
    for variant in variants:
        if isinstance(variant, dict):
            type_name = variant.get("type")
            if isinstance(type_name, str):
                types.add(type_name)
    return frozenset(types)


def _validate_output_contract(payload: Mapping[str, Any], contract: ToolContract) -> str | None:
    for name, expected in contract.output_types.items():
        if name not in payload:
            return f"required output {name!r} is missing"
        actual = _json_type(payload[name])
        if actual not in expected:
            return f"output {name!r} has type {actual}; expected {sorted(expected)}"
    return None


def _validate_audit_payload(payload: Mapping[str, Any]) -> str | None:
    content_hash = payload["content_hash"]
    if len(content_hash) != 64 or any(char not in "0123456789abcdef" for char in content_hash):
        return "audit content_hash is not a lowercase SHA-256 digest"
    open_errors = payload["open_errors"]
    if isinstance(open_errors, bool) or open_errors < 0:
        return "audit open_errors must be a non-negative integer"
    issues = payload["issues"]
    for index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            return f"audit issues[{index}] must be an object"
        required = {"id", "rule_code", "severity", "message", "evidence", "status"}
        if not required <= set(issue):
            return f"audit issues[{index}] is missing fields: {sorted(required - set(issue))}"
        string_fields = required - {"evidence", "id"}
        if not all(isinstance(issue[name], str) for name in string_fields):
            return f"audit issues[{index}] has non-string rule/status fields"
        # ``persist=False`` deliberately returns transient issues before SQLite assigns ids.
        if issue["id"] is not None and not isinstance(issue["id"], str):
            return f"audit issues[{index}].id must be a string or null"
        if not isinstance(issue["evidence"], list):
            return f"audit issues[{index}].evidence must be an array"
    total = payload.get("total")
    if total is not None and (
        isinstance(total, bool) or not isinstance(total, int) or total < len(issues)
    ):
        return "audit total must be an integer no smaller than the returned issue page"
    return None


def _validate_json_tree(root: Any, limits: ResultLimits) -> str | None:
    stack: list[tuple[Any, int]] = [(root, 1)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > limits.max_nodes:
            return f"tool result exceeds the {limits.max_nodes}-node limit"
        if depth > limits.max_depth:
            return f"tool result exceeds the {limits.max_depth}-level depth limit"
        if isinstance(value, str):
            if len(value) > limits.max_string_chars:
                return f"tool result contains a string longer than {limits.max_string_chars} chars"
            if _has_unsafe_control(value):
                return "tool result contains forbidden control or bidi-override characters"
        elif isinstance(value, dict):
            if len(value) > limits.max_collection_items:
                return "tool result object exceeds the collection-item limit"
            for key, child in value.items():
                if not isinstance(key, str):
                    return "tool result contains a non-string object key"
                if len(key) > 256 or _has_unsafe_control(key):
                    return "tool result contains an unsafe object key"
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            if len(value) > limits.max_collection_items:
                return "tool result array exceeds the collection-item limit"
            stack.extend((child, depth + 1) for child in value)
        elif isinstance(value, float) and not math.isfinite(value):
            return "tool result contains a non-finite number"
        elif value is not None and not isinstance(value, (bool, int, float)):
            return f"tool result contains unsupported value type {type(value).__name__}"
    return None


_BIDI_AND_INVISIBLE = {
    "\u200b",
    "\u200c",
    "\u200d",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
    "\ufeff",
}


def _has_unsafe_control(value: str) -> bool:
    return any(
        char in _BIDI_AND_INVISIBLE
        or (unicodedata.category(char) == "Cc" and char not in {"\n", "\r", "\t"})
        for char in value
    )


def _prompt_injection_flags(payload: Mapping[str, Any]) -> tuple[str, ...]:
    strings: list[str] = []
    stack: list[Any] = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, dict):
            strings.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    if any(scan_for_injection(value) for value in strings):
        return ("prompt_injection_pattern",)
    return ()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _decode_error(code: MCPErrorCode, message: str) -> MCPCallError:
    return MCPCallError(code=code, phase=MCPPhase.DECODE, message=_safe_text(message))


def _safe_message(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return _safe_text(f"{type(exc).__name__}: {text}")


def _find_group_exception(
    group: BaseExceptionGroup, expected: type[BaseException]
) -> BaseException | None:
    for nested in group.exceptions:
        if isinstance(nested, expected):
            return nested
        if isinstance(nested, BaseExceptionGroup):
            found = _find_group_exception(nested, expected)
            if found is not None:
                return found
    return None


def _safe_exception_group(group: BaseExceptionGroup) -> str:
    leaves: list[str] = []
    stack: list[BaseException] = list(group.exceptions)
    while stack and len(leaves) < 3:
        current = stack.pop(0)
        if isinstance(current, BaseExceptionGroup):
            stack[0:0] = list(current.exceptions)
        else:
            leaves.append(_safe_message(current))
    return _safe_text("MCP subprocess task failed: " + " | ".join(leaves))


def _transport_retryable(phase: MCPPhase, contract: ToolContract) -> bool:
    """Return whether replay is safe after a transport failure in ``phase``.

    Before dispatch, retrying cannot duplicate a tool side effect. Once CALL begins, the outcome
    is unknown, so only a contract explicitly advertised as idempotent may be replayed.
    """
    if phase in {MCPPhase.CONNECT, MCPPhase.INITIALIZE, MCPPhase.DISCOVER}:
        return True
    return phase is MCPPhase.CALL and contract.idempotent


def _timeout_error(
    config: LocalStdioConfig, phase: MCPPhase, contract: ToolContract
) -> MCPCallError:
    if phase is MCPPhase.CALL:
        return MCPCallError(
            code=MCPErrorCode.CALL_TIMEOUT,
            phase=MCPPhase.CALL,
            message=f"MCP tool call exceeded {config.call_timeout_seconds:g} seconds",
            retryable=contract.idempotent,
            details={
                "outcome": "unknown",
                "tool_idempotent": contract.idempotent,
            },
        )
    return MCPCallError(
        code=MCPErrorCode.CONNECTION_TIMEOUT,
        phase=phase,
        message=(
            "MCP subprocess handshake/discovery exceeded "
            f"{config.connect_timeout_seconds:g} seconds"
        ),
        retryable=True,
    )


def _primitive_timeout_error(config: LocalStdioConfig, phase: MCPPhase, kind: str) -> MCPCallError:
    if phase is MCPPhase.CALL:
        return MCPCallError(
            code=MCPErrorCode.CALL_TIMEOUT,
            phase=phase,
            message=f"MCP {kind} read exceeded {config.call_timeout_seconds:g} seconds",
            retryable=True,
            details={"outcome": "read_only"},
        )
    return MCPCallError(
        code=MCPErrorCode.CONNECTION_TIMEOUT,
        phase=phase,
        message=(
            f"MCP subprocess handshake/{kind} discovery exceeded "
            f"{config.connect_timeout_seconds:g} seconds"
        ),
        retryable=True,
    )


def _mcp_protocol_error(exc: BaseException, phase: MCPPhase) -> MCPCallError:
    error = getattr(exc, "error", None)
    jsonrpc_code = getattr(error, "code", None)
    message = _safe_message(exc)
    return MCPCallError(
        code=MCPErrorCode.REMOTE_ERROR if phase is MCPPhase.CALL else MCPErrorCode.PROTOCOL_DRIFT,
        phase=phase,
        message=message,
        retryable=False,
        details={"jsonrpc_code": jsonrpc_code} if isinstance(jsonrpc_code, int) else {},
        untrusted_input=True,
        risk_flags=("prompt_injection_pattern",) if scan_for_injection(message) else (),
    )


def _safe_text(value: str, limit: int = 800) -> str:
    cleaned = "".join(
        char
        if char not in _BIDI_AND_INVISIBLE
        and (char in {"\n", "\r", "\t"} or unicodedata.category(char) != "Cc")
        else "?"
        for char in value
    )
    return cleaned[:limit]
