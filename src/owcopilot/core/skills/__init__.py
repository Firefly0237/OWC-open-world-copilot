"""Skill registry — the self-describing capability layer an agent selects from.

A *skill* is one named capability with enough metadata for an agent (or a human) to decide
whether and how to call it: a description, an input schema, a cost tier, and — crucially for this
project — a declared *side effect* (read-only / proposes a patch / writes canon). The ReAct agent
in :mod:`owcopilot.agent` renders the registry into its tool manifest and dispatches by name; the
metadata lets it reason about cost and stay inside the safe action space (it never auto-invokes a
``writes_canon`` skill — that path stays with the human review queue).

This module holds only the *abstraction* (zero app dependencies, cheap to import). The concrete
OWCopilot skill set lives in :mod:`owcopilot.core.skills.builtin` and is re-exported below.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CostTier(str, Enum):
    """How expensive a skill is to run."""

    DETERMINISTIC = "deterministic"  # pure code: $0, no model call
    LLM = "llm"  # may call a model; costed and slower


class SideEffect(str, Enum):
    """What a skill changes — the safety axis the agent reasons about."""

    READ_ONLY = "read_only"  # never writes canon content or files
    PROPOSES_PATCH = "proposes_patch"  # persists a *proposed* fix; no canon write
    WRITES_CANON = "writes_canon"  # writes approved content (never auto-invoked by the agent)


class SkillError(ValueError):
    """Raised for an unknown skill name or invalid arguments. The agent feeds the message back to
    the model as an observation so it can self-correct (e.g. supply a missing argument)."""


@dataclass(frozen=True)
class SkillParameter:
    """One model-facing argument. Bound session arguments (content_root, sqlite_path) are NOT
    parameters — the registry injects those, so the agent never has to manage them."""

    name: str
    type: str  # "string" | "integer" | "boolean" | "array" | "object"
    description: str
    required: bool = False


@dataclass(frozen=True)
class Skill:
    """A single named capability plus the metadata an agent needs to call it safely."""

    name: str
    description: str
    cost_tier: CostTier
    side_effect: SideEffect
    handler: Callable[..., dict[str, Any]]
    parameters: tuple[SkillParameter, ...] = ()

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        """Validate ``args`` against the declared parameters, then call the handler.

        Only declared parameters are forwarded: a model that hallucinates an extra argument (or
        re-supplies a session argument like ``content_root``) can't crash the bound handler.
        """
        missing = [p.name for p in self.parameters if p.required and p.name not in args]
        if missing:
            raise SkillError(
                f"skill '{self.name}' is missing required argument(s): {', '.join(missing)}"
            )
        declared = {p.name for p in self.parameters}
        call_kwargs = {key: value for key, value in args.items() if key in declared}
        return self.handler(**call_kwargs)

    def signature(self) -> str:
        """Render the call signature for the tool manifest, e.g. ``propose_fix(issue_id*: string,
        max_candidates: integer)``. A trailing ``*`` marks a required parameter."""
        parts = [f"{p.name}{'*' if p.required else ''}: {p.type}" for p in self.parameters]
        return f"{self.name}({', '.join(parts)})"

    def manifest_line(self) -> str:
        return (
            f"- {self.signature()}: {self.description} "
            f"[{self.cost_tier.value}; {self.side_effect.value}]"
        )


@dataclass
class SkillRegistry:
    """An ordered, name-addressed collection of skills."""

    _skills: dict[str, Skill] = field(default_factory=dict)

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            raise SkillError(f"skill '{skill.name}' is already registered")
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill:
        skill = self._skills.get(name)
        if skill is None:
            known = ", ".join(self.names()) or "(none)"
            raise SkillError(f"unknown skill '{name}'. Available skills: {known}")
        return skill

    def names(self) -> list[str]:
        return list(self._skills)

    def __contains__(self, name: object) -> bool:
        return name in self._skills

    def __iter__(self) -> Iterator[Skill]:
        return iter(self._skills.values())

    def __len__(self) -> int:
        return len(self._skills)

    def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch by name. Raises :class:`SkillError` for an unknown name or bad arguments."""
        return self.get(name).run(args)

    def manifest(self) -> str:
        """Render every skill as a manifest block for an agent's system prompt."""
        return "\n".join(skill.manifest_line() for skill in self)


from .builtin import default_skill_registry  # noqa: E402  re-exported after the abstraction

__all__ = [
    "CostTier",
    "SideEffect",
    "Skill",
    "SkillError",
    "SkillParameter",
    "SkillRegistry",
    "default_skill_registry",
]
