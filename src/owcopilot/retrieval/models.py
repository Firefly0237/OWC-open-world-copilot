"""Retrieval domain models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RetrievalHit(BaseModel):
    ref: str
    object_type: str
    title: str
    body: str = ""
    score: float
    source: str
    metadata: dict[str, str] = Field(default_factory=dict)


class ContextPack(BaseModel):
    query: str
    budget_tokens: int
    hits: list[RetrievalHit] = Field(default_factory=list)

    @property
    def refs(self) -> list[str]:
        return [hit.ref for hit in self.hits]
