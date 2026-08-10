"""Route one existing Agent skill through MCP without widening its capability surface.

The product already has a complete, scoped :class:`~owcopilot.skills.SkillRegistry`.  MCP is an
execution transport for an allowlisted capability, not a second registry and not permission to
expose every server tool.  This module therefore replaces only ``audit_project`` while preserving
the original registry order and its host-owned semantic/no-progress contract.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from ..skills import SkillRegistry
from .skill_adapter import (
    MCPInvocationTrace,
    MCPToolClient,
    build_mcp_audit_skill,
)
from .types import LocalStdioConfig


def registry_with_mcp_audit(
    registry: SkillRegistry,
    config: LocalStdioConfig,
    *,
    client: MCPToolClient | None = None,
    trace_sink: Callable[[MCPInvocationTrace], None] | None = None,
) -> SkillRegistry:
    """Return a registry whose existing ``audit_project`` executes over checked MCP stdio.

    The native skill must already exist.  Its semantic projection and external-state marker are
    copied onto the MCP-backed handler, because transport metadata (latency, schema fingerprint)
    must not look like domain progress and an out-of-process audit still observes the same canon
    revision.  Every other ``Skill`` object is reused unchanged; names, order and permissions do
    not widen.
    """

    native_audit = registry.get("audit_project")
    mcp_audit = build_mcp_audit_skill(
        config,
        client=client,
        trace_sink=trace_sink,
    )
    routed_audit = replace(
        mcp_audit,
        semantic_projection=native_audit.semantic_projection,
        semantic_projection_name=native_audit.semantic_projection_name,
        state_marker=native_audit.state_marker,
        state_marker_policy=native_audit.state_marker_policy,
    )

    routed = SkillRegistry()
    for skill in registry:
        routed.register(routed_audit if skill.name == "audit_project" else skill)
    return routed


__all__ = ["registry_with_mcp_audit"]
