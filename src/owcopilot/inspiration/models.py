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
    # Detected at ingest so a whole book in any language is handled without asking the user.
    # Optional with defaults so references stored before this field still load.
    language: str = ""  # human label of the dominant language, e.g. "中文"
    languages: list[str] = Field(default_factory=list)  # all significant language labels
    char_count: int = 0
    chunk_count: int = 0
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
    # Chunks whose text matched a prompt-injection pattern. Uploaded references are untrusted and
    # reach generation prompts via grounding (OWASP LLM01 indirect injection), so we surface them
    # for the human's risk call rather than silently feeding them into a prompt.
    injection_flagged_chunks: list[str] = Field(default_factory=list)
