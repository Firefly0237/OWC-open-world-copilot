"""Domain models for creating a new world seed."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..content.models import ContentBundle


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
    # Expansion grounds on the existing world by default — that IS the point — so project-fact
    # retrieval is on unless a caller deliberately wants a blank-slate side batch.
    use_project_facts: bool = True
    poi_count: int = Field(default=3, ge=0, le=12)
    npc_count: int = Field(default=4, ge=0, le=16)
    quest_count: int = Field(default=3, ge=0, le=12)
    notes: str = ""


class ReferenceReportItem(BaseModel):
    source_ref: str
    source_title: str
    used_for: str
    transformation: str
    excluded: list[str] = Field(default_factory=list)


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
    # Verbal self-reflection distilled from this round (Reflexion memory), carried forward.
    reflection: str = ""
    # IN-B1 M2: primary failing dimension of this round's critique (blocker>minor>non-ok), so the
    # worldgen genesis/expand path feeds real dimensions into calibration like the assist paths do.
    # "general" when the critique was unparsable or had no failing dimension.
    primary_dim: str = "general"


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
