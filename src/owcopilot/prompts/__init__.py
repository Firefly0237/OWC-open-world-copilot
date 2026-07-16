# prompt 注册表：全项目面向模型 prompt 的可审查索引。prompt 仍留在各自使用处（内聚+动态插值），
#   注册表只按导入路径引用并挂元数据（用途/task/契约/版本）。防漂移测试保证新 prompt 必登记。
"""Prompt registry — the reviewable index of every model-facing prompt in the project.

Prompts stay WHERE THEY ARE USED (module constants / builder functions — cohesion and the
dynamic parts they interpolate live there); this registry *references* them by import path and
attaches the metadata that makes prompt engineering reviewable: purpose, the router task label
the call is billed/routed under, the output contract the model must honour, and a version for
A/B iteration. ``owcopilot prompts`` renders the registry; a drift test asserts every
``*_SYSTEM/_PROMPT`` module constant in the codebase is registered, so a new prompt cannot be
added silently.

Kinds:
- ``static``    — a complete system prompt used verbatim.
- ``template``  — a complete system prompt with named slots (e.g. ``{manifest}``).
- ``fragment``  — a reusable piece composed into a larger prompt at call time.
- ``builder``   — assembled by a function; the registry references the builder, whose docstring
  and source are the authoritative description of the assembly.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PromptSpec:
    """One registered prompt: identity + review metadata + a live reference to the object."""

    name: str  # unique dotted id, e.g. "agent.react.system.text"
    purpose: str  # one line: what this prompt makes the model do
    task: str  # router/telemetry task label the call runs under ("" = composed into another)
    kind: str  # "static" | "template" | "fragment" | "builder"
    output_contract: str  # what shape the model must reply in
    source: str  # "module.path:ATTRIBUTE" — resolved live, never a copy of the text
    version: int = 1
    #: Optional dotted path to the Pydantic model this prompt's output is validated against
    #: (e.g. "owcopilot.qa.models:QAAnswer"), making the output contract machine-verifiable.
    #: Additive + last: default None keeps every existing spec/CLI view byte-identical to before.
    schema: str | None = None

    def resolve(self) -> Any:
        """Import and return the referenced object (the actual prompt text / dict / function).

        Dotted attributes ("Class.method") resolve through each segment.
        """
        module_path, _, attr = self.source.partition(":")
        obj: Any = importlib.import_module(module_path)
        for part in attr.split("."):
            obj = getattr(obj, part)
        return obj

    def text(self) -> str | None:
        """The prompt text for static/template/fragment kinds; ``None`` for builders.

        A dict-valued fragment set (e.g. per-stage suffixes) renders as labelled sections.
        """
        if self.kind == "builder":
            return None
        obj = self.resolve()
        if isinstance(obj, dict):
            return "\n\n".join(f"[{key}]\n{value}" for key, value in obj.items())
        return str(obj)

    def resolve_schema(self) -> Any:
        """Import and return the Pydantic model referenced by :attr:`schema`, or ``None``.

        ``None`` both when ``schema`` is unset (most prompts: free text, or an output shaped by
        hand-written parsing/merging code rather than validated straight against one model) and
        never as a silent failure otherwise — a stale dotted path raises ``ImportError`` /
        ``AttributeError`` immediately, exactly like :meth:`resolve`.
        """
        if self.schema is None:
            return None
        module_path, _, attr = self.schema.partition(":")
        obj: Any = importlib.import_module(module_path)
        for part in attr.split("."):
            obj = getattr(obj, part)
        return obj


def _spec(
    name: str,
    purpose: str,
    task: str,
    kind: str,
    output_contract: str,
    source: str,
    *,
    schema: str | None = None,
) -> PromptSpec:
    return PromptSpec(
        name=name,
        purpose=purpose,
        task=task,
        kind=kind,
        output_contract=output_contract,
        source=source,
        schema=schema,
    )


#: Every model-facing prompt in the project. Grouped by domain; names are stable ids.
PROMPT_SPECS: tuple[PromptSpec, ...] = (
    # -- agent loop ---------------------------------------------------------------------------
    _spec(
        "agent.react.system.text",
        "Drive the text ReAct loop: one Thought + one Action (or Final Answer) per turn, never "
        "self-authored Observations, tools only from the manifest.",
        "agent_react",
        "template",
        "Thought/Action/Action Input lines, or Thought/Final Answer",
        "owcopilot.agent.react:_SYSTEM_PROMPT",
    ),
    _spec(
        "agent.react.system.native",
        "Drive the native function-calling loop: same role/safety rules as the text prompt; "
        "actions are structured tool_calls and the tool list travels in the tools schema, not "
        "in the prompt (hence static, no {manifest} slot).",
        "agent_react",
        "static",
        "tool_calls (OpenAI function-calling) or plain-text final report",
        "owcopilot.agent.react:_NATIVE_SYSTEM_PROMPT",
    ),
    _spec(
        "agent.react.generate_finish",
        'One extra budget-exhausted finish call (LangChain early_stopping_method="generate" '
        "semantics, opt-in): forbid further tool calls and answer from the transcript alone, or "
        "admit the transcript is insufficient and summarize what is known.",
        "agent_react",
        "static",
        "Thought/Final Answer lines (no Action)",
        "owcopilot.agent.react:_GENERATE_FINISH_SYSTEM",
    ),
    _spec(
        "agent.compressor.system",
        "Compress older transcript steps into a dense factual summary when the token budget "
        "threshold is crossed, preserving decisions and open leads.",
        "compact",
        "static",
        "plain-text summary",
        "owcopilot.agent.context_compressor:_COMPACT_SYSTEM_PROMPT",
    ),
    _spec(
        "agent.reflexion.self_reflection",
        "Self-Reflection component of the Reflexion loop (Shinn 2023): turn a failed trial's "
        "evaluator feedback into one concise verbal reflection fed forward to the next attempt.",
        "agent_react",
        "static",
        "one plain-text reflection sentence (no preamble)",
        "owcopilot.agent.reflexion:_REFLECTION_SYSTEM_PROMPT",
    ),
    # -- assist / content generation ----------------------------------------------------------
    _spec(
        "assist.engine_explain.system",
        "Summarise deterministic engine-side issue diagnoses (preflight/sync-back/recognize) into "
        "a short planner-facing narrative: explain + suggest only, decide nothing.",
        "engine_explain",
        "static",
        "short Chinese prose (no JSON)",
        "owcopilot.assist.engine_explain:_ENGINE_EXPLAIN_SYSTEM",
    ),
    _spec(
        "assist.characters.system",
        "Generate a grounded character profile sheet (persona, voice, relations) from canon "
        "evidence only.",
        "character_profile",
        "static",
        "one JSON object (profile fields)",
        "owcopilot.assist.characters:_SYSTEM_PROMPT",
    ),
    _spec(
        "assist.dialogue_trees.system",
        "Generate a branching dialogue tree grounded in canon; speakers/conditions must resolve "
        "to real entities and state vars.",
        "dialogue_tree",
        "static",
        "one JSON object (nodes/edges)",
        "owcopilot.assist.dialogue_trees:_SYSTEM_PROMPT",
    ),
    _spec(
        "assist.flavor.system",
        "Generate flavor-text batches (item/POI blurbs) in the world's register, grounded in "
        "the provided canon rows.",
        "flavor_batch",
        "static",
        "one JSON array of flavor entries",
        "owcopilot.assist.flavor:_SYSTEM_PROMPT",
    ),
    _spec(
        "assist.critic.verdict_shape",
        "Shared verdict schema appended to every critic prompt so all critics reply in one "
        "parseable shape.",
        "",
        "fragment",
        "JSON verdict {verdict, score, fixes, ...}",
        "owcopilot.assist.critic:_VERDICT_SHAPE",
        # Every critic (quest/character/dialogue/bark/flavor) appends this fragment and then
        # parses its reply through the SAME `parse_critique` -> CritiqueResult path — see
        # assist.critic.system below.
        schema="owcopilot.assist.critic:CritiqueResult",
    ),
    _spec(
        "assist.critic.system",
        "Family of per-artifact critic prompts (quest/character/dialogue/bark/flavor): critique "
        "against canon + style guides, dimension-aware, lessons optionally appended.",
        "quest_critique",
        "builder",
        "JSON verdict (see assist.critic.verdict_shape)",
        "owcopilot.assist.critic:_critic_system_prompt",
        # parse_critique() (critic.py) builds CritiqueResult directly from the parsed JSON's
        # verdict/score/summary/dimensions fields.
        schema="owcopilot.assist.critic:CritiqueResult",
    ),
    _spec(
        "assist.contradiction.judge",
        "LLM tier of the layered contradiction scanner: judge whether two canon statements "
        "genuinely contradict (conservative; structural tiers run first).",
        "contradiction_judge",
        "static",
        'JSON {"judgements": [{"index": int, "contradicts": bool, "point": str}]}',
        "owcopilot.assist.contradiction:_JUDGE_SYSTEM",
    ),
    _spec(
        "assist.sweep.judge",
        "Theme-sweep judge: flag content that violates a named editorial/compliance theme.",
        "theme_sweep",
        "static",
        "JSON verdict per item",
        "owcopilot.assist.sweep:_JUDGE_SYSTEM_PROMPT",
    ),
    # -- extraction ---------------------------------------------------------------------------
    _spec(
        "extraction.service.system",
        "Extract structured lore objects from a free-form manuscript into the content schema.",
        "extract_lore",
        "static",
        "one JSON object (extracted entities/quests/...)",
        "owcopilot.extraction.service:_SYSTEM_PROMPT",
    ),
    _spec(
        "extraction.service.fill",
        "Fill missing fields of a partially-extracted object from the source text (second pass).",
        "extract_fill",
        "static",
        "one JSON object (field completions)",
        "owcopilot.extraction.service:_FILL_SYSTEM_PROMPT",
    ),
    _spec(
        "extraction.faithfulness.judge",
        "Judge whether an extracted claim is supported by the quoted source span (extraction's "
        "own entailment check).",
        "verify_faithfulness",
        "static",
        'JSON {"verdicts": [{"ref": str, "supported": bool}]}',
        "owcopilot.extraction.faithfulness:_JUDGE_SYSTEM",
    ),
    # -- logic --------------------------------------------------------------------------------
    _spec(
        "logic.draft.system",
        "Draft QuestLogic (state-machine stages/conditions) for a quest, using only declared "
        "state vars and real entity refs.",
        "quest_draft",
        "static",
        "one JSON object (QuestLogic)",
        "owcopilot.logic.draft:_SYSTEM",
        # _parse_logic() (logic/draft.py) validates the parsed JSON directly via
        # QuestLogic.model_validate(data).
        schema="owcopilot.content.models:QuestLogic",
    ),
    # -- QA / retrieval -----------------------------------------------------------------------
    _spec(
        "qa.answer.system",
        "Answer a lore question strictly from the retrieved context pack, citing refs, refusing "
        "when evidence is insufficient.",
        "qa_answer",
        "builder",
        "JSON {answer, citations, confidence, refused, ...}",
        "owcopilot.qa.service:_system_prompt",
        # parse_qa_answer() (qa/service.py) validates the parsed JSON directly via
        # QAAnswer.model_validate(payload).
        schema="owcopilot.qa.models:QAAnswer",
    ),
    _spec(
        "qa.expand.system",
        "Rewrite a lore question into retrieval-friendly variants (query expansion).",
        "qa_expand",
        "static",
        "JSON array of rephrasings",
        "owcopilot.qa.service:_EXPAND_SYSTEM",
    ),
    _spec(
        "qa.faithfulness.judge",
        "RAGAS-style entailment judge: does the retrieved evidence support this claim? "
        "(existence is not support; unparseable verdicts fail closed).",
        "faithfulness_judge",
        "static",
        'JSON {"supported": bool, "reason": str}',
        "owcopilot.qa.faithfulness:_JUDGE_SYSTEM",
    ),
    _spec(
        "qa.community.cluster",
        "Write a GraphRAG community report (title+summary) for one cluster from member/tie "
        "names only — no invention.",
        "community_report",
        "static",
        'JSON {"title": str, "summary": str}',
        "owcopilot.qa.community_index:_COMMUNITY_SYSTEM",
        # _complete_report() (qa/community_index.py) validates the parsed JSON directly via
        # _ReportShape.model_validate(payload) before folding title/summary into CommunityReport.
        schema="owcopilot.qa.community_index:_ReportShape",
    ),
    _spec(
        "qa.community.global",
        "Write the global macro-overview report from the per-cluster reports.",
        "community_report",
        "static",
        'JSON {"title": str, "summary": str}',
        "owcopilot.qa.community_index:_GLOBAL_SYSTEM",
        # Same _complete_report() -> _ReportShape.model_validate() path as qa.community.cluster.
        schema="owcopilot.qa.community_index:_ReportShape",
    ),
    PromptSpec(
        name="retrieval.agentic.followup",
        purpose=(
            "Agentic-RAG follow-up rewriter: given the question and evidence so far, write 1-3 "
            "short queries aimed at the evidence gap (bridging with entity NAMES already seen, "
            "never internal ids)."
        ),
        task="agentic_rag_followup",
        kind="static",
        output_contract="JSON array of query strings",
        source="owcopilot.retrieval.agentic:_FOLLOWUP_SYSTEM",
        version=2,
    ),
    _spec(
        "retrieval.agentic.sufficiency",
        "Agentic-RAG level-2 sufficiency judge (CRAG retrieval-evaluator / Self-RAG ISSUP): does "
        "the evidence CONTAIN the asked-for fact (support), not merely mention the same entities "
        "(existence)? On false, name the still-missing fact to steer the corrective round.",
        "agentic_rag_sufficiency",
        "static",
        'JSON {"sufficient": bool, "missing": str}',
        "owcopilot.retrieval.agentic:_SUFFICIENCY_SYSTEM",
        # _llm_sufficiency() (retrieval/agentic.py) calls complete_structured(..., model_cls=
        # SufficiencyVerdict) — the gateway's structured-output layer validates directly against it.
        schema="owcopilot.retrieval.agentic:SufficiencyVerdict",
    ),
    # -- recognize ----------------------------------------------------------------------------
    _spec(
        "recognize.relations.system",
        "Recognise typed relations between known entities mentioned in a text span.",
        "recognize_relations",
        "static",
        "JSON array of {source, target, kind}",
        "owcopilot.recognize.llm_relations:_RELATION_SYSTEM",
    ),
    # -- worldgen (staged generation) ---------------------------------------------------------
    _spec(
        "worldgen.stages.role",
        "Shared role prefix for every staged-generation call (original-world designer, "
        "brief-driven, one JSON object per stage); identical across the chain so provider "
        "prefix caching amortises it.",
        "world_seed",
        "fragment",
        "(prefix; stage suffix names the JSON keys)",
        "owcopilot.worldgen.service:_ROLE",
    ),
    _spec(
        "worldgen.stages.suffixes",
        "Per-stage suffixes (premise/factions/regions/entities/quests/...) naming that stage's "
        "JSON keys and appending 'world so far' grounding lines.",
        "world_seed",
        "fragment",
        "one JSON object per stage (keys per suffix)",
        "owcopilot.worldgen.service:_STAGE_SUFFIX",
    ),
    _spec(
        "worldgen.stages.retry.spine",
        "Retry suffix when the quest spine is missing required beats.",
        "world_seed",
        "fragment",
        "raw JSON object (spine complete)",
        "owcopilot.worldgen.service:_SPINE_REQUIRED_RETRY",
    ),
    _spec(
        "worldgen.tot.premise_eval",
        "Tree-of-Thoughts value function (LLM variant): score a premise candidate for "
        "originality/coherence/expandability.",
        "world_seed",
        "static",
        'JSON {"score": number, "reason": str}',
        "owcopilot.worldgen.tot:_PREMISE_EVAL_SYSTEM",
    ),
    # -- multi-agent --------------------------------------------------------------------------
    _spec(
        "multi_agent.decompose.system",
        "Orchestrator goal decomposition: JSON subtask array, roles diagnosis/repair_proposal, "
        "worker_id constrained to the registered routing table, plus a per-subtask max_steps "
        "effort budget (Anthropic effort scaling: complexity → step/tool-call budget, "
        "clamped [2, 12]).",
        "orchestrator_decompose",
        "builder",
        "JSON array of subtasks",
        "owcopilot.multi_agent.orchestrator:OrchestratorAgent._decompose_goal",
    ),
)


def prompt_registry() -> dict[str, PromptSpec]:
    """Name-addressed view of :data:`PROMPT_SPECS` (names are unique by construction/test)."""
    return {spec.name: spec for spec in PROMPT_SPECS}


__all__ = ["PROMPT_SPECS", "PromptSpec", "prompt_registry"]
