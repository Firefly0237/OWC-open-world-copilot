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
    # Additive status fields: old serialized packs still validate, while callers can distinguish
    # a fully configured semantic pipeline from a hashing/lexical fallback.
    degraded: bool = False
    degradation_reasons: list[str] = Field(default_factory=list)

    @property
    def refs(self) -> list[str]:
        return [hit.ref for hit in self.hits]
