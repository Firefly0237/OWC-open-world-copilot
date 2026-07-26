# 规约翻译 agent：确定性编排 + LLM 单节点 + 闭世界接地 + 回译确认。
"""Spec-authoring agent: turn a planner's plain-language intent into a checkable temporal spec.

Deterministic orchestration with the LLM confined to one node (structural translation); closed-world
grounding, expression assembly, type validation and back-translation are all deterministic. The
engine — never the model — decides truth; a human confirms the back-translation before a spec is
ever checked as an audit gate.
"""

from __future__ import annotations

from .grounding import SymbolCatalog, build_symbol_catalog, ground_surface
from .models import (
    DraftPattern,
    OperandGrounding,
    OperandStatus,
    OutcomeStatus,
    SpecDraft,
    TranslationOutcome,
)
from .pipeline import (
    GatewaySpecTranslator,
    OfflineSpecTranslator,
    ScriptedSpecTranslator,
    SpecTranslator,
    backtranslate_spec,
    draft_to_json,
    translate_intent,
)

__all__ = [
    "DraftPattern",
    "GatewaySpecTranslator",
    "OfflineSpecTranslator",
    "OperandGrounding",
    "OperandStatus",
    "OutcomeStatus",
    "ScriptedSpecTranslator",
    "SpecDraft",
    "SpecTranslator",
    "SymbolCatalog",
    "TranslationOutcome",
    "backtranslate_spec",
    "build_symbol_catalog",
    "draft_to_json",
    "ground_surface",
    "translate_intent",
]
