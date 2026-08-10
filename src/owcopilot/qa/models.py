"""Lore QA models."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Citation(BaseModel):
    ref: str
    text: str = ""
    # Optional provenance fields are additive so answers/caches written before locatable intake
    # remain valid. ``locator`` preserves the native source coordinate; page/bbox are convenient
    # normalized projections for the UI.
    locator: dict[str, Any] | None = None
    page: int | None = None
    bbox: list[float] | None = None
    revision: str = ""
    source_title: str = ""
    source_preview: str = ""
    supported_claims: list[str] = Field(default_factory=list)

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


class QAFaithfulness(BaseModel):
    """RAGAS-style entailment annotation attached to a grounded answer (annotate, not block).

    Populated only when the opt-in faithfulness gate runs (``LoreQAService(faithfulness=...)`` is
    ``offline`` or ``llm``); it is ``None`` on the default ``off`` path, so it never changes
    existing behaviour. It carries a *signal*, not a verdict that overrides the answer: a low score
    never rewrites or suppresses the answer — it only flags that some claims lack evidence support,
    so a human reviewer can check them (review stays the authority).

    * ``judge`` — which judge produced this: ``"offline"`` (deterministic lexical stand-in),
      ``"llm"`` (live model judge), or ``"unavailable"`` (``llm`` was requested but no usable model
      provider — an honest non-verification, never a faked pass).
    * ``ok`` — whether the answer cleared ``threshold``. ``False`` for ``"unavailable"`` too:
      an unverified answer is not asserted faithful (fail-closed).
    * ``score`` — ``supported_claims / total_claims`` in ``[0, 1]``; ``None`` when unavailable.
    * ``unsupported_claims`` — the claim texts the evidence did not support.
    * ``note`` — reviewer-facing Chinese guidance describing what was (not) supported.
    """

    judge: str
    ok: bool
    score: float | None = None
    threshold: float
    unsupported_claims: list[str] = Field(default_factory=list)
    claim_evidence: list[dict[str, Any]] = Field(default_factory=list)
    note: str = ""


class QAAnswer(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    confidence: float = 0.0
    mentioned_entities: list[str] = Field(default_factory=list)
    unresolved_mentions: list[str] = Field(default_factory=list)
    refused: bool = False
    grounded: bool = False
    # ``citation_existence_only`` is intentionally distinct from claim support; consumers no
    # longer have to infer the strength of grounding from a single boolean.
    grounding_level: str = "none"
    verification_errors: list[str] = Field(default_factory=list)
    retrieval_degraded: bool = False
    retrieval_degradation_reasons: list[str] = Field(default_factory=list)
    # Additive, default None: the opt-in faithfulness (entailment) annotation. Present only when the
    # gate ran; None keeps the default off-path byte-compatible for existing consumers.
    faithfulness: QAFaithfulness | None = None

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
