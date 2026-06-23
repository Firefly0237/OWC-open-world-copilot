"""Lore QA models."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Citation(BaseModel):
    ref: str
    text: str = ""

    @field_validator("ref", mode="before")
    @classmethod
    def _extract_ref(cls, value: Any) -> str:
        text = str(value or "").strip()
        bracketed = re.search(r"\[([a-z_]+:[^\]]+)\]", text)
        if bracketed:
            return bracketed.group(1)
        ref_like = re.search(r"\b[a-z_]+:[A-Za-z0-9_:\-]+\b", text)
        if ref_like:
            return ref_like.group(0)
        return text


class QAAnswer(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    confidence: float = 0.0
    mentioned_entities: list[str] = Field(default_factory=list)
    unresolved_mentions: list[str] = Field(default_factory=list)
    refused: bool = False
    grounded: bool = False
    verification_errors: list[str] = Field(default_factory=list)

    @field_validator("answer", mode="before")
    @classmethod
    def _coerce_answer(cls, value: Any) -> str:
        return "" if value is None else str(value)

    @field_validator("citations", mode="before")
    @classmethod
    def _coerce_citations(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return [{"ref": item} if isinstance(item, str) else item for item in value]
        if isinstance(value, str):
            return [{"ref": value}]
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: Any) -> float:
        if isinstance(value, str):
            normalized = value.strip().lower()
            labels = {
                "high": 0.85,
                "medium": 0.55,
                "low": 0.25,
                "高": 0.85,
                "中": 0.55,
                "低": 0.25,
            }
            if normalized in labels:
                return labels[normalized]
        return value

    @field_validator(
        "mentioned_entities", "unresolved_mentions", "verification_errors", mode="before"
    )
    @classmethod
    def _coerce_string_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return value


class QAVerification(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    unresolved_mentions: list[str] = Field(default_factory=list)
