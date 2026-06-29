"""The built-in OWCopilot skill set: the existing read-only / propose tool surface, wrapped as
self-describing skills with each session argument (content_root, sqlite_path) bound in.

This deliberately mirrors the *safe* half of the MCP tool surface
(:mod:`owcopilot.mcp_server.tools`) — diagnosis and proposal only. Canon writes (review accept,
patch apply) and delivery (export) are intentionally excluded: the agent's whole action space is
deterministic, $0, and human-gated, so its only cost is its own reasoning.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from . import CostTier, SideEffect, Skill, SkillParameter, SkillRegistry

if TYPE_CHECKING:
    from ...pipeline.project import ProjectContext


def default_skill_registry(
    *,
    content_root: str,
    sqlite_path: str | None = None,
    project: ProjectContext | None = None,
) -> SkillRegistry:
    """Build the agent's capability layer, bound to one project.

    Two lifecycle modes, selected by ``project``:

    * ``project is None`` (default, unchanged): the bound tool handlers each open the project
      themselves (one fresh, consistent view per call). The agent always observes the latest
      persisted state because every call re-reads it. ``content_root`` / ``sqlite_path`` are bound
      here, not model-facing parameters, so the agent never has to manage them.
    * ``project`` is an already-open :class:`ProjectContext`: it is bound into every handler so the
      whole session (every ReAct step, every multi-agent worker) reuses **one** context — one
      parse / graph / vector build per task instead of one per tool call. Writes are immediately
      visible to later tools because they share the one live ``SQLiteStore`` connection. The caller
      that opened the shared context owns its lifecycle (open once at task start, close at the end);
      this function never opens or closes it.
    """
    # Lazy import keeps `owcopilot.core.skills` cheap to import and avoids any import-time coupling
    # to the (heavier) pipeline/llm stack the tool handlers pull in.
    from ...mcp_server import tools

    def bind(tool: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        """Inject the session args so they are never part of the model-facing parameter set.

        ``project`` is injected too when a shared context was supplied; the handlers treat a
        non-None ``project`` as "reuse this, don't open/close" (see ``mcp_server.tools._project``),
        so when ``project is None`` the partial is identical to the historical binding.
        """
        return partial(tool, content_root=content_root, sqlite_path=sqlite_path, project=project)

    registry = SkillRegistry()

    registry.register(
        Skill(
            name="audit_project",
            description=(
                "Run the deterministic consistency audit (broken refs, timeline/quest-logic, "
                "localization, injection). Persists issues you can then list or fix."
            ),
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=bind(tools.audit_project),
        )
    )
    registry.register(
        Skill(
            name="list_issues",
            description="List persisted audit issues, optionally filtered.",
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=bind(tools.list_issues),
            parameters=(
                SkillParameter("severity", "string", "error | warning", required=False),
                SkillParameter("rule_code", "string", "Filter by audit rule code.", required=False),
                SkillParameter("status", "string", "open | resolved | ignored", required=False),
            ),
        )
    )
    registry.register(
        Skill(
            name="build_context_pack",
            description=(
                "Retrieve the canon most relevant to a query (grounding lookup), with refs."
            ),
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=bind(tools.build_context_pack),
            parameters=(
                SkillParameter("query", "string", "What to look up in the world.", required=True),
                SkillParameter(
                    "budget_tokens", "integer", "Context size budget (default 800).", required=False
                ),
            ),
        )
    )
    registry.register(
        Skill(
            name="impact_of",
            description=(
                "Preview the blast radius of a planned change (pure graph walk, no model). "
                "Each change is an object {change_type, target_ref}."
            ),
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=bind(tools.impact_of),
            parameters=(
                SkillParameter(
                    "changes",
                    "array",
                    "List of {change_type, target_ref}; change_type one of entity_rename, "
                    "entity_delete, entity_field_change, relation_change, content_change.",
                    required=True,
                ),
                SkillParameter(
                    "max_depth", "integer", "Graph walk depth (default 2).", required=False
                ),
            ),
        )
    )
    registry.register(
        Skill(
            name="propose_fix",
            description=(
                "Propose shadow-validated fix candidates for one audit issue (any candidate that "
                "would add new errors is dropped). Stores PROPOSALS only — never writes canon."
            ),
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.PROPOSES_PATCH,
            handler=bind(tools.propose_fix),
            parameters=(
                SkillParameter(
                    "issue_id",
                    "string",
                    "Issue id from audit_project / list_issues.",
                    required=True,
                ),
                SkillParameter(
                    "max_candidates", "integer", "Max candidates (default 3).", required=False
                ),
            ),
        )
    )
    registry.register(
        Skill(
            name="quality_harness",
            description=(
                "Run the consolidated quality loop: audit + export gate + readiness + fix "
                "proposals, and report the project phase plus the safe next tool calls."
            ),
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=bind(tools.quality_harness),
            parameters=(
                SkillParameter(
                    "max_issues", "integer", "Top issues to inspect (default 5).", required=False
                ),
                SkillParameter(
                    "propose_fixes",
                    "boolean",
                    "Include fix proposals (default true).",
                    required=False,
                ),
            ),
        )
    )
    return registry
