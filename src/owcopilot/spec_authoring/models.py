# 规约翻译的数据模型：LLM 只产出的结构化草稿 + 闭世界接地结果 + 供人确认的产物。
"""Data models for the spec-authoring pipeline (NL intent → checkable temporal spec).

The pipeline is deliberately split so the LLM decides only ONE thing — the coarse property shape and
which *surface* names the planner referred to (a :class:`SpecDraft`). Everything downstream is
deterministic: closed-world grounding of those surfaces to real flags, expression assembly, type
validation, and a natural-language back-translation for the planner to confirm.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from ..logic.temporal import TemporalSpec


class DraftPattern(str, Enum):
    """The coarse property shape the LLM classifies the intent into (restricted, enumerable — not
    free-form logic). Maps deterministically to a
    :class:`~owcopilot.logic.temporal.PropertyPattern` during assembly. ``UNSUPPORTED`` covers
    B-tier/numeric intents the A-tier engine does not decide."""

    MUTEX = "MUTEX"  # two flags must not both hold → AG not(a and b)
    NEVER = "NEVER"  # a flag must never hold → AG not(a)
    ALWAYS = "ALWAYS"  # a flag must always hold → AG a
    REACHABLE = "REACHABLE"  # a flag must be attainable → EF a
    NO_SOFTLOCK = "NO_SOFTLOCK"  # never softlock → AG EF ending
    UNSUPPORTED = "UNSUPPORTED"  # precedence/response/numeric — outside the A-tier templates


class SpecDraft(BaseModel):
    """The LLM node's sole output: a pattern + the surface strings it identified. The surfaces are
    NOT real flag ids — grounding resolves them against the closed flag catalog; the model is never
    trusted to invent a flag id."""

    pattern: DraftPattern
    operands: list[str] = Field(default_factory=list)  # surface names the planner referred to
    description: str = ""


class OperandStatus(str, Enum):
    GROUNDED = "grounded"  # resolved to a real logic flag
    ENTITY_NO_FLAG = "entity_no_flag"  # names a canon entity but no flag represents its state
    UNKNOWN = "unknown"  # not in the closed world at all


class OperandGrounding(BaseModel):
    """The closed-world grounding of one draft surface."""

    surface: str
    status: OperandStatus
    flag_id: str = ""
    flag_name: str = ""
    confidence: float = 0.0
    note: str = ""


class OutcomeStatus(str, Enum):
    NEEDS_CONFIRMATION = "needs_confirmation"  # a grounded, type-valid candidate awaiting a human
    REJECTED = "rejected"  # closed-world grounding or type check failed
    UNSUPPORTED = "unsupported"  # the pattern is outside the A-tier templates


class TranslationOutcome(BaseModel):
    """The full, auditable result of translating one intent — the deterministic verdict, a candidate
    spec (only when it grounded), the per-surface grounding trail, and the back-translation the
    planner confirms. The pipeline never auto-applies the spec; confirmation is a human step."""

    intent: str
    status: OutcomeStatus
    draft: SpecDraft
    groundings: list[OperandGrounding] = Field(default_factory=list)
    spec: TemporalSpec | None = None
    back_translation: str = ""
    message: str = ""
