"""Execution-time tool scoping for multi-agent workers.

Background:
    ``SkillRegistry.manifest(allowed=...)`` only narrows the tool list *shown to the model*
    in the system prompt — it does not stop a model (or an instruction injected into an
    observation) from emitting an out-of-scope ``Action:`` that is still registered.
    ``SkillRegistry.run(name, args)`` dispatches purely by name, so a nominally read-only
    worker could reach a ``PROPOSES_PATCH`` skill at execution time.  The
    ``allowed_skills`` "whitelist" was therefore cosmetic — visible in the prompt, but not
    enforced at dispatch.

This module closes that gap *inside the multi_agent package only* (it does not edit the
shared ``core.skills`` registry or the ``agent.react`` loop).  ``scoped_registry`` returns a
thin proxy that quacks like a ``SkillRegistry`` but DENIES, at ``run()`` time, any skill not
in the allowed set — turning the whitelist into a real execution-layer sandbox.  A denied
call raises ``SkillError`` (the same type an unknown skill raises), so the ReAct loop already
handles it: the denial is fed back to the model as an ``is_error`` observation for self-
correction rather than crashing the agent.  Deny-by-default == minimal attack surface.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ..core.skills import Skill, SkillError, SkillRegistry


class ScopedSkillRegistry:
    """A read-through view over a ``SkillRegistry`` that enforces an allowed-skill set.

    Membership, iteration, and ``manifest`` are all filtered to the allowed set, and —
    crucially — ``run`` rejects any skill outside it.  The underlying registry is never
    mutated; this is a non-owning proxy.
    """

    def __init__(self, base: SkillRegistry, allowed: set[str]) -> None:
        self._base = base
        self._allowed = set(allowed)

    # -- execution gate ------------------------------------------------------
    def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name not in self._allowed:
            raise SkillError(
                f"skill '{name}' is not in this agent's allowed tool set "
                f"({sorted(self._allowed)}). Denied at execution (out of scope)."
            )
        # Defence in depth: even if allowed, a skill the base registry doesn't have must
        # surface the base registry's own unknown-skill error.
        return self._base.run(name, args)

    # -- read-through, filtered to the allowed set ---------------------------
    def get(self, name: str) -> Skill:
        if name not in self._allowed:
            raise SkillError(
                f"skill '{name}' is not in this agent's allowed tool set "
                f"({sorted(self._allowed)})."
            )
        return self._base.get(name)

    def names(self) -> list[str]:
        return [n for n in self._base.names() if n in self._allowed]

    def __contains__(self, name: object) -> bool:
        return name in self._allowed and name in self._base

    def __iter__(self) -> Iterator[Skill]:
        return (s for s in self._base if s.name in self._allowed)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def manifest(self, allowed: set[str] | None = None) -> str:
        # Intersect the caller-requested view with our hard scope so the manifest can never
        # advertise a tool the execution gate would deny.
        effective = self._allowed if allowed is None else (set(allowed) & self._allowed)
        return self._base.manifest(allowed=effective)


def scoped_registry(base: SkillRegistry, allowed: set[str] | None) -> SkillRegistry:
    """Return a registry scoped to ``allowed`` skills (execution-enforced).

    When ``allowed`` is ``None`` the base registry is returned unchanged (full access,
    backward-compatible).  Otherwise a :class:`ScopedSkillRegistry` proxy is returned; it is
    structurally compatible with ``SkillRegistry`` everywhere ``ReActAgent`` uses it
    (``manifest`` / ``run`` / ``__contains__``), so it is typed as ``SkillRegistry`` for the
    callers' convenience.
    """
    if allowed is None:
        return base
    return ScopedSkillRegistry(base, allowed)  # type: ignore[return-value]
