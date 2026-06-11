"""Models for distilling unstructured manuscripts into production content drafts."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..content.models import ContentBundle


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
    stats: dict[str, int] = Field(default_factory=dict)
