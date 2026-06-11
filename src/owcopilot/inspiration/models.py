"""Models for user-provided inspiration references."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ReferenceSource(BaseModel):
    id: str
    title: str
    source_type: str = "uploaded_file"
    original_filename: str | None = None
    allowed_uses: list[str] = Field(default_factory=lambda: ["inspiration"])
    text_hash: str
    created_at: str
    metadata: dict[str, str] = Field(default_factory=dict)


class ReferenceChunk(BaseModel):
    id: str
    source_id: str
    chunk_index: int
    title: str
    body: str
    metadata: dict[str, str] = Field(default_factory=dict)


class ReferenceIngestResult(BaseModel):
    source: ReferenceSource
    chunks: list[ReferenceChunk]
    indexed_count: int
