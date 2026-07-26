# 技能执行期作用域：把 --skills / worker 白名单变成真正的执行层沙箱（最小权限）。
"""Execution-time skill scoping — the single permission truth for the agent's tool surface.

Background:
    ``SkillRegistry.manifest(allowed=...)`` / ``openai_tools(allowed=...)`` only narrow the tool
    list *shown to the model*. They do not stop a model (or an instruction injected into an
    observation) from emitting an out-of-scope call for a skill that is still *registered*:
    ``SkillRegistry.run(name, args)`` dispatches purely by name. So a nominally read-only surface
    could reach a ``PROPOSES_PATCH`` skill at execution time — the "whitelist" was cosmetic.

``scoped_registry`` closes that gap by converging the permission to ONE place: it returns a view
that quacks like a :class:`~owcopilot.skills.SkillRegistry` but DENIES, at ``run()`` time, any skill
outside the allowed set. Because both the schema layer (``as_structured_tools`` / ``openai_tools``
iterate the registry) and the execution layer (``registry.run``) read through this one object, they
share the same scope by construction — there is no second gate to keep in sync. A denied call
raises :class:`~owcopilot.skills.SkillError` (the same type an unknown skill raises), so the ReAct
loop already handles it: the denial is fed back to the model as an ``is_error`` observation for
self-correction rather than crashing the agent. Deny-by-default == minimal attack surface.

Both the single-agent CLI path (``cli.main._cmd_agent`` for ``--skills``) and the multi-agent
workers/verifier use this — it is a general skill-permission concern, hence its home is the shared
``skills`` package (``multi_agent.skill_scope`` re-exports it for backward compatibility).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from . import Skill, SkillNotFoundError, SkillRegistry


class ScopedSkillRegistry:
    """A read-through view over a ``SkillRegistry`` that enforces an allowed-skill set.

    Membership, iteration, ``manifest`` and ``openai_tools`` are all filtered to the allowed set,
    and — crucially — ``run`` rejects any skill outside it. The underlying registry is never
    mutated; this is a non-owning proxy.
    """

    def __init__(self, base: SkillRegistry, allowed: set[str]) -> None:
        self._base = base
        self._allowed = set(allowed)

    # -- execution gate ------------------------------------------------------
    def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name not in self._allowed:
            raise SkillNotFoundError(
                f"skill '{name}' is not in this agent's allowed tool set "
                f"({sorted(self._allowed)}). Denied at execution (out of scope)."
            )
        # Defence in depth: even if allowed, a skill the base registry doesn't have must
        # surface the base registry's own unknown-skill error.
        return self._base.run(name, args)

    # -- read-through, filtered to the allowed set ---------------------------
    def get(self, name: str) -> Skill:
        if name not in self._allowed:
            raise SkillNotFoundError(
                f"skill '{name}' is not in this agent's allowed tool set ({sorted(self._allowed)})."
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

    def openai_tools(self, allowed: set[str] | None = None) -> list[dict[str, Any]]:
        # The native function-calling twin of ``manifest`` — same intersection semantics, so
        # the tools schema shown to a native-tool-calling model can never advertise a skill
        # the execution gate would deny.
        effective = self._allowed if allowed is None else (set(allowed) & self._allowed)
        return self._base.openai_tools(allowed=effective)


def scoped_registry(base: SkillRegistry, allowed: set[str] | None) -> SkillRegistry:
    """Return a registry scoped to ``allowed`` skills (execution-enforced).

    When ``allowed`` is ``None`` the base registry is returned unchanged (full access,
    backward-compatible). Otherwise a :class:`ScopedSkillRegistry` proxy is returned; it is
    structurally compatible with ``SkillRegistry`` everywhere ``ReActAgent`` uses it
    (``__iter__`` / ``run`` / ``__contains__`` / ``names`` / ``manifest`` / ``openai_tools``),
    so it is typed as ``SkillRegistry`` for the callers' convenience.
    """
    if allowed is None:
        return base
    return ScopedSkillRegistry(base, allowed)  # type: ignore[return-value]
