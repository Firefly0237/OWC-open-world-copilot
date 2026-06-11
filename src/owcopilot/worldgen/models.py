"""Domain models for creating a new world seed."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..content.models import ContentBundle


class WorldSeedBrief(BaseModel):
    idea: str = Field(min_length=1, max_length=4000)
    medium: str = "open-world game"
    game_genre: str = ""
    world_styles: list[str] = Field(default_factory=list)
    tone: str = ""
    era: str = ""
    player_fantasy: str = ""
    core_conflict: str = ""
    reference_mode: str = "灵感参考"
    reference_query: str = ""
    use_project_facts: bool = False
    faction_count: int = Field(default=3, ge=1, le=8)
    region_count: int = Field(default=2, ge=1, le=8)
    npc_count: int = Field(default=8, ge=1, le=24)
    quest_count: int = Field(default=5, ge=1, le=16)
    term_count: int = Field(default=5, ge=0, le=24)
    notes: str = ""


class ReferenceReportItem(BaseModel):
    source_ref: str
    source_title: str
    used_for: str
    transformation: str
    excluded: list[str] = Field(default_factory=list)


class WorldSeedDraft(BaseModel):
    id: str
    brief: WorldSeedBrief
    summary: str
    bundle: ContentBundle
    reference_report: list[ReferenceReportItem] = Field(default_factory=list)
    project_context_refs: list[str] = Field(default_factory=list)
    inspiration_context_refs: list[str] = Field(default_factory=list)
