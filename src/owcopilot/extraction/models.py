"""Models for distilling unstructured manuscripts into production content drafts."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..content.models import ContentBundle


class CoverageReport(BaseModel):
    """How much of the source the extraction actually read, stated honestly.

    A long manuscript (a whole novel) is covered by *coarsening* chunk granularity within a
    bounded cost budget, so coverage stays 100% while the number of model calls stays capped.
    Only a document larger than the entire budget is covered partially — and then the uncovered
    tail is reported here rather than silently dropped, so the creator always knows exactly what
    the draft is (and is not) based on. This is the "no silent degradation" discipline applied
    to input size.
    """

    total_chars: int = 0
    covered_chars: int = 0
    chunk_count: int = 0
    chunk_chars: int = 0
    # "full" = whole doc at fine granularity; "coarsened" = whole doc at larger chunks to stay
    # within the call budget; "partial" = doc exceeds the budget, only the head was read.
    granularity: str = "full"
    language: str = ""  # human label of the dominant language (e.g. "中文")
    languages: list[str] = Field(default_factory=list)  # all significant language labels
    mixed: bool = False
    note: str = ""  # a creator-facing sentence describing coverage + language handling
    # 1-based indices of chunks whose model reply could not be parsed (skipped, not silently
    # dropped): a per-chunk failure no longer aborts the whole run.
    failed_chunks: list[int] = Field(default_factory=list)

    @property
    def covered_ratio(self) -> float:
        return 1.0 if self.total_chars <= 0 else min(1.0, self.covered_chars / self.total_chars)

    @property
    def complete(self) -> bool:
        return self.granularity != "partial"


class PlotBeat(BaseModel):
    """One beat of the story structure recovered from the manuscript."""

    order: int
    title: str
    summary: str = ""
    location: str | None = None
    participants: list[str] = Field(default_factory=list)


class ExtractionGap(BaseModel):
    """A field the manuscript did not pin down; the user fills it or delegates to AI."""

    ref: str  # unique gap id, e.g. "entity:npc_xxx.description"
    object_ref: str  # e.g. "entity:npc_xxx"
    field: str
    question: str
    suggestion: str = ""  # filled by the AI gap-fill pass; user still confirms


class UnsupportedItem(BaseModel):
    """A faithfulness flag: an extracted claim the source does not actually back. It may be a model
    inference or invention; we surface it for human verification rather than passing it off as
    grounded (the round-26 "don't mask, surface" discipline). Beyond the original name check this
    now also covers *relations* and *attributes* — the model can name two real entities yet invent
    the link or trait between them, which a name-only check never caught.
    """

    ref: str  # "entity:npc_xxx" / "term:term_xxx" / "relation:src|kind|tgt"
    name: str
    kind: str  # entity kind / "term" / "relation" / "attribute"
    # why it was flagged, and the human-readable claim that isn't supported
    reason: str = "name_not_in_source"
    detail: str = ""
    # verifier that raised it: "deterministic" (co-occurrence/substring) or "llm" (entailment judge)
    source_check: str = "deterministic"


class ExtractionDraft(BaseModel):
    """The reviewable output of one extraction run.

    `bundle` carries ai_draft/pending_review content only; nothing here touches the
    content store until the draft passes the review queue.
    """

    id: str
    source_title: str
    source_kind: str = "文稿"
    summary: str = ""
    bundle: ContentBundle
    plot_beats: list[PlotBeat] = Field(default_factory=list)
    gaps: list[ExtractionGap] = Field(default_factory=list)
    unresolved_relations: list[dict[str, str]] = Field(default_factory=list)
    # Extracted objects whose name is not found in the source text — surfaced for human verification
    # rather than passed off as faithfully grounded (the round-26 "don't mask, surface" discipline).
    unsupported: list[UnsupportedItem] = Field(default_factory=list)
    coverage: CoverageReport | None = None
    stats: dict[str, int] = Field(default_factory=dict)
