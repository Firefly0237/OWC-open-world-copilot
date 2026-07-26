# 技能注册表：agent 的自描述能力层。每个技能带 描述/输入 schema/成本档/副作用标注。
# 关键：一份参数源同时喂文本 manifest 与原生 tools schema（两路一致）；run() 校验+强转参数；
#   WRITES_CANON 的技能永不被 agent 自动调用（写 canon 必须过人审）。
"""Skill registry — the self-describing capability layer an agent selects from.

A *skill* is one named capability with enough metadata for an agent (or a human) to decide
whether and how to call it: a description, an input schema, a cost tier, and — crucially for this
project — a declared *side effect* (read-only / proposes a patch / writes canon). The ReAct agent
in :mod:`owcopilot.agent` renders the registry into its tool manifest and dispatches by name; the
metadata lets it reason about cost and stay inside the safe action space (it never auto-invokes a
``writes_canon`` skill — that path stays with the human review queue).

This module holds only the *abstraction* (zero app dependencies, cheap to import). The concrete
OWCopilot skill set lives in :mod:`owcopilot.skills.builtin` and is re-exported below.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..errors import OWCopilotError


class CostTier(str, Enum):
    """How expensive a skill is to run."""

    DETERMINISTIC = "deterministic"  # pure code: $0, no model call
    LLM = "llm"  # may call a model; costed and slower


class SideEffect(str, Enum):
    """What a skill changes — the safety axis the agent reasons about."""

    READ_ONLY = "read_only"  # never writes canon content or files
    PROPOSES_PATCH = "proposes_patch"  # persists a *proposed* fix; no canon write
    WRITES_CANON = "writes_canon"  # writes approved content (never auto-invoked by the agent)


class SkillError(OWCopilotError, ValueError):
    """Raised for an unknown skill name or invalid arguments. The agent feeds the message back to
    the model as an observation so it can self-correct (e.g. supply a missing argument).

    Kept a ``ValueError`` (backward compatibility for every ``except ValueError`` / ``except
    SkillError`` site) *and* an :class:`~owcopilot.errors.OWCopilotError`. The three subtypes below
    name the failure *mode* so the recovery layer can classify by type (``isinstance``) rather than
    by scraping the message text — see :func:`owcopilot.skills.errors.classify_error`."""


class SkillNotFoundError(SkillError):
    """A skill / obs_id does not exist or is out of the agent's allowed scope → ``NOT_FOUND``."""


class SkillConflictError(SkillError):
    """The call is not allowed in this state or policy (WRITES_CANON auto-invoke ban, a
    double-registration, ``read_observation`` outside a run) → ``CONFLICT`` (do not just retry)."""


class SkillArgumentError(SkillError):
    """The arguments are missing / wrong type / wrong enum — a fixable argument problem →
    ``INVALID_ARG``. Bare :class:`SkillError` raises fall here too (the argument default)."""


# Map our compact parameter type names to JSON-schema types for the OpenAI tools serialization.
# Unknown types fall back to "string" (the safest permissive default).
_JSON_SCHEMA_TYPES: dict[str, str] = {
    "string": "string",
    "integer": "integer",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
    "number": "number",
}

#: Sentinel returned by ``_coerce_type`` when a value cannot be validated/coerced to a type.
_INVALID = object()

#: Strings a boolean parameter accepts (case-insensitive), for models that reply with text.
_TRUE_STRINGS = frozenset({"true", "1", "yes"})
_FALSE_STRINGS = frozenset({"false", "0", "no"})


def _coerce_type(type_name: str, value: Any) -> Any:
    """Validate ``value`` against a declared parameter type, coercing the common LLM slips.

    Lenient where it is safe — a stringified scalar (``"5"`` / ``"true"``) becomes the declared
    scalar type — but returns :data:`_INVALID` for a genuinely wrong shape (a string for an
    ``object`` param, ``"abc"`` for an ``integer``). ``bool`` is checked before ``int`` because it
    is an ``int`` subclass and must not satisfy an integer/number parameter. An unknown declared
    type passes through unchanged (matches the permissive JSON-schema fallback).
    """
    if type_name == "string":
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            return _INVALID
        if isinstance(value, (int, float)):
            return str(value)
        return _INVALID
    if type_name == "integer":
        if isinstance(value, bool):
            return _INVALID
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return _INVALID
        return _INVALID
    if type_name == "number":
        if isinstance(value, bool):
            return _INVALID
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return _INVALID
        return _INVALID
    if type_name == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return bool(value) if value in (0, 1) else _INVALID
        if isinstance(value, str):
            low = value.strip().lower()
            if low in _TRUE_STRINGS:
                return True
            if low in _FALSE_STRINGS:
                return False
        return _INVALID
    if type_name == "array":
        return value if isinstance(value, list) else _INVALID
    if type_name == "object":
        return value if isinstance(value, dict) else _INVALID
    return value  # unknown declared type → permissive passthrough


def _coerce_arg(skill_name: str, param: SkillParameter, value: Any) -> Any:
    """Coerce/validate one argument, raising :class:`SkillError` (fed back to the model) on a
    genuine type or enum violation."""
    coerced = _coerce_type(param.type, value)
    if coerced is _INVALID:
        raise SkillArgumentError(
            f"skill '{skill_name}' argument '{param.name}' expects {param.type}, "
            f"got {type(value).__name__} ({value!r})"
        )
    if param.enum is not None and coerced not in param.enum:
        raise SkillArgumentError(
            f"skill '{skill_name}' argument '{param.name}' must be one of "
            f"{list(param.enum)}, got {coerced!r}"
        )
    return coerced


@dataclass(frozen=True)
class SkillParameter:
    """One model-facing argument. Bound session arguments (content_root, sqlite_path) are NOT
    parameters — the registry injects those, so the agent never has to manage them."""

    name: str
    type: str  # "string" | "integer" | "boolean" | "array" | "object" | "number"
    description: str
    required: bool = False
    #: Optional closed set of allowed values. Enforced in ``Skill.run`` and rendered into the
    #: native tool schema so the model is told the choices up front.
    enum: tuple[str, ...] | None = None


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
        # Item 7: WRITES_CANON guard — canon writes require human review and must never be
        # auto-invoked by an agent.  Turning this design constraint into a code contract means
        # any future skill mis-classified as WRITES_CANON fails loudly at first call rather than
        # silently writing approved content without a human in the loop.
        if self.side_effect is SideEffect.WRITES_CANON:
            raise SkillConflictError(
                f"skill '{self.name}' has side_effect=WRITES_CANON and cannot be "
                "auto-invoked by the agent. Canon writes require human review."
            )
        missing = [p.name for p in self.parameters if p.required and p.name not in args]
        if missing:
            raise SkillArgumentError(
                f"skill '{self.name}' is missing required argument(s): {', '.join(missing)}"
            )
        # Only declared parameters are forwarded, and each is validated/coerced against its type
        # (and enum) so a mistyped tool-arg fails loudly here — fed back to the model to retry —
        # rather than reaching the handler as the wrong shape. Same surface for both the text and
        # the native function-calling paths, since both dispatch through here.
        by_name = {p.name: p for p in self.parameters}
        call_kwargs: dict[str, Any] = {}
        for key, value in args.items():
            param = by_name.get(key)
            if param is None:
                continue  # a hallucinated extra / re-supplied session arg — drop it
            call_kwargs[key] = _coerce_arg(self.name, param, value)
        return self.handler(**call_kwargs)

    def signature(self) -> str:
        """Render the call signature for the tool manifest, e.g. ``propose_fix(issue_id*: string,
        max_candidates: integer)``. A trailing ``*`` marks a required parameter; an enum parameter
        appends its allowed values so the text path advertises the same choices as the schema."""
        parts: list[str] = []
        for p in self.parameters:
            token = f"{p.name}{'*' if p.required else ''}: {p.type}"
            if p.enum is not None:
                token += f" (one of: {', '.join(p.enum)})"
            parts.append(token)
        return f"{self.name}({', '.join(parts)})"

    def manifest_line(self) -> str:
        return (
            f"- {self.signature()}: {self.description} "
            f"[{self.cost_tier.value}; {self.side_effect.value}]"
        )

    def openai_tool_schema(self) -> dict[str, Any]:
        """Render this skill as one OpenAI ``tools`` entry (function-calling schema).

        Used by the agent's opt-in native tool-calling path: the model receives this JSON schema
        and replies with structured ``tool_calls`` instead of the text ``Action:``/``Action Input:``
        format. Session-bound arguments (content_root, sqlite_path) are intentionally absent — same
        as the text manifest — because the registry injects them. Mirrors :meth:`run`'s validation
        surface so the two paths accept the same arguments.
        """
        properties: dict[str, Any] = {}
        required: list[str] = []
        for p in self.parameters:
            properties[p.name] = {"type": _JSON_SCHEMA_TYPES.get(p.type, "string")}
            if p.description:
                properties[p.name]["description"] = p.description
            if p.enum is not None:
                properties[p.name]["enum"] = list(p.enum)
            if p.required:
                required.append(p.name)
        parameters: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            parameters["required"] = required
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }


@dataclass
class SkillRegistry:
    """An ordered, name-addressed collection of skills."""

    _skills: dict[str, Skill] = field(default_factory=dict)

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            raise SkillConflictError(f"skill '{skill.name}' is already registered")
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill:
        skill = self._skills.get(name)
        if skill is None:
            known = ", ".join(self.names()) or "(none)"
            raise SkillNotFoundError(f"unknown skill '{name}'. Available skills: {known}")
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

    def manifest(self, allowed: set[str] | None = None) -> str:
        """Render every skill as a manifest block for an agent's system prompt.

        allowed=None (default): render ALL registered skills (backward-compatible).
        allowed=set[str]: render only skills whose name is in the set.
        Unknown names in allowed are silently ignored (the skill simply won't appear).
        """
        if allowed is None:
            skills: list[Skill] = list(self._skills.values())
        else:
            skills = [s for s in self._skills.values() if s.name in allowed]
        return "\n".join(skill.manifest_line() for skill in skills)

    def openai_tools(self, allowed: set[str] | None = None) -> list[dict[str, Any]]:
        """Render the skills as an OpenAI ``tools`` schema list (native function-calling).

        Honours the same ``allowed`` filter as :meth:`manifest`, so the native-tools path exposes
        exactly the same tool surface as the text path.
        """
        if allowed is None:
            skills: list[Skill] = list(self._skills.values())
        else:
            skills = [s for s in self._skills.values() if s.name in allowed]
        return [skill.openai_tool_schema() for skill in skills]


from .builtin import default_skill_registry  # noqa: E402  re-exported after the abstraction
from .scope import (  # noqa: E402  execution-time scoping, imported after the abstraction
    ScopedSkillRegistry,
    scoped_registry,
)

__all__ = [
    "CostTier",
    "ScopedSkillRegistry",
    "SideEffect",
    "Skill",
    "SkillArgumentError",
    "SkillConflictError",
    "SkillError",
    "SkillNotFoundError",
    "SkillParameter",
    "SkillRegistry",
    "default_skill_registry",
    "scoped_registry",
]
