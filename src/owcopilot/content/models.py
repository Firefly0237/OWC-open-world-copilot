# v2 内容中枢的统一数据模型（Entity/Quest/Relation… 全项目真源结构）。
"""Unified content models for the v2 content hub.

These models are the file-backed facts that later pipeline stages index, audit, retrieve, patch,
review and export.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_serializer


class Origin(str, Enum):
    HUMAN = "human"
    AI_DRAFT = "ai_draft"
    AI_PATCH = "ai_patch"


class ReviewStatus(str, Enum):
    APPROVED = "approved"
    PENDING_REVIEW = "pending_review"
    REJECTED = "rejected"


class EntityType(str, Enum):
    NPC = "npc"
    LOCATION = "location"
    FACTION = "faction"
    ITEM = "item"
    EVENT = "event"
    REGION = "region"
    ORGANIZATION = "organization"
    CONCEPT = "concept"
    TERM = "term"
    SKILL = "skill"
    ACHIEVEMENT = "achievement"


class SourceRef(BaseModel):
    """Where a content object came from before normalization."""

    path: str
    line: int | None = None
    sheet: str | None = None
    row: int | None = None
    column: str | None = None


class ProvenanceMixin(BaseModel):
    origin: Origin = Origin.HUMAN
    source_ref: SourceRef | None = None
    review_status: ReviewStatus = ReviewStatus.APPROVED
    # The (world_id, version) scope is a *storage / sharding* dimension, NOT a
    # content field. It is deliberately absent from the content models, so content files and their
    # serialization (model_dump / content_hash / snapshot payloads / ContentStore.save round-trip)
    # stay byte-for-byte identical to the pre-scope baseline. Sharding is applied at the SQLite
    # layer: the store stamps its current scope (set by ProjectContext) onto each row on write, so
    # no object ever carries world_id/version. (Entity keeps its own pre-existing free-text
    # ``version`` field, which is content, not scope.)


class Entity(ProvenanceMixin):
    id: str
    name: str
    type: EntityType
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    status: str = "active"
    version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Relation(ProvenanceMixin):
    source: str
    target: str
    kind: str
    valid_from: int | None = None
    valid_until: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def relation_sort_key(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    """关系的规范排序键：`(source, kind, target)` 三元组 + 整条 payload 的规范 JSON 作次级键。

    Canonical ordering key for a *serialized* relation.

    ``relations`` is stored as a ``list`` but its semantics are a **set**: every dedup / removal /
    tombstone site in the codebase keys on the ``(source, kind, target)`` triple and nothing else
    (``store._relation_tombstone``, ``worldgen._dedupe_relations``, ``normalize._derive_relations``,
    ``pipeline.review``, ``content_editing.remove_relation``). Position carries no meaning —
    time-boxing is carried explicitly by ``valid_from`` / ``valid_until``, not by list order.

    The triple is therefore the primary key. It is NOT unique on its own (two relations may share a
    triple and differ in ``metadata`` / provenance, and DUPLICATE_RELATION is a real audit rule), so
    the whole payload's canonical JSON is appended as a total, deterministic tiebreaker — otherwise
    same-triple entries would sort unstably and reintroduce the very drift this key exists to kill.
    ``default=str`` keeps the tiebreaker total for ``mode="python"`` dumps, where nested values are
    still model objects; it only ever orders entries, never enters a hash or a file.
    """
    return (
        str(payload.get("source", "")),
        str(payload.get("kind", "")),
        str(payload.get("target", "")),
        json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str),
    )


class QuestEventRefKind(str, Enum):
    MENTIONS_EVENT = "mentions_event"
    REFERENCES_RESULT = "references_result"


class QuestEventReference(ProvenanceMixin):
    """A quest-to-event reference with semantic intent.

    `mentions_event` means the quest may discuss or investigate an event. `references_result`
    means the quest depends on the event outcome, which is timeline-sensitive.
    """

    id: str
    quest_id: str
    event_id: str
    ref_kind: QuestEventRefKind = QuestEventRefKind.MENTIONS_EVENT
    note: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class QuestStage(BaseModel):
    id: str
    summary: str
    location: str | None = None
    required_entities: list[str] = Field(default_factory=list)


class Reward(BaseModel):
    kind: str
    value: str
    amount: int | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, value: Any) -> str:
        return "" if value is None else str(value)


# >>> WS-A logic contract (frozen; downstream workstreams consume, do not edit shape) >>>
class LogicVarType(str, Enum):
    BOOL = "bool"
    INT = "int"
    ENUM = "enum"


class LogicVar(BaseModel):
    """A quest/world state variable the logic layer reads and writes."""

    id: str
    name: str = ""
    type: LogicVarType = LogicVarType.BOOL
    default: bool | int | str = False
    enum_values: list[str] = Field(default_factory=list)  # only for type=enum


class Effect(BaseModel):
    """A mutation applied to a variable when a stage completes."""

    var: str
    op: Literal["set", "inc", "dec"] = "set"
    value: bool | int | str = True


class StageLogic(BaseModel):
    stage_id: str
    precondition: str = ""  # boolean expression source; empty = always enterable
    effects_on_complete: list[Effect] = Field(default_factory=list)


class Branch(BaseModel):
    id: str
    from_stage: str
    condition: str = ""  # taken when this evaluates true
    to_stage: str = ""  # next stage id; empty + outcome set = a terminal outcome
    outcome: str = ""
    # Consequences applied when this branch is taken — the structural home for a choice's results
    # (e.g. faction reputation deltas via a `rep:<faction_id>` target, or any logic variable). This
    # is what keeps a generated "choice → reputation/standing change" out of an unstructured string.
    effects: list[Effect] = Field(default_factory=list)


class QuestLogic(BaseModel):
    """The native quest logic/state layer (single source of truth).

    Variables + boolean preconditions + on-complete effects + branches turn a quest from metadata
    into playable logic. Deterministic audit (see audit/rules/logic_rules.py) checks it for
    unreachable stages, deadlocks, undefined variables and type errors."""

    variables: list[LogicVar] = Field(default_factory=list)
    precondition: str = ""  # quest-level gate
    stage_logic: list[StageLogic] = Field(default_factory=list)
    branches: list[Branch] = Field(default_factory=list)
    unlocks: list[str] = Field(default_factory=list)  # quest ids unlocked on completion


# <<< WS-A logic contract <<<


class Quest(ProvenanceMixin):
    id: str
    title: str
    giver_npc: str | None = None
    location: str | None = None
    objective: str = ""
    prerequisites: list[str] = Field(default_factory=list)
    timeline_order: int | None = None
    stages: list[QuestStage] = Field(default_factory=list)
    rewards: list[Reward] = Field(default_factory=list)
    dialogue_refs: list[str] = Field(default_factory=list)
    localization_keys: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    logic: QuestLogic | None = None  # None = legacy quest, behaviour unchanged


class RegionBrief(ProvenanceMixin):
    id: str
    name: str
    level_min: int | None = None
    level_max: int | None = None
    themes: list[str] = Field(default_factory=list)
    allowed_content: list[str] = Field(default_factory=list)
    banned_content: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class POI(ProvenanceMixin):
    id: str
    name: str
    region_id: str | None = None
    purpose: str = ""
    controlling_faction: str | None = None
    level_min: int | None = None
    level_max: int | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DialogueRef(ProvenanceMixin):
    id: str
    text_key: str
    speaker_id: str | None = None
    quest_id: str | None = None
    text: str | None = None
    locale: str | None = None
    ui_max_len: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DialogueChoice(BaseModel):
    """A player option leading to another node (or ending the tree when next_node is None).

    ``effects`` are the state mutations applied when the player picks this choice — the structural
    home for "a dialogue choice changes the world" (the Detroit/BG3 mechanic), reusing the frozen
    ``Effect`` type from the WS-A logic contract so dialogue and quest logic share one effect
    vocabulary. Consumed by ``logic/dialogue.py`` (walk/explore) and validated by the dialogue
    audit rules (effect vars must be declared in the bound quest's logic).
    """

    text: str
    next_node: str | None = None
    condition: str = ""
    effects: list[Effect] = Field(default_factory=list)
    # Opt-in localization key (A1 remediation): empty by default so every pre-existing choice is
    # unaffected. When set, the exporters (xliff/unity_localization/unreal_datatable) treat
    # ``text`` as a translatable unit keyed by this string, the same way ``DialogueRef.text_key``
    # already works. See ``_omit_empty_optional_fields`` for why this stays out of old dumps.
    text_key: str = ""

    @model_serializer(mode="wrap")
    def _omit_empty_optional_fields(self, handler: Any) -> Any:
        # EMPTY optional fields are omitted from dumps so every pre-existing bundle, export and
        # content hash stays byte-identical (the golden-fixture byte tests are the sentinel);
        # a missing key round-trips back to each field's default.
        data = handler(self)
        if isinstance(data, dict):
            if not data.get("effects"):
                data.pop("effects", None)
            if not data.get("text_key"):
                data.pop("text_key", None)
        return data


class DialogueNode(BaseModel):
    id: str
    speaker_id: str | None = None
    text: str = ""
    choices: list[DialogueChoice] = Field(default_factory=list)
    next_node: str | None = None  # linear continuation when there are no choices
    # Opt-in localization key (A1 remediation) — see DialogueChoice.text_key for the rationale;
    # the two mirror each other so a tree's lines and its branch labels share one mechanism.
    text_key: str = ""

    @model_serializer(mode="wrap")
    def _omit_empty_text_key(self, handler: Any) -> Any:
        # Same byte-stability technique as DialogueChoice: an empty text_key never serializes, so
        # every dialogue tree written before this field existed keeps hashing/exporting identically.
        data = handler(self)
        if isinstance(data, dict) and not data.get("text_key"):
            data.pop("text_key", None)
        return data


class DialogueTree(ProvenanceMixin):
    """A branching conversation: nodes wired by choices/next links, entered at root_node."""

    id: str
    title: str = ""
    quest_id: str | None = None
    participants: list[str] = Field(default_factory=list)
    root_node: str = ""
    nodes: dict[str, DialogueNode] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LocalizedText(ProvenanceMixin):
    id: str
    text_key: str
    locale: str
    text: str
    ui_max_len: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Term(ProvenanceMixin):
    id: str
    canonical: str
    aliases: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    description: str = ""


class StyleGuide(ProvenanceMixin):
    id: str = "style_guide"
    body: str = ""
    rules: list[str] = Field(default_factory=list)


class StyleElementKind(str, Enum):
    """Which cascade level an element lands on (see ``assist/sweep`` rubric cascade).

    ``lexical`` elements carry concrete surface tokens/patterns and resolve on the deterministic
    $0 word/regex level (L1). ``semantic`` elements name a criterion that cannot be spelled as a
    word list ("情绪含蓄内敛，冲突留白") and land on the embedding concept level (L2) and the LLM
    judge (L3). This is "确定性优先" applied at element granularity, not whole-station granularity.
    """

    LEXICAL = "lexical"
    SEMANTIC = "semantic"


class StyleElementPolarity(str, Enum):
    """Whether an element asserts presence (``include``) or a taboo (``exclude``).

    For a "日式风格" check, "应出现鸟居" (include) and "不应出现十字架/教堂" (exclude) are equally
    load-bearing: a hit on an ``exclude`` element is a *violation*, a hit on an ``include`` element
    is a *confirmed presence*. Both surface as candidates for a human — never auto-resolved."""

    INCLUDE = "include"
    EXCLUDE = "exclude"


class StyleRubricElement(BaseModel):
    """One binary, human-checkable criterion inside a :class:`StyleRubric`.

    The single mechanism carrying three jobs at once (the reason the whole station stands on this
    shape, per §3.1 of the design): each element is simultaneously a **retrieval query** (its terms
    / concept text), a **binary yes/no question** (``text``), and the **hit reason** shown to the
    reviewer. ``kind`` routes it down the cascade; ``polarity`` distinguishes presence from taboo.
    """

    id: str
    text: str  # the human-readable binary criterion; doubles as the hit reason shown to a reviewer
    kind: StyleElementKind = StyleElementKind.LEXICAL
    terms: list[str] = Field(default_factory=list)  # L1 word list (lexical membership)
    patterns: list[str] = Field(default_factory=list)  # L1 optional regexes
    polarity: StyleElementPolarity = StyleElementPolarity.INCLUDE
    note: str = ""
    # Few-shot anchors for the L2 concept level, and the landing spot for "逐一排除" negative-sample
    # reflow (a reviewer dismissing a match teaches this element). Carried, but reflow itself is out
    # of scope for the infra round (a product decision, see design §7-Q4).
    exemplars_pos: list[str] = Field(default_factory=list)
    exemplars_neg: list[str] = Field(default_factory=list)


class StyleRubric(ProvenanceMixin):
    """A versioned, binary element checklist that operationalizes an abstract style tag.

    The 12th canon content type. It is deliberately NOT :class:`StyleGuide` (prose ``body`` +
    unstructured ``rules``, no version, no per-element structure): a rubric is a *versioned
    evaluation baseline* — changing the checklist changes the standard, so it must leave a trail
    (``version`` / ``frozen_at`` / ``author``) and be pinnable by a content fingerprint
    (``content_hash`` of the rubric, computed at the sweep boundary). It reuses the same
    file-backed store / copy-on-write version overlay / export chain as every other canon type.

    This is domain knowledge externalized as data (the Agent-Skills insight — knowledge lives
    outside the model), stood up as a canon type rather than borrowing the Skills name."""

    id: str
    label: str = ""  # human name, e.g. "和风浓度 v3"
    abstract_tag: str = ""  # the abstract word this checklist operationalizes, e.g. "日式风格"
    version: str = "v1"  # rubric content version (bumped on every edit to the element list)
    frozen_at: str | None = None  # ISO timestamp when this version was frozen; None = draft
    author: str = ""
    elements: list[StyleRubricElement] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContentBundle(BaseModel):
    """A normalized in-memory bundle loaded from the file-backed content store."""

    entities: dict[str, Entity] = Field(default_factory=dict)
    relations: list[Relation] = Field(default_factory=list)
    quest_event_refs: dict[str, QuestEventReference] = Field(default_factory=dict)
    quests: dict[str, Quest] = Field(default_factory=dict)
    regions: dict[str, RegionBrief] = Field(default_factory=dict)
    pois: dict[str, POI] = Field(default_factory=dict)
    dialogues: dict[str, DialogueRef] = Field(default_factory=dict)
    dialogue_trees: dict[str, DialogueTree] = Field(default_factory=dict)
    localized_texts: dict[str, LocalizedText] = Field(default_factory=dict)
    terms: dict[str, Term] = Field(default_factory=dict)
    style_guides: dict[str, StyleGuide] = Field(default_factory=dict)
    style_rubrics: dict[str, StyleRubric] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def _canonical_relation_order(self, handler: Any) -> Any:
        # relations 是 list、语义却是集合，故序列化时按规范键排序：内容相同的世界必须得到同一个
        # content_hash，与关系的插入顺序无关。
        #
        # Relations serialize in canonical order. ``relations`` is a list whose semantics are a set
        # (see :func:`relation_sort_key`), so its insertion order is an artifact of *how* a bundle
        # was assembled — regenerate a world, change import order, or merge two versions and the
        # same content produced a different order. ``content_hash`` hashes this dump, so before this
        # serializer that artifact leaked straight into the hash: identical worlds hashed
        # differently, silently costing cache hits, drifting audit fingerprints and making
        # ``content_hash_before/after`` report "content changed" when nothing had.
        #
        # Only ``relations`` is normalized. Every other list here is ORDER-SIGNIFICANT
        # (``Quest.stages``, ``DialogueNode.choices``, ``StyleGuide.rules``, …) and sorting one of
        # those would corrupt authored meaning, so this must never be generalized to lists at large.
        data = handler(self)
        if isinstance(data, dict) and isinstance(data.get("relations"), list):
            data["relations"] = sorted(data["relations"], key=relation_sort_key)
        # ``style_rubrics`` is the newest collection. An EMPTY one is dropped from the dump so every
        # pre-existing world — none of which carry a rubric — hashes, exports and snapshots
        # byte-for-byte as before (``content_hash`` / golden-fixture bytes are the sentinel). A
        # world that actually has a rubric keeps the key. Same technique as ``DialogueChoice`` empty
        # ``effects``; a missing key round-trips back to the default empty dict.
        if isinstance(data, dict) and not data.get("style_rubrics"):
            data.pop("style_rubrics", None)
        return data

    def add_entity(self, entity: Entity) -> None:
        self.entities[entity.id] = entity

    def add_relation(self, relation: Relation) -> None:
        self.relations.append(relation)

    def has_entity(self, entity_id: str) -> bool:
        return entity_id in self.entities
