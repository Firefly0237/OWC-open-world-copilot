"""Domain models for creating a new world seed."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..content.models import ContentBundle

# 溯源报告行现在住在素材线里（草稿链路也产它），这里保留原来的导入位置：
# ``from ..worldgen.models import ReferenceReportItem`` 全仓照旧可用。
from ..inspiration.report import ReferenceReportItem


class WorldSeedBrief(BaseModel):
    """Only `idea` is required. Every other dimension is optional and, when left empty,
    is OMITTED from the model prompt entirely — an empty field name in the prompt reads
    as an instruction to invent something for it (round-12 user report: a blank
    protagonist field pushed protagonist content into a worldview-only request).
    Counts may be 0 = "do not generate this section at all"."""

    idea: str = Field(min_length=1, max_length=4000)
    medium: str = ""
    game_genre: str = ""
    world_styles: list[str] = Field(default_factory=list)
    tone: str = ""
    era: str = ""
    player_fantasy: str = ""
    core_conflict: str = ""
    # Round-16: dimensions every real worldbuilding bible carries (genre/tone/era were
    # already here): the magic-or-tech premise, the world's physical scope, and the
    # content red-lines section (compliance is a first-class part of CN game docs).
    magic_level: str = ""
    world_scale: str = ""
    content_restrictions: str = ""
    # Creator-given protagonists/key cast ("名字：一句设定" per entry). The model must
    # keep them, deepen them, and weave relationships among them — not invent replacements.
    key_characters: list[str] = Field(default_factory=list)
    reference_mode: str = "灵感参考"
    reference_query: str = ""
    # Ground the new world in the inspiration library (on by default — that is what the library
    # is for). use_project_facts grounds it in the world's OWN already-approved canon, so a world
    # created after a manuscript extraction stays consistent with what was just imported.
    use_references: bool = True
    use_project_facts: bool = False
    faction_count: int = Field(default=3, ge=0, le=8)
    region_count: int = Field(default=2, ge=0, le=8)
    npc_count: int = Field(default=8, ge=0, le=24)
    quest_count: int = Field(default=5, ge=0, le=16)
    term_count: int = Field(default=5, ge=0, le=24)
    notes: str = ""


class WorldExpandBrief(BaseModel):
    """Grow more content on an EXISTING world, anchored to one focus.

    ``focus_ref`` is a typed canon reference the expansion deepens — ``region:<id>``,
    ``faction:<id>`` or ``quest:<id>``. Every piece of new content must reference existing canon
    ids (the deterministic assembly resolves them and flags anything dangling). Counts may be 0 to
    skip a content kind. Unlike creation, the spine/style guide are NOT regenerated — the existing
    one is read back as grounding, so a batch cannot drift from the world it extends."""

    focus_ref: str = Field(min_length=1, max_length=200)
    # What to deepen — the angle the focus stage sharpens into an expansion brief. Optional; an
    # empty angle lets the model read the focus's own unrealised tension from the canon.
    angle: str = Field(default="", max_length=2000)
    reference_mode: str = "灵感参考"
    reference_query: str = ""
    # Ground the batch in the inspiration library too (on by default, mirrors WorldSeedBrief).
    use_references: bool = True
    # Expansion grounds on the existing world by default — that IS the point — so project-fact
    # retrieval is on unless a caller deliberately wants a blank-slate side batch.
    use_project_facts: bool = True
    poi_count: int = Field(default=3, ge=0, le=12)
    npc_count: int = Field(default=4, ge=0, le=16)
    quest_count: int = Field(default=3, ge=0, le=12)
    notes: str = ""


class ExpandGrounding(BaseModel):
    """The verifiable grounding record for one expansion batch — the honest answer to "did the new
    content actually reference existing canon, or invent / omit references?"

    Every reference the model wrote is classified into exactly one of three buckets, so the totals
    are auditable and nothing is hidden:
      * ``grounded_refs`` — resolved to a real id the world contains (new→canon or new→new);
      * ``dangling_refs`` — pointed at a non-empty value that matches no real id (invented);
      * ``unspecified_refs`` — the model left the reference empty/blank, so assembly auto-anchored
        it to the focus. This is NOT silently grounded: an empty reference is a gap the writer
        should fill, so it is surfaced here and counted against the "trusted" gate.

    A batch is trustworthy only when BOTH ``dangling_refs`` and ``unspecified_refs`` are empty.
    ``canon_anchor`` names the focus the whole batch hangs from."""

    canon_anchor: str = ""
    grounded_refs: int = 0
    dangling_refs: list[str] = Field(default_factory=list)
    unspecified_refs: list[str] = Field(default_factory=list)
    canon_ids_referenced: list[str] = Field(default_factory=list)

    @property
    def is_trustworthy(self) -> bool:
        return not self.dangling_refs and not self.unspecified_refs


class DensitySignal(BaseModel):
    """A deterministic read on whether an expansion is diluting the main line. Surfaced to the
    planner as a signal (not a hard block): unbounded side-content is a real risk, so we measure it
    instead of pretending expansion is free. ``note`` is empty when the balance looks healthy."""

    existing_quests: int = 0
    new_quests: int = 0
    busiest_region: str = ""
    busiest_region_quests: int = 0
    note: str = ""


class WorldRefineRound(BaseModel):
    """One pass of the optional quests-stage generate→critique→refine loop, surfaced so the human
    reviewer can see how the capstone stage was improved before it reached them (mirrors
    ``assist.drafts.RefineRound`` for the single-quest loop)."""

    round: int
    verdict: str
    score: float
    gap_count: int
    fixes: list[str] = Field(default_factory=list)
    summary: str = ""
    auto_review_ok: bool = True  # False when this round's critique could not be parsed
    # Feedback note distilled from this round and carried into later attempts.
    reflection: str = ""
    # Primary failing dimension of this round's critique (blocker>minor>non-ok), so the
    # worldgen genesis/expand path feeds real dimensions into calibration like the assist paths do.
    # "general" when the critique was unparsable or had no failing dimension.
    primary_dim: str = "general"
    # Snapshot of the quest batch evaluated by this round's critique ({"quests": [...]}),
    # retained when a revision replaces it. None on pass/terminal rounds and old trails.
    artifact_before: dict[str, Any] | None = None


class ConversionNote(BaseModel):
    """One honest record of how the deterministic normaliser handled a gap or invalid reference in
    the model's raw output. This is the ledger that replaced silent completion: instead of faking a
    placeholder, round-robining a wrong foreign key, or dropping a relation with no trace, the
    normaliser records what happened here so nothing is hidden from the human reviewer.

    * ``kind`` classifies the event (see the four ``record_*`` helpers on ``ConversionReport``);
    * ``ref`` names the affected object (``quest:<id>``, ``poi:<id>``, a relation triple, or a
      section label like ``阵营``);
    * ``detail`` carries the raw value / disposition in a compact machine-readable form;
    * ``message`` is the Chinese, guidance-style explanation a reviewer reads.
    """

    kind: Literal["shortfall", "dangling_ref", "dropped_relation", "derived_stage"]
    ref: str
    detail: str = ""
    message: str = ""


class ConversionReport(BaseModel):
    """The full ledger for one genesis assembly. Every place the normaliser could have silently
    faked, coerced or dropped content is recorded here instead. An empty ledger (``is_clean``) means
    the model's output normalised without any completion — the honest ideal."""

    notes: list[ConversionNote] = Field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.notes

    def by_kind(self, kind: str) -> list[ConversionNote]:
        return [note for note in self.notes if note.kind == kind]

    def record_shortfall(self, label: str, requested: int, produced: int) -> None:
        """The model produced fewer items than requested. We no longer pad with placeholder
        filler — the shortfall is reported so the reviewer supplies real content or lowers count."""
        self.notes.append(
            ConversionNote(
                kind="shortfall",
                ref=label,
                detail=f"requested={requested} produced={produced}",
                message=(
                    f"模型只产出 {produced} 个「{label}」，少于请求的 {requested} 个；"
                    f"未用占位内容补足，请补写缺少的 {requested - produced} 个，或调低该项数量。"
                ),
            )
        )

    def record_dangling_ref(self, owner_ref: str, field: str, value: str) -> None:
        """A foreign key points at an id that does not exist. We no longer round-robin it onto some
        arbitrary real object — the original value is kept verbatim so the deterministic reference
        audit (UNKNOWN_ENTITY_REF / MISSING_RELATION_ENDPOINT) flags it into the review flow."""
        self.notes.append(
            ConversionNote(
                kind="dangling_ref",
                ref=owner_ref,
                detail=f"{field}={value}",
                message=(
                    f"{owner_ref} 的 {field} 指向不存在的对象「{value}」；未静默替换为其它对象，"
                    f"已保留原值交由确定性审计标记，请在审阅时改指向真实 id 或补建该对象。"
                ),
            )
        )

    def record_dropped_relation(self, source: str, kind: str, target: str, missing: str) -> None:
        """A declared relation named an endpoint that does not exist. The relation is dropped (an
        edge to a phantom node is not content), but the loss is recorded rather than silent."""
        self.notes.append(
            ConversionNote(
                kind="dropped_relation",
                ref=f"relation:{source}:{kind}:{target}",
                detail=f"missing={missing}",
                message=(
                    f"关系「{source} {kind} {target}」的端点「{missing}」不存在，已丢弃该关系；"
                    f"如需保留，请补建缺失的对象或修正端点 id。"
                ),
            )
        )

    def record_derived_stage(self, quest_ref: str, objective: str) -> None:
        """A quest arrived with no stages, so ONE stage was derived from the quest's own objective.
        This is kept (it is derived from the content itself, not invented), but recorded so the
        reviewer knows the arc is a single beat rather than a model-authored multi-stage plan."""
        self.notes.append(
            ConversionNote(
                kind="derived_stage",
                ref=quest_ref,
                detail=f"objective={objective}",
                message=(
                    f"{quest_ref} 未提供 stages，已按其 objective 派生一个阶段（源于任务自身，"
                    f"非凭空编造）；如需分幕推进，请补写具体阶段。"
                ),
            )
        )


class WorldSeedDraft(BaseModel):
    id: str
    brief: WorldSeedBrief
    summary: str
    bundle: ContentBundle
    reference_report: list[ReferenceReportItem] = Field(default_factory=list)
    project_context_refs: list[str] = Field(default_factory=list)
    inspiration_context_refs: list[str] = Field(default_factory=list)
    # Empty unless the optional world critic ran; one entry per refine round on the quests stage.
    refine_trail: list[WorldRefineRound] = Field(default_factory=list)
    # True when the quests critic's reply was unparsable even after a retry, so the capstone did not
    # clear an auto-review — flagged for human scrutiny rather than silently treated as passed.
    auto_review_incomplete: bool = False
    # The honest normalisation ledger: shortfalls, dangling refs, dropped relations and derived
    # stages the deterministic assembly recorded instead of silently completing. Empty = clean.
    conversion_report: ConversionReport = Field(default_factory=ConversionReport)


class WorldExpandDraft(BaseModel):
    """The result of one expansion: ``bundle`` holds ONLY the new content (new locations/NPCs/quests
    plus the relations wiring them to the existing world). It flows through the same review-queue
    write path as a world seed — accepting it merges the new content into the project, and the
    existing conflict check blocks any id that would overwrite canon."""

    id: str
    brief: WorldExpandBrief
    focus_label: str
    angle: str
    bundle: ContentBundle
    grounding: ExpandGrounding
    density: DensitySignal = Field(default_factory=DensitySignal)
    reference_report: list[ReferenceReportItem] = Field(default_factory=list)
    project_context_refs: list[str] = Field(default_factory=list)
    inspiration_context_refs: list[str] = Field(default_factory=list)
    refine_trail: list[WorldRefineRound] = Field(default_factory=list)
    auto_review_incomplete: bool = False
    # The honest normalisation ledger for the ONE silent-completion this assembly still does (a
    # quest with no stages gets one derived from its own objective) — mirrors WorldSeedDraft's
    # conversion_report (genesis). Dangling/blank references are NOT recorded here: those are
    # already covered by ``grounding`` (ExpandGrounding), expand's own honest ledger. Empty = clean.
    conversion_report: ConversionReport = Field(default_factory=ConversionReport)
