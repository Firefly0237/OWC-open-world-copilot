"""World seed generation service.

The world is no longer built in one big ``gateway.complete`` call. That single shot produced a
shallow, internally-incoherent world: factions, cast and quests were imagined in the same breath
and rarely cross-referenced each other. :meth:`WorldSeedService.generate` now runs a *grounded
chain* — premise → factions → regions → cast → quests — where each stage is a focused call that
reads the prior stages' output and references their ids. The stages assemble the SAME merged
payload the old single shot produced, so the deterministic normalisation (``_bundle_from_payload``)
and the human-review write path downstream are untouched.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from ..content.lang import detect_language, language_directive
from ..content.models import (
    POI,
    ContentBundle,
    Entity,
    EntityType,
    Origin,
    Quest,
    QuestStage,
    RegionBrief,
    Relation,
    ReviewStatus,
    StyleGuide,
    Term,
)
from ..inspiration.retrieval import ReferenceContextBuilder
from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json_object
from ..llm.spotlight import spotlight_references
from ..retrieval.context_pack import ContextPackBuilder
from ..retrieval.models import ContextPack, RetrievalHit
from ..util import unique_id
from . import stages
from .critic import QuestRefineOutcome, WorldQuestCritic, run_quest_refine_loop
from .models import ReferenceReportItem, WorldRefineRound, WorldSeedBrief, WorldSeedDraft


class WorldSeedService:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        bundle: ContentBundle,
        project_context_builder: ContextPackBuilder,
        reference_context_builder: ReferenceContextBuilder,
        critic: WorldQuestCritic | None = None,
        max_refine_rounds: int = 0,
    ) -> None:
        self.gateway = gateway
        self.bundle = bundle
        self.project_context_builder = project_context_builder
        self.reference_context_builder = reference_context_builder
        # The quests-stage refine loop is opt-in: without a critic the chain is the plain
        # staged generation (deterministic audit + human review still downstream either way).
        self.critic = critic
        self.max_refine_rounds = max_refine_rounds if critic is not None else 0

    def generate(
        self,
        brief: WorldSeedBrief,
        *,
        budget_tokens: int = 1800,
        progress: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> WorldSeedDraft:
        def emit(name: str) -> None:
            if progress is not None:
                progress("stage", {"name": name})

        query = _brief_query(brief)
        emit("retrieving")
        project_pack = (
            self.project_context_builder.build(query, budget_tokens=budget_tokens // 2, limit=6)
            if brief.use_project_facts
            else ContextPack(query=query, budget_tokens=budget_tokens // 2)
        )
        reference_query = brief.reference_query.strip() or query
        inspiration_pack = (
            self.reference_context_builder.build(
                reference_query, budget_tokens=budget_tokens, limit=8
            )
            if brief.use_references
            else ContextPack(query=reference_query, budget_tokens=budget_tokens)
        )

        # Each stage shares this prefix (role + retrieved context + the overall section plan) so
        # DeepSeek's server-side prefix cache amortises it across the chain; only the stage-specific
        # suffix and the grounding lines for "the world so far" change per call.
        prefix = _common_prefix(project_pack, inspiration_pack, brief)
        base_user = _brief_user_message(brief)
        payload: dict[str, Any] = {}
        relations: list[Any] = []
        reference_rows: list[Any] = []

        # --- stage 1: premise + dramatic spine (the North Star every later stage grounds in) ---
        emit(stages.PREMISE)
        premise_system = prefix + _premise_suffix(brief)
        premise = self._stage(stages.PREMISE, premise_system, base_user)
        if not _has_spine(premise):
            # The spine is the whole point of this stage — a premise with no central conflict makes
            # the downstream chain drift. Don't silently fall back to a summary-only world: ask once
            # more, explicitly, for the missing backbone (the model usually supplies it on retry).
            premise = self._stage(stages.PREMISE, premise_system + _SPINE_REQUIRED_RETRY, base_user)
        payload["summary"] = str(premise.get("summary") or brief.idea)
        # Fold the spine into the persisted style guide so the dramatic architecture is visible to
        # the planner (lorebook / style guide) and survives review, then feed it as grounding so
        # factions/places/cast/quests all serve one coherent conflict instead of drifting apart.
        payload["style_guide"] = _style_guide_with_spine(_dict(premise.get("style_guide")), premise)
        if brief.term_count > 0:
            payload["terms"] = _list(premise.get("terms"))
        relations += _list(premise.get("relations"))
        reference_rows += _list(premise.get("reference_report"))
        world_lines = _spine_lines(payload["summary"], premise)

        # --- stage 2: factions, grounded in the premise ---
        if brief.faction_count > 0:
            emit(stages.FACTIONS)
            factions = self._stage(
                stages.FACTIONS, prefix + _factions_suffix(brief, world_lines), base_user
            )
            payload["factions"] = _list(factions.get("factions"))
            relations += _list(factions.get("relations"))
            reference_rows += _list(factions.get("reference_report"))
            world_lines += _faction_context_lines(payload["factions"])
        else:
            payload["factions"] = []

        # --- stage 3: regions + locations, grounded in premise + factions ---
        if brief.region_count > 0:
            emit(stages.REGIONS)
            regions = self._stage(
                stages.REGIONS, prefix + _regions_suffix(brief, world_lines), base_user
            )
            payload["regions"] = _list(regions.get("regions"))
            payload["locations"] = _list(regions.get("locations"))
            relations += _list(regions.get("relations"))
            reference_rows += _list(regions.get("reference_report"))
            world_lines += _place_context_lines(payload["regions"], payload["locations"])
        else:
            payload["regions"] = []
            payload["locations"] = []

        # --- stage 4: cast, grounded in factions + places (deepens creator key characters) ---
        if brief.npc_count > 0:
            emit(stages.CAST)
            cast = self._stage(stages.CAST, prefix + _cast_suffix(brief, world_lines), base_user)
            payload["npcs"] = _list(cast.get("npcs"))
            relations += _list(cast.get("relations"))
            reference_rows += _list(cast.get("reference_report"))
            world_lines += _cast_context_lines(payload["npcs"])
        else:
            payload["npcs"] = []

        # --- stage 5: quests, grounded in cast + places (+ optional critique→refine loop) ---
        trail: list[WorldRefineRound] = []
        auto_review_incomplete = False
        if brief.quest_count > 0:
            outcome = self._quests_stage(brief, prefix, base_user, payload, world_lines, emit)
            payload["quests"] = outcome.quests
            relations += outcome.relations
            reference_rows += outcome.reference_rows
            trail = outcome.trail
            auto_review_incomplete = outcome.auto_review_incomplete
        else:
            payload["quests"] = []

        payload["relations"] = relations
        payload["reference_report"] = reference_rows

        emit("assembling")
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        draft_id = (
            "world_seed_" + hashlib.sha256(f"{brief.idea}\n{serialized}".encode()).hexdigest()[:12]
        )
        bundle = _bundle_from_payload(
            payload,
            draft_id=draft_id,
            brief=brief,
            existing=self.bundle,
            inspiration_pack=inspiration_pack,
            project_pack=project_pack,
        )
        report = _reference_report(payload, inspiration_pack, brief.reference_mode)
        return WorldSeedDraft(
            id=draft_id,
            brief=brief,
            summary=str(payload.get("summary") or brief.idea),
            bundle=bundle,
            reference_report=report,
            project_context_refs=project_pack.refs,
            inspiration_context_refs=inspiration_pack.refs,
            refine_trail=trail,
            auto_review_incomplete=auto_review_incomplete,
        )

    def revise(
        self, prior_bundle: ContentBundle, brief: WorldSeedBrief, feedback: str, *, draft_id: str
    ) -> tuple[ContentBundle, str]:
        """Targeted-stage revision: re-run only the stage the feedback is about, grounded in the
        rest of the world, then re-assemble. This keeps the staged quality (a single-pass world
        revise would reintroduce the single-shot weakness staged generation was built to fix)."""
        payload = _payload_from_bundle(prior_bundle)
        stage = _classify_revise_stage(feedback)
        empty = ContextPack(query="", budget_tokens=0)
        prefix = _common_prefix(empty, empty, brief)
        base_user = _brief_user_message(brief)
        world_lines = _revise_world_lines(payload, stage)
        suffix = _STAGE_SUFFIX[stage](brief, world_lines)
        directive = (
            f"\n\n[REVISE] 只重做本阶段以满足审阅意见，保持与世界其余部分（上方"
            f"「已确立」）的接地不变，沿用其中的既有 id：{feedback.strip()}"
        )
        result = self._stage(stage, prefix + suffix + directive, base_user)
        if stage is stages.FACTIONS:
            payload["factions"] = _list(result.get("factions"))
        elif stage is stages.REGIONS:
            payload["regions"] = _list(result.get("regions"))
            payload["locations"] = _list(result.get("locations"))
        elif stage is stages.CAST:
            payload["npcs"] = _list(result.get("npcs"))
        else:  # QUESTS
            payload["quests"] = _list(result.get("quests"))
        payload["relations"] = _list(payload.get("relations")) + _list(result.get("relations"))
        bundle = _bundle_from_payload(
            payload,
            draft_id=draft_id,
            brief=brief,
            existing=self.bundle,
            inspiration_pack=empty,
            project_pack=empty,
        )
        return bundle, stage

    def _stage(self, stage: str, system: str, user: str) -> dict[str, Any]:
        """One grounded stage call. Returns the parsed JSON slice the stage emitted.

        A stage that wraps its JSON in prose or truncates it would otherwise crash the whole world
        build, so we extract the object robustly and, on failure, re-ask ONCE demanding a bare
        object (root-cause fix for the common cause). If it still can't be parsed the error is
        surfaced — a half-built world is never silently returned."""
        raw = self.gateway.complete(task="world_seed", system=system, user=user)
        try:
            return parse_world_seed_payload(raw)
        except ValueError:
            retry = self.gateway.complete(
                task="world_seed", system=system + _JSON_ONLY_RETRY, user=user
            )
            return parse_world_seed_payload(retry)

    def _quests_stage(
        self,
        brief: WorldSeedBrief,
        prefix: str,
        base_user: str,
        payload: dict[str, Any],
        world_lines: list[str],
        emit: Callable[[str], None],
    ) -> QuestRefineOutcome:
        """Generate the quests stage, then (if a critic is wired) run the SHARED critique→refine
        loop genesis and expansion both use (``critic.run_quest_refine_loop``)."""
        emit(stages.QUESTS)
        system = prefix + _quests_suffix(brief, world_lines)
        result = self._stage(stages.QUESTS, system, base_user)
        quests = _list(result.get("quests"))
        relations = _list(result.get("relations"))
        reference_rows = _list(result.get("reference_report"))
        if self.critic is None:
            return QuestRefineOutcome(quests, relations, reference_rows, [], False)

        def regenerate(
            prior: list[Any], fixes: list[str]
        ) -> tuple[list[Any], list[Any], list[Any]]:
            r = self._stage(stages.QUESTS, system, _refine_user_message(base_user, prior, fixes))
            return (
                _list(r.get("quests")),
                _list(r.get("relations")),
                _list(r.get("reference_report")),
            )

        return run_quest_refine_loop(
            critic=self.critic,
            max_rounds=self.max_refine_rounds,
            quests=quests,
            relations=relations,
            reference_rows=reference_rows,
            npc_refs=_refs(payload.get("npcs")),
            place_refs=_refs(payload.get("locations")) | _refs(payload.get("regions")),
            context_lines=_cast_context_lines(_list(payload.get("npcs")))
            + _place_context_lines(_list(payload.get("regions")), _list(payload.get("locations"))),
            brief=brief.idea,
            regenerate=regenerate,
            emit=emit,
        )


_JSON_ONLY_RETRY = (
    "\n\nYour previous reply could not be parsed as JSON. Reply with ONLY the single JSON object "
    "described above — no prose, no markdown fences, and do not truncate it."
)


def parse_world_seed_payload(raw: str) -> dict[str, Any]:
    """Extract the stage's JSON object, tolerating markdown fences and surrounding prose.

    Raises ``ValueError`` when no usable object is found, so the caller can retry or surface the
    failure rather than crash mid-build."""
    return extract_json_object(raw)


_BRIEF_OPTIONAL_LABELS: list[tuple[str, str]] = [
    ("medium", "载体/媒介"),
    ("game_genre", "玩法/类型"),
    ("tone", "基调"),
    ("era", "时代/技术水平"),
    ("magic_level", "魔法/超自然体系"),
    ("world_scale", "世界尺度"),
    ("player_fantasy", "主角/玩家身份"),
    ("core_conflict", "核心冲突"),
    ("content_restrictions", "内容红线（必须避免）"),
    ("notes", "补充要求"),
]


def _brief_user_message(brief: WorldSeedBrief) -> str:
    """Compose the user message from the FILLED brief fields only.

    Never serialize the whole brief: an empty `"player_fantasy": ""` in the prompt reads
    as "this dimension exists, fill it" and the model invents a protagonist for a
    worldview-only request. Absent means absent.
    """
    lines = [f"核心想法：{brief.idea.strip()}"]
    if brief.world_styles:
        styles = "、".join(s.strip() for s in brief.world_styles if s.strip())
        if styles:
            lines.append(f"世界风格：{styles}")
    for field, label in _BRIEF_OPTIONAL_LABELS:
        value = str(getattr(brief, field) or "").strip()
        if value:
            lines.append(f"{label}：{value}")
    characters = [c.strip() for c in brief.key_characters if c.strip()]
    if characters:
        lines.append("主要人物（必须保留并深化，纳入 npcs，并在 relations 中设计他们之间的关系）：")
        lines.extend(f"- {character}" for character in characters)
    lines.append(
        "以上是创作者提供的全部设定。未提及的维度由你根据核心想法自行裁量，"
        "保持内在一致即可——不要为未提及的维度强加具体设定（例如未提及主角就不要设计主角）。"
    )
    return "\n".join(lines)


def _section_plan(brief: WorldSeedBrief) -> str:
    """Counts speak only for requested sections; a 0-count section is an explicit
    'return an empty array', not a smaller target."""
    wanted: list[str] = []
    skipped: list[str] = []
    for key, count in (
        ("factions", brief.faction_count),
        ("regions", brief.region_count),
        ("npcs", brief.npc_count),
        ("quests", brief.quest_count),
        ("terms", brief.term_count),
    ):
        if count > 0:
            wanted.append(f"{key}={count}")
        else:
            skipped.append(key)
    plan = "Target counts: " + (", ".join(wanted) if wanted else "(none)") + ". "
    if skipped:
        plan += (
            "The creator explicitly does NOT want these sections — return [] for: "
            + ", ".join(skipped)
            + ". "
        )
    if brief.region_count <= 0:
        plan += "Also return [] for locations. "
    return plan


# --- staged prompts -------------------------------------------------------------------------
#
# Each stage's system prompt is `_common_prefix(...) + <stage suffix>`. The prefix (role +
# retrieved context + overall plan) is identical across the chain so the provider's prefix cache
# amortises it; the suffix names this stage's JSON keys and appends the grounding lines for "the
# world so far". The leading stage marker (in each suffix) is inert for real models and is the
# dispatch key for the offline double — see worldgen/stages.py.

_ROLE = (
    "You are a senior worldbuilding and narrative designer building an ORIGINAL game world from a "
    "creator's brief, in the brief's own genre, medium and language — never assume a default genre "
    "or audience. You build it in grounded stages; at each stage you return ONE JSON object only, "
    "no markdown, no prose outside the JSON. Descriptions must be specific enough for a level "
    "designer to build from: concrete detail and a current tension or hook, no generic filler. "
    "Use uploaded references only as inspiration/structure per reference_mode — never as canonical "
    "lore facts, and avoid long verbatim reuse unless the brief asks for quotation. If project "
    "facts are provided, preserve them as higher-priority facts. Each reference_report item must "
    "include source_ref, source_title, used_for, transformation, excluded."
)


def _common_prefix(
    project_pack: ContextPack, inspiration_pack: ContextPack, brief: WorldSeedBrief
) -> str:
    project_lines = _context_lines(project_pack.hits)
    inspiration_lines = _context_lines(inspiration_pack.hits)
    # Detect the brief's language so the whole world is written in it (and mixed-language briefs
    # keep their proper nouns verbatim) — the same no-ask-the-user discipline as ingestion.
    directive = language_directive(detect_language(_brief_query(brief)))
    return (
        _ROLE
        + "\n\n"
        + directive
        + "\n\nOverall plan for the whole world (this stage produces only its own slice):\n"
        + _section_plan(brief)
        + "\n\nProject facts context:\n"
        + ("\n".join(project_lines) if project_lines else "(none)")
        # Untrusted, un-reviewed-per-chunk reference text is the OWASP LLM01 indirect-injection
        # vector — fence it as data so the model never obeys instructions hidden inside it.
        + "\n\nInspiration reference context:\n"
        + spotlight_references(inspiration_lines)
    )


def _stage_header(stage: str, ordinal: str, title: str) -> str:
    return f"{stages.stage_marker(stage)}\n\nSTAGE {ordinal} — {title}\n"


def _premise_suffix(brief: WorldSeedBrief) -> str:
    terms_line = (
        f"- terms: {brief.term_count} in-world vocabulary entries {{canonical, description (what "
        "the word concretely means in THIS world), aliases}}.\n"
        if brief.term_count > 0
        else "- terms: return [] (the creator did not request world terms).\n"
    )
    # Outline-first: before any faction/place/character exists, fix the dramatic spine that every
    # later stage must serve. This is the North Star that stops a staged world from drifting into a
    # flat, internally-disconnected list (the Plan-and-Write / Re3 / Dramatron lesson). The spine
    # fields are folded into the persisted style guide and fed as grounding to every later stage.
    return (
        _stage_header(stages.PREMISE, "1/5", "PREMISE & DRAMATIC SPINE")
        + "Design the world's DRAMATIC SPINE — the North Star every later stage (factions, places, "
        "cast, quests) must visibly serve. Return keys: summary, central_conflict, themes, "
        "dramatic_question, faction_axes, stakes, style_guide, terms, reference_report.\n"
        "- summary: 3-5 sentences naming the setting, its central tension, and the player's "
        "vantage — grounded in the brief.\n"
        "- central_conflict: 2-3 sentences — the ENGINE of the world: which forces want what, why "
        "they collide NOW, and what breaks if either side wins. Every faction, place, character "
        "and quest must visibly trace back to this.\n"
        "- themes: 2-3 thematic throughlines (short phrases) the world keeps interrogating.\n"
        "- dramatic_question: ONE sentence — the open question the world poses to the player.\n"
        "- faction_axes: 2-4 opposing pressures (each a short phrase naming TWO sides in tension); "
        "the FACTIONS stage will occupy these positions, so make them concrete and mutually "
        "opposed, not vague.\n"
        "- stakes: ONE sentence — what is changing or breaking RIGHT NOW that puts the world in "
        "motion (a world in motion, never a static museum).\n"
        "- style_guide: {body: voice/themes/do's-and-don'ts, rules: [concrete writing rules]}.\n"
        + terms_line
    )


def _factions_suffix(brief: WorldSeedBrief, world_lines: list[str]) -> str:
    return (
        _stage_header(stages.FACTIONS, "2/5", "FACTIONS")
        + f"Design exactly {brief.faction_count} factions that OCCUPY the conflict axes below. "
        "Each faction must take a clear position on at least one axis and hold a goal that "
        "DIRECTLY OPPOSES another faction's, so the central conflict is embodied in who they "
        "are — not a set of unrelated groups. Return keys: factions, relations, reference_report.\n"
        '- factions: each {id (e.g. "fac_iron"), name, description: what they control, what they '
        "want, WHO they oppose and why, and the pressure breaking on them right now}.\n"
        "- relations: faction↔faction ties among the ids you just defined {source, target, kind} "
        "(enemy_of / rival_of / allied_with / trades_with) — the web must show real push-and-pull, "
        "not all-allies.\n" + _grounding_block(world_lines)
    )


def _regions_suffix(brief: WorldSeedBrief, world_lines: list[str]) -> str:
    return (
        _stage_header(stages.REGIONS, "3/5", "REGIONS & LOCATIONS")
        + f"Design exactly {brief.region_count} regions and a handful of buildable locations, "
        "grounded in the premise and factions below. Return keys: regions, locations, "
        "reference_report.\n"
        '- regions: each {id ("region_*"), name, description, themes [], level_min, level_max}.\n'
        '- locations: each {id ("loc_*"), name, description, purpose, region_id (one of the '
        "region ids above), controlling_faction (one of the FACTION ids below), tags []}.\n"
        "Reference the faction ids EXACTLY as written below — do not rename or invent factions.\n"
        + _grounding_block(world_lines)
    )


def _cast_suffix(brief: WorldSeedBrief, world_lines: list[str]) -> str:
    key_cast = (
        "You MUST keep and deepen the creator's key characters (never replace them); weave them "
        "into the cast and into relations.\n"
        if any(c.strip() for c in brief.key_characters)
        else ""
    )
    return (
        _stage_header(stages.CAST, "4/5", "CAST")
        + f"Design exactly {brief.npc_count} NPCs grounded in the factions and places below. "
        "Every NPC must have a personal STAKE in the central conflict and a stance toward their "
        "faction's goal (loyal / torn / quietly defecting) — people caught inside the conflict, "
        "not bystanders. Return keys: npcs, relations, reference_report.\n"
        '- npcs: each {id ("npc_*"), name, description: role, what they want, their stake in the '
        "central conflict, and a current tension pulling them; faction_id (one of the faction ids "
        "below), location_id (one of the location ids below)}.\n"
        + key_cast
        + "- relations: ties among the npcs and to factions, using the ids below {source, target, "
        "kind}.\n"
        "Reference faction/location ids EXACTLY as written below.\n" + _grounding_block(world_lines)
    )


def _quests_suffix(brief: WorldSeedBrief, world_lines: list[str]) -> str:
    return (
        _stage_header(stages.QUESTS, "5/5", "QUESTS")
        + f"Design exactly {brief.quest_count} quests grounded in the cast and places below. Each "
        "quest must DRAMATIZE one facet of the central conflict and push the dramatic question "
        "forward — putting the player inside a real choice between the factions' tensions, not a "
        "generic fetch errand. Return keys: quests, relations, reference_report.\n"
        "- quests: each {title (a player-facing quest-log headline, never an id), objective (one "
        "concrete sentence: who wants what done and why it matters now), giver_npc (one of the "
        "NPC ids below), location (one of the LOCATION ids below), stages (2-4 stage summaries, "
        "each a CONCRETE SCENE naming where it happens and what the player does, referencing the "
        "cast/places below; build one escalating arc 铺垫->冲突->高潮/抉择, keeping the choice and "
        "outcomes INSIDE the climax stage, not as parallel ending stages), "
        "tags []}.\n"
        "Reference the cast and location ids EXACTLY as written below.\n"
        + _grounding_block(world_lines)
    )


def _grounding_block(world_lines: list[str]) -> str:
    return "\nAlready established (reference these EXACTLY, by id):\n" + "\n".join(world_lines)


_SPINE_REQUIRED_RETRY = (
    "\n\nYour previous reply omitted the dramatic spine. You MUST include a concrete "
    "central_conflict (2-3 sentences naming the opposing forces and what is at stake) and "
    "faction_axes (the opposing pressures the factions will embody). Return the full JSON again."
)


def _has_spine(premise: dict[str, Any]) -> bool:
    """The premise carries a usable dramatic spine when it names a central conflict or the axes the
    factions will occupy — without either, downstream stages have nothing to cohere around."""
    return bool(str(premise.get("central_conflict") or "").strip()) or bool(
        _list(premise.get("faction_axes"))
    )


def _spine_lines(summary: str, premise: dict[str, Any]) -> list[str]:
    """The dramatic spine fed as grounding to every later stage — the North Star that keeps the
    staged world coherent. Missing fields degrade gracefully (a real model may omit one)."""
    lines = [f"世界前提：{summary}"]
    conflict = str(premise.get("central_conflict") or "").strip()
    if conflict:
        lines.append(f"核心冲突（一切设定必须服务于此）：{conflict}")
    themes = "、".join(str(t).strip() for t in _list(premise.get("themes")) if str(t).strip())
    if themes:
        lines.append(f"主题：{themes}")
    question = str(premise.get("dramatic_question") or "").strip()
    if question:
        lines.append(f"核心戏剧问题：{question}")
    stakes = str(premise.get("stakes") or "").strip()
    if stakes:
        lines.append(f"当下变局（世界正在发生的事）：{stakes}")
    axes = "；".join(str(a).strip() for a in _list(premise.get("faction_axes")) if str(a).strip())
    if axes:
        lines.append(f"阵营对抗轴：{axes}")
    return lines


def _style_guide_with_spine(style_guide: dict[str, Any], premise: dict[str, Any]) -> dict[str, Any]:
    """Persist the dramatic spine inside the style guide body so the planner sees the conflict /
    themes / stakes the whole world was built to serve (no schema change — it rides in body)."""
    result = dict(style_guide)
    body = str(result.get("body") or "").strip()
    spine_parts: list[str] = []
    conflict = str(premise.get("central_conflict") or "").strip()
    if conflict:
        spine_parts.append(f"核心冲突：{conflict}")
    themes = "、".join(str(t).strip() for t in _list(premise.get("themes")) if str(t).strip())
    if themes:
        spine_parts.append(f"主题：{themes}")
    question = str(premise.get("dramatic_question") or "").strip()
    if question:
        spine_parts.append(f"核心戏剧问题：{question}")
    stakes = str(premise.get("stakes") or "").strip()
    if stakes:
        spine_parts.append(f"当下变局：{stakes}")
    if spine_parts:
        spine_block = "戏剧主轴\n" + "\n".join(spine_parts)
        result["body"] = f"{body}\n\n{spine_block}".strip() if body else spine_block
    return result


def _refine_user_message(base_user: str, prior_quests: list[Any], fixes: list[str]) -> str:
    """Quests-stage regeneration message: the brief + the prior quest batch + the fixes to apply.
    Mirrors ``assist.drafts._draft_user_message``; the offline double keys on the ``[REFINE]``
    marker to return a grounded, deepened batch."""
    prior_json = json.dumps(prior_quests, ensure_ascii=False)
    fix_lines = "\n".join(f"- {fix}" for fix in fixes)
    return (
        f"{base_user}\n\n"
        "[REFINE] 这是上一版任务批次。请产出改进后的完整 quests JSON：保留可用部分，逐条解决下列"
        "问题，把每个任务接地到已确立的角色与地点（用其 id），让任务更具体、可量产。\n"
        f"上一版任务：\n{prior_json}\n\n"
        f"必须解决的问题：\n{fix_lines}"
    )


def _faction_context_lines(factions: list[Any]) -> list[str]:
    lines: list[str] = []
    for raw in factions:
        item = _dict(raw)
        ident = str(item.get("id") or item.get("name") or "").strip()
        if not ident:
            continue
        name = str(item.get("name") or ident)
        lines.append(f"- 阵营 {ident}（{name}）：{str(item.get('description') or '')}".strip())
    return lines


def _place_context_lines(regions: list[Any], locations: list[Any]) -> list[str]:
    lines: list[str] = []
    for raw in regions:
        item = _dict(raw)
        ident = str(item.get("id") or item.get("name") or "").strip()
        if not ident:
            continue
        name = str(item.get("name") or ident)
        themes = "、".join(str(theme) for theme in _list(item.get("themes")))
        lines.append(f"- 区域 {ident}（{name}）：{themes}".strip())
    for raw in locations:
        item = _dict(raw)
        ident = str(item.get("id") or item.get("name") or "").strip()
        if not ident:
            continue
        name = str(item.get("name") or ident)
        region = str(item.get("region_id") or "")
        faction = str(item.get("controlling_faction") or "")
        purpose = str(item.get("purpose") or item.get("description") or "")
        lines.append(
            f"- 地点 {ident}（{name}，区域 {region}，控制方 {faction}）：{purpose}".strip()
        )
    return lines


def _cast_context_lines(npcs: list[Any]) -> list[str]:
    lines: list[str] = []
    for raw in npcs:
        item = _dict(raw)
        ident = str(item.get("id") or item.get("name") or "").strip()
        if not ident:
            continue
        name = str(item.get("name") or ident)
        faction = str(item.get("faction_id") or "")
        location = str(item.get("location_id") or "")
        desc = str(item.get("description") or "")
        lines.append(f"- {ident}（{name}，阵营 {faction}，所在 {location}）：{desc}".strip())
    return lines


def _refs(items: Any) -> set[str]:
    """Both the ids and the display names earlier stages emitted — a downstream stage may reference
    an upstream entity by either form, and `_bundle_from_payload`'s id_map resolves either."""
    out: set[str] = set()
    for raw in _list(items):
        item = _dict(raw)
        for key in ("id", "name"):
            value = str(item.get(key) or "").strip()
            if value:
                out.add(value)
    return out


def _context_lines(hits: list[RetrievalHit]) -> list[str]:
    return [f"- [{hit.ref}] {hit.title}: {hit.body}".strip() for hit in hits]


def _brief_query(brief: WorldSeedBrief) -> str:
    parts = [
        brief.idea,
        " ".join(brief.world_styles),
        brief.tone,
        brief.era,
        brief.game_genre,
        brief.player_fantasy,
        brief.core_conflict,
        brief.notes,
    ]
    return " ".join(part for part in parts if part.strip())


_STAGE_SUFFIX = {
    stages.FACTIONS: _factions_suffix,
    stages.REGIONS: _regions_suffix,
    stages.CAST: _cast_suffix,
    stages.QUESTS: _quests_suffix,
}

# Keyword → stage. The reviewer usually names the aspect ("阵营更对立", "第三个任务太平淡"); on no
# match we revise quests, the most common revision target.
_STAGE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    (stages.FACTIONS, ("阵营", "势力", "派系", "组织", "faction")),
    (stages.REGIONS, ("区域", "地点", "地区", "地图", "场景", "region", "location")),
    (stages.CAST, ("角色", "人物", "npc", "character", "卡司", "主角", "配角")),
    (stages.QUESTS, ("任务", "支线", "主线", "剧情", "quest")),
]


def _classify_revise_stage(feedback: str) -> str:
    low = feedback.lower()
    for stage, keywords in _STAGE_KEYWORDS:
        if any(keyword.lower() in low for keyword in keywords):
            return stage
    return stages.QUESTS


def _payload_from_bundle(bundle: ContentBundle) -> dict[str, Any]:
    """Convert an assembled world bundle back into the stage-payload shape the chain consumes, so a
    single revised stage can be merged and the whole thing re-normalised by _bundle_from_payload."""

    def entity_item(entity: Entity) -> dict[str, Any]:
        return {
            "id": entity.id,
            "name": entity.name,
            "description": entity.description,
            "aliases": list(entity.aliases),
            "tags": list(entity.tags),
        }

    style = bundle.style_guides.get("style_guide")
    return {
        "summary": style.body if style else "",
        "style_guide": {"body": style.body, "rules": list(style.rules)} if style else {},
        "terms": [term.model_dump(mode="json") for term in bundle.terms.values()],
        "factions": [
            entity_item(e) for e in bundle.entities.values() if e.type is EntityType.FACTION
        ],
        "npcs": [entity_item(e) for e in bundle.entities.values() if e.type is EntityType.NPC],
        "regions": [region.model_dump(mode="json") for region in bundle.regions.values()],
        "locations": [poi.model_dump(mode="json") for poi in bundle.pois.values()],
        "quests": [q.model_dump(mode="json", exclude_none=True) for q in bundle.quests.values()],
        "relations": [
            {"source": r.source, "target": r.target, "kind": r.kind} for r in bundle.relations
        ],
    }


def _revise_world_lines(payload: dict[str, Any], stage: str) -> list[str]:
    """Ground the revised stage in everything that precedes it, as initial generation does."""
    lines = [f"世界主轴：{payload.get('summary', '')}"]
    if stage in (stages.REGIONS, stages.CAST, stages.QUESTS):
        lines += _faction_context_lines(_list(payload.get("factions")))
    if stage in (stages.CAST, stages.QUESTS):
        lines += _place_context_lines(
            _list(payload.get("regions")), _list(payload.get("locations"))
        )
    if stage is stages.QUESTS:
        lines += _cast_context_lines(_list(payload.get("npcs")))
    return lines


def _bundle_from_payload(
    payload: dict[str, Any],
    *,
    draft_id: str,
    brief: WorldSeedBrief,
    existing: ContentBundle,
    inspiration_pack: ContextPack,
    project_pack: ContextPack,
) -> ContentBundle:
    bundle = ContentBundle()
    id_map: dict[str, str] = {}
    used_entities = set(existing.entities)
    used_regions = set(existing.regions)
    used_pois = set(existing.pois)
    used_quests = set(existing.quests)
    used_terms = set(existing.terms)
    refs = ",".join(inspiration_pack.refs[:8])
    common_meta: dict[str, Any] = {
        "world_seed_id": draft_id,
        "brief": brief.idea,
        "reference_mode": brief.reference_mode,
        "inspiration_refs": refs,
        "project_context_refs": ",".join(project_pack.refs[:8]),
    }

    style = _dict(payload.get("style_guide"))
    body = str(style.get("body") or payload.get("summary") or brief.idea)
    bundle.style_guides["style_guide"] = StyleGuide(
        body=body,
        rules=[str(item) for item in _list(style.get("rules"))],
        origin=Origin.AI_DRAFT,
        review_status=ReviewStatus.PENDING_REVIEW,
    )

    for item in _ensure_count(_list(payload.get("factions")), brief.faction_count, "阵营"):
        raw = _dict(item)
        entity = _entity_from_item(
            raw,
            entity_type=EntityType.FACTION,
            prefix="fac",
            used=used_entities,
            metadata=common_meta,
        )
        _remember(id_map, raw, entity.id)
        bundle.entities[entity.id] = entity

    for item in _ensure_count(_list(payload.get("regions")), brief.region_count, "区域"):
        raw = _dict(item)
        region_id = _unique_id(
            "region",
            str(raw.get("id") or raw.get("name") or "region"),
            used_regions,
        )
        _remember(id_map, raw, region_id)
        bundle.regions[region_id] = RegionBrief(
            id=region_id,
            name=str(raw.get("name") or region_id),
            level_min=_int(raw.get("level_min"), 1),
            level_max=_int(raw.get("level_max"), 20),
            themes=[str(item) for item in _list(raw.get("themes"))],
            allowed_content=[str(item) for item in _list(raw.get("allowed_content"))],
            banned_content=[str(item) for item in _list(raw.get("banned_content"))],
            metadata=common_meta,
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
        )

    locations = _ensure_count(
        _list(payload.get("locations")),
        max(brief.region_count + 2, 3) if brief.region_count > 0 else 0,
        "地点",
    )
    region_ids = list(bundle.regions) or list(existing.regions)
    faction_ids = _ids_by_type(bundle, EntityType.FACTION) or _ids_by_type(
        existing, EntityType.FACTION
    )
    used_locations = used_pois | used_entities
    for index, item in enumerate(locations):
        raw = _dict(item)
        loc_id = _unique_id(
            "loc",
            str(raw.get("id") or raw.get("name") or "location"),
            used_locations,
        )
        used_pois.add(loc_id)
        used_entities.add(loc_id)
        _remember(id_map, raw, loc_id)
        poi_region_id = _known_or(
            _resolve(raw.get("region_id"), id_map), region_ids, _round_robin(region_ids, index)
        )
        faction_id = _known_or(
            _resolve(raw.get("controlling_faction"), id_map),
            faction_ids,
            _round_robin(faction_ids, index),
        )
        # No preset filler text: a missing description stays minimal and theme-neutral.
        description = str(raw.get("description") or raw.get("purpose") or raw.get("name") or "")
        bundle.entities[loc_id] = Entity(
            id=loc_id,
            name=str(raw.get("name") or loc_id),
            type=EntityType.LOCATION,
            description=description,
            tags=[str(item) for item in _list(raw.get("tags"))],
            metadata=common_meta,
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
        )
        bundle.pois[loc_id] = POI(
            id=loc_id,
            name=str(raw.get("name") or loc_id),
            region_id=poi_region_id,
            purpose=str(raw.get("purpose") or description),
            controlling_faction=faction_id,
            level_min=_int(raw.get("level_min"), None),
            level_max=_int(raw.get("level_max"), None),
            tags=[str(item) for item in _list(raw.get("tags"))],
            metadata=common_meta,
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
        )
        if faction_id:
            bundle.relations.append(_relation(loc_id, faction_id, "controlled_by", common_meta))

    location_ids = list(bundle.pois) or list(existing.pois)
    for index, item in enumerate(_ensure_count(_list(payload.get("npcs")), brief.npc_count, "NPC")):
        raw = _dict(item)
        entity = _entity_from_item(
            raw,
            entity_type=EntityType.NPC,
            prefix="npc",
            used=used_entities,
            metadata=common_meta,
        )
        _remember(id_map, raw, entity.id)
        bundle.entities[entity.id] = entity
        faction_id = _known_or(
            _resolve(raw.get("faction_id"), id_map),
            faction_ids,
            _round_robin(faction_ids, index),
        )
        location_id = _known_or(
            _resolve(raw.get("location_id"), id_map),
            location_ids,
            _round_robin(location_ids, index),
        )
        if faction_id:
            bundle.relations.append(_relation(entity.id, faction_id, "member_of", common_meta))
        if location_id:
            bundle.relations.append(_relation(entity.id, location_id, "located_in", common_meta))

    npc_ids = _ids_by_type(bundle, EntityType.NPC) or _ids_by_type(existing, EntityType.NPC)
    for index, item in enumerate(
        _ensure_count(_list(payload.get("quests")), brief.quest_count, "任务")
    ):
        raw = _dict(item)
        quest_id = _unique_id(
            "quest",
            str(raw.get("id") or raw.get("title") or "quest"),
            used_quests,
        )
        _remember(id_map, raw, quest_id)
        quest_location = _known_or(
            _resolve(raw.get("location"), id_map),
            location_ids,
            _round_robin(location_ids, index),
        )
        quest_giver = _known_or(
            _resolve(raw.get("giver_npc"), id_map),
            npc_ids,
            _round_robin(npc_ids, index),
        )
        # Models that mirror the name/description shape of other sections still parse:
        # quests accept name→title and description→objective as fallbacks.
        title = str(raw.get("title") or raw.get("name") or quest_id)
        objective = str(raw.get("objective") or raw.get("description") or title)
        # When the model omits stages, derive ONE stage from the quest's own objective —
        # never inject preset beats (确认线索/作出选择 was steering every fallback quest
        # toward the same investigation-shaped arc).
        raw_stages = _list(raw.get("stages")) or [objective]
        stages = [
            QuestStage(
                id=f"{quest_id}_stage_{stage_index + 1}",
                summary=(
                    str(
                        _dict(stage).get("summary")
                        or _dict(stage).get("description")
                        or _dict(stage).get("name")
                        or stage
                    )
                    if isinstance(stage, dict)
                    else str(stage)
                ),
                location=quest_location,
            )
            for stage_index, stage in enumerate(raw_stages)
        ]
        bundle.quests[quest_id] = Quest(
            id=quest_id,
            title=title,
            giver_npc=quest_giver,
            location=quest_location,
            objective=objective,
            timeline_order=index + 1,
            stages=stages,
            localization_keys=[f"quest.{quest_id}.objective"],
            tags=[str(item) for item in _list(raw.get("tags"))],
            metadata=common_meta,
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
        )

    for item in _ensure_count(_list(payload.get("terms")), brief.term_count, "术语"):
        raw = _dict(item)
        term_id = _unique_id(
            "term",
            str(raw.get("id") or raw.get("canonical") or "term"),
            used_terms,
        )
        _remember(id_map, raw, term_id)
        bundle.terms[term_id] = Term(
            id=term_id,
            canonical=str(raw.get("canonical") or raw.get("name") or term_id),
            aliases=[str(item) for item in _list(raw.get("aliases"))],
            forbidden=[str(item) for item in _list(raw.get("forbidden"))],
            description=str(raw.get("description") or raw.get("definition") or ""),
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
        )

    for item in _list(payload.get("relations")):
        raw = _dict(item)
        source = _resolve(raw.get("source"), id_map)
        target = _resolve(raw.get("target"), id_map)
        kind = str(raw.get("kind") or "").strip()
        known_relation_ids = set(bundle.entities) | set(bundle.pois) | set(bundle.regions)
        if source in known_relation_ids and target in known_relation_ids and kind:
            bundle.relations.append(_relation(source, target, kind, common_meta))
    bundle.relations = _dedupe_relations(bundle.relations)
    return bundle


def _reference_report(
    payload: dict[str, Any],
    inspiration_pack: ContextPack,
    reference_mode: str,
) -> list[ReferenceReportItem]:
    rows: list[ReferenceReportItem] = []
    by_ref = {hit.ref: hit for hit in inspiration_pack.hits}
    for item in _list(payload.get("reference_report")):
        raw = _dict(item)
        source_ref = str(raw.get("source_ref") or "")
        hit = by_ref.get(source_ref)
        if not source_ref or hit is None:
            continue
        rows.append(
            ReferenceReportItem(
                source_ref=source_ref,
                source_title=str(
                    raw.get("source_title") or hit.metadata.get("source_title") or hit.title
                ),
                used_for=str(raw.get("used_for") or reference_mode),
                transformation=str(raw.get("transformation") or "转化为新的世界设定元素。"),
                excluded=[str(entry) for entry in _list(raw.get("excluded"))],
            )
        )
    if rows:
        return rows
    return [
        ReferenceReportItem(
            source_ref=hit.ref,
            source_title=hit.metadata.get("source_title") or hit.title,
            used_for=f"{reference_mode}：主题、人物关系或任务节奏参考",
            transformation="转化为新的阵营关系、地点功能和任务目标；参考资料不进入正式设定事实库。",
            excluded=["未复用参考材料中的专有名词", "未复用长段原文"],
        )
        for hit in inspiration_pack.hits[:5]
    ]


def _entity_from_item(
    raw: dict[str, Any],
    *,
    entity_type: EntityType,
    prefix: str,
    used: set[str],
    metadata: dict[str, Any],
) -> Entity:
    entity_id = _unique_id(prefix, str(raw.get("id") or raw.get("name") or entity_type.value), used)
    return Entity(
        id=entity_id,
        name=str(raw.get("name") or entity_id),
        type=entity_type,
        description=str(raw.get("description") or ""),
        aliases=[str(item) for item in _list(raw.get("aliases"))],
        tags=[str(item) for item in _list(raw.get("tags"))],
        metadata=metadata,
        origin=Origin.AI_DRAFT,
        review_status=ReviewStatus.PENDING_REVIEW,
    )


def _relation(source: str, target: str, kind: str, metadata: dict[str, Any]) -> Relation:
    return Relation(
        source=source,
        target=target,
        kind=kind,
        metadata=metadata,
        origin=Origin.AI_DRAFT,
        review_status=ReviewStatus.PENDING_REVIEW,
    )


def _dedupe_relations(relations: list[Relation]) -> list[Relation]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[Relation] = []
    for relation in relations:
        key = (relation.source, relation.kind, relation.target)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(relation)
    return deduped


def _remember(id_map: dict[str, str], raw: dict[str, Any], canonical_id: str) -> None:
    for value in (raw.get("id"), raw.get("name"), raw.get("title"), raw.get("canonical")):
        if value:
            id_map[str(value)] = canonical_id
    id_map[canonical_id] = canonical_id


def _resolve(value: Any, id_map: dict[str, str]) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    return id_map.get(raw) or raw or None


def _unique_id(prefix: str, raw: str, used: set[str]) -> str:
    return unique_id(prefix, raw, used)


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _ensure_count(items: list[Any], count: int, label: str) -> list[Any]:
    result = list(items)
    while len(result) < count:
        index = len(result) + 1
        result.append(
            {
                "name": f"{label}{index}",
                "description": f"由一句话世界种子补全的{label}。",
            }
        )
    return result[:count]


def _int(value: Any, default: int | None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ids_by_type(bundle: ContentBundle, entity_type: EntityType) -> list[str]:
    return [entity.id for entity in bundle.entities.values() if entity.type is entity_type]


def _round_robin(values: list[str], index: int) -> str | None:
    if not values:
        return None
    return values[index % len(values)]


def _known_or(value: str | None, known_values: list[str], fallback: str | None) -> str | None:
    return value if value in set(known_values) else fallback
