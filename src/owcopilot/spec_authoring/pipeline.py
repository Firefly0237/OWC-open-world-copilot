# 规约翻译编排：确定性小图 + LLM 只在「结构翻译」一格；接地/组装/回译全确定性。
"""The spec-authoring pipeline: a deterministic orchestration with the LLM confined to one node.

Flow (each step a named, deterministic node except ``translate``):

    intent --translate(LLM)--> SpecDraft --ground--> closed-world symbols
        --assemble--> TemporalSpec --validate(parse+type)--> back-translate --> needs_confirmation

    (grounding rejects any surface outside canon; assembly rejects on arity / type error.)

Why this shape (design evidence): end-to-end NL→formula accuracy is low (GPT-4 ~77.7%), but
splitting into *structural translation* (the LLM's one job) and *closed-world grounding*
(deterministic) makes each step independently checkable. The deterministic engine — never the
model — decides truth: the model only proposes a shape and names; grounding, assembly and the
temporal check are all code.

The LLM node is an injectable :class:`SpecTranslator`. :class:`OfflineSpecTranslator` makes the
whole pipeline runnable and testable at $0 (it extracts explicitly-quoted operand surfaces and
classifies the pattern by keyword); :class:`GatewaySpecTranslator` swaps in a real model and
nothing else changes. Truth-deciding never depends on which translator is used.
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_validated
from ..logic.expr import LogicSyntaxError, parse_expr
from ..logic.semantics import type_errors
from ..logic.temporal import PropertyPattern, TemporalSpec
from .grounding import SymbolCatalog, ground_surface
from .models import (
    DraftPattern,
    OperandGrounding,
    OperandStatus,
    OutcomeStatus,
    SpecDraft,
    TranslationOutcome,
)

# How many grounded flags each pattern needs, and the (engine pattern, expression template).
_PATTERN_ARITY = {
    DraftPattern.MUTEX: 2,
    DraftPattern.NEVER: 1,
    DraftPattern.ALWAYS: 1,
    DraftPattern.REACHABLE: 1,
    DraftPattern.NO_SOFTLOCK: 0,
}


class SpecTranslator(Protocol):
    """The one LLM node: classify a plain-language intent into a restricted :class:`SpecDraft`
    (pattern + surface names). It never emits real flag ids or a full formula."""

    def translate(self, intent: str, catalog: SymbolCatalog) -> SpecDraft: ...


# --------------------------------------------------------------------------------------------------
# Orchestration (deterministic).
# --------------------------------------------------------------------------------------------------


def translate_intent(intent: str, quest, bundle, translator: SpecTranslator) -> TranslationOutcome:
    """Run the full pipeline for one intent against one quest. Returns an auditable outcome; never
    mutates the quest and never auto-applies the spec (confirmation is a human step)."""
    from .grounding import build_symbol_catalog

    catalog = build_symbol_catalog(quest, bundle)
    draft = translator.translate(intent, catalog)

    if draft.pattern is DraftPattern.UNSUPPORTED:
        return TranslationOutcome(
            intent=intent,
            status=OutcomeStatus.UNSUPPORTED,
            draft=draft,
            message=(
                "该意图不属于 A 档可判定的模式（状态不变式 / 可达 / 软锁）；"
                "先序/响应等时序性质属 B 档，数值区间请直接声明 int 规约。"
            ),
        )

    arity = _PATTERN_ARITY[draft.pattern]
    groundings = [ground_surface(s, catalog) for s in draft.operands[:arity]]
    if len(groundings) < arity:
        got = len(groundings)
        return TranslationOutcome(
            intent=intent,
            status=OutcomeStatus.REJECTED,
            draft=draft,
            groundings=groundings,
            message=f"模式 {draft.pattern.value} 需要 {arity} 个符号，但只识别到 {got} 个。",
        )
    rejected = [g for g in groundings if g.status is not OperandStatus.GROUNDED]
    if rejected:
        return TranslationOutcome(
            intent=intent,
            status=OutcomeStatus.REJECTED,
            draft=draft,
            groundings=groundings,
            message="闭世界校验未通过：" + "；".join(g.note for g in rejected),
        )

    spec = _assemble_spec(intent, draft, groundings, quest)
    if spec is None:
        return TranslationOutcome(
            intent=intent,
            status=OutcomeStatus.REJECTED,
            draft=draft,
            groundings=groundings,
            message="组装后的规约表达式类型检查未通过（符号类型与模式不匹配）。",
        )

    return TranslationOutcome(
        intent=intent,
        status=OutcomeStatus.NEEDS_CONFIRMATION,
        draft=draft,
        groundings=groundings,
        spec=spec,
        back_translation=backtranslate_spec(spec, draft.pattern, groundings),
        message="已产出候选规约，请策划确认回译是否符合原意后再纳入审计。",
    )


def _assemble_spec(
    intent: str, draft: SpecDraft, groundings: list[OperandGrounding], quest
) -> TemporalSpec | None:
    """Build the temporal expression from the pattern + grounded flag ids, then type-check it
    against the quest's symbol table. Returns None on a type error (a reject signal)."""
    ids = [g.flag_id for g in groundings]
    if draft.pattern is DraftPattern.MUTEX:
        pattern, expression = PropertyPattern.AG, f"not ({ids[0]} and {ids[1]})"
    elif draft.pattern is DraftPattern.NEVER:
        pattern, expression = PropertyPattern.AG, f"not {ids[0]}"
    elif draft.pattern is DraftPattern.ALWAYS:
        pattern, expression = PropertyPattern.AG, ids[0]
    elif draft.pattern is DraftPattern.REACHABLE:
        pattern, expression = PropertyPattern.EF, ids[0]
    else:  # NO_SOFTLOCK
        pattern, expression = PropertyPattern.AG_EF_ENDING, ""

    if expression:
        symbols = {v.id: v.type.value for v in (quest.logic.variables if quest.logic else [])}
        try:
            tree = parse_expr(expression)
        except LogicSyntaxError:
            return None
        if type_errors(tree, symbols):
            return None
    # A stable content-derived id allows one quest to carry several authored properties while an
    # identical candidate keeps the same id and is therefore rejected as a duplicate on accept.
    spec_fingerprint = hashlib.sha256(
        f"{quest.id}\0{pattern.value}\0{expression}\0{intent.strip()}".encode()
    ).hexdigest()[:12]
    return TemporalSpec(
        id=f"{quest.id}_authored_{spec_fingerprint}",
        pattern=pattern,
        expression=expression,
        description=draft.description or intent,
        provenance="planner",
    )


def backtranslate_spec(
    spec: TemporalSpec, pattern: DraftPattern, groundings: list[OperandGrounding]
) -> str:
    """Render the assembled spec back into plain language for the planner to confirm — the HITL
    checkpoint. Uses flag *names* (not ids) so the sentence reads like the original intent."""
    names = [g.flag_name or g.flag_id for g in groundings]
    if pattern is DraftPattern.MUTEX:
        return f"在任何可达状态下，『{names[0]}』与『{names[1]}』不会同时成立。"
    if pattern is DraftPattern.NEVER:
        return f"在任何可达状态下，『{names[0]}』永远不成立。"
    if pattern is DraftPattern.ALWAYS:
        return f"在任何可达状态下，『{names[0]}』始终成立。"
    if pattern is DraftPattern.REACHABLE:
        return f"存在一个可达状态，使『{names[0]}』成立；这不表示该状态之后能够通关。"
    return "无论走到哪个可达状态，都仍存在一条走到某个结局的路径（不会卡死 / 无软锁）。"


# --------------------------------------------------------------------------------------------------
# The LLM node — offline ($0, deterministic) and gateway-backed (real model) translators.
# --------------------------------------------------------------------------------------------------

_QUOTE_PAIRS = '「」『』“”""【】《》'


def _extract_quoted(intent: str) -> list[str]:
    """Pull out explicitly-quoted operand surfaces (「…」/『…』/\"…\"/【…】/《…》), in order."""
    out: list[str] = []
    openers = {"「": "」", "『": "』", "“": "”", '"': '"', "【": "】", "《": "》"}
    i = 0
    while i < len(intent):
        ch = intent[i]
        closer = openers.get(ch)
        if closer is not None:
            end = intent.find(closer, i + 1)
            if end > i:
                token = intent[i + 1 : end].strip()
                if token:
                    out.append(token)
                i = end + 1
                continue
        i += 1
    return out


def _classify_pattern(intent: str) -> DraftPattern:
    """Keyword classifier, deliberately generic (not tuned to any single case). Order matters:
    soft-lock and mutex are checked before the broad ALWAYS keywords they may textually contain."""
    _softlock = ("软锁", "卡死", "死局", "都能通关", "不会卡", "总能通关", "不卡")
    _mutex = ("互斥", "不能同时", "不该同时", "不会同时", "不得同时", "不应同时")
    _reach = ("可达", "能达成", "可以达到", "应可达", "能够达成", "能到达", "应当可达")
    if any(k in intent for k in _softlock):
        return DraftPattern.NO_SOFTLOCK
    if any(k in intent for k in _mutex):
        return DraftPattern.MUTEX
    if any(k in intent for k in _reach):
        return DraftPattern.REACHABLE
    if any(k in intent for k in ("永不", "永远不", "从不", "绝不", "始终不", "不得", "不应")):
        return DraftPattern.NEVER
    if any(k in intent for k in ("始终", "总是", "永远", "一直", "恒")):
        return DraftPattern.ALWAYS
    return DraftPattern.UNSUPPORTED


class OfflineSpecTranslator:
    """Deterministic, $0 stand-in for the LLM translate node.

    Classifies the pattern by keyword and takes the explicitly-quoted spans as operand surfaces —
    enough to run the whole pipeline (grounding, assembly, back-translation, and the reject paths)
    offline. A real model identifies surfaces from unquoted prose; the pipeline is identical either
    way because grounding, not the translator, decides which real flag a surface is."""

    def translate(self, intent: str, catalog: SymbolCatalog) -> SpecDraft:
        pattern = _classify_pattern(intent)
        operands = [] if pattern is DraftPattern.NO_SOFTLOCK else _extract_quoted(intent)
        return SpecDraft(pattern=pattern, operands=operands, description=intent.strip())


_SYSTEM_PROMPT = (
    "你是把策划的大白话意图翻译成『受限时序性质骨架』的助手。只输出 JSON，不要解释。\n"
    "只能在这些模式里选一个 pattern："
    "MUTEX（两者不该同时成立）/ NEVER（某状态永不成立）/ ALWAYS（某状态始终成立）/ "
    "REACHABLE（某状态可达）/ NO_SOFTLOCK（任何时候都能通关，不卡死）/ "
    "UNSUPPORTED（先序、响应、数值区间等 A 档之外的性质）。\n"
    "operands 只能填意图里提到的『状态/flag 的大白话名字』（字符串），"
    "绝不要发明或猜测 flag id——接地由后续确定性步骤负责。"
    "MUTEX 填 2 个，NEVER/ALWAYS/REACHABLE 填 1 个，NO_SOFTLOCK 填 0 个。\n"
    '输出格式：{"pattern": "...", "operands": ["...", "..."], "description": "对意图的简述"}'
)


class GatewaySpecTranslator:
    """Real-model translate node: asks a model (via the single gateway chokepoint) for a
    :class:`SpecDraft` JSON and validates it in code (never trusts the shape). Falls back to the
    offline classifier if the model reply cannot be validated, so a bad reply degrades rather than
    crashes the pipeline."""

    def __init__(self, gateway: LLMGateway, *, task: str = "spec_translate") -> None:
        self.gateway = gateway
        self.task = task
        self.used_fallback = False
        self.fallback_reason = ""

    def translate(self, intent: str, catalog: SymbolCatalog) -> SpecDraft:
        # One translator instance may be reused; report the state of this call, not a previous one.
        self.used_fallback = False
        self.fallback_reason = ""
        flags = ", ".join(sorted(catalog.flag_ids)) or "(该任务未声明 flag)"
        user = f"任务可用 flag：{flags}\n策划意图：{intent}\n请只输出 JSON。"
        try:
            reply = self.gateway.complete(task=self.task, system=_SYSTEM_PROMPT, user=user)
            return extract_validated(reply, SpecDraft)
        except (ValueError, KeyError) as exc:
            self.used_fallback = True
            # Category-only on purpose: raw provider/model output can contain project text and must
            # not leak into telemetry or a browser error surface.
            self.fallback_reason = f"invalid_structured_output:{type(exc).__name__}"
            return OfflineSpecTranslator().translate(intent, catalog)


class ScriptedSpecTranslator:
    """A translator that returns a fixed :class:`SpecDraft` (or replays a JSON string through the
    same validation the gateway uses). For tests that need to drive a specific model output
    deterministically, bypassing the keyword classifier."""

    def __init__(self, draft: SpecDraft | str) -> None:
        self._draft = draft

    def translate(self, intent: str, catalog: SymbolCatalog) -> SpecDraft:
        if isinstance(self._draft, str):
            return extract_validated(self._draft, SpecDraft)
        return self._draft


def draft_to_json(draft: SpecDraft) -> str:
    """Serialize a draft the way a model would emit it (for scripted-translator tests)."""
    return json.dumps(draft.model_dump(), ensure_ascii=False)
