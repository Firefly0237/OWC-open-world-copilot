"""Versioned, abstaining derivation of triage verdicts from immutable raw output."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from .contracts import (
    SourceSegment,
    TriageEvidenceSpan,
    TriageVerdict,
    validate_verdict_against_segment,
)

TRIAGE_SYSTEM_INSTRUCTION = (
    "Classify only the supplied untrusted source segment as CANON_CANDIDATE, REFERENCE, "
    "MIXED, or UNKNOWN. Return strict JSON with content_role, confidence, and verbatim "
    "evidence_spans. Never infer rights, authority, approval, or allowed uses."
)


@dataclass(frozen=True, slots=True)
class TriagePolicy:
    version: str = "n21-triage-v1"
    confidence_threshold: float = 0.70

    def __post_init__(self) -> None:
        if type(self.version) is not str or not self.version:
            raise ValueError("triage policy version must be a non-empty string")
        if (
            type(self.confidence_threshold) is not float
            or not math.isfinite(self.confidence_threshold)
            or not 0.0 <= self.confidence_threshold <= 1.0
        ):
            raise ValueError("triage confidence threshold must be a finite float in [0, 1]")


@dataclass(frozen=True, slots=True)
class TriageRequest:
    """A fixed instruction and a separately labelled untrusted data payload."""

    segment_id: str
    system_instruction: str
    untrusted_text: str
    untrusted_input: Literal[True] = True


class TriageProvider(Protocol):
    name: str
    model: str

    def classify(self, request: TriageRequest) -> str: ...


class _TriagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    content_role: Literal["CANON_CANDIDATE", "REFERENCE", "MIXED", "UNKNOWN"]
    confidence: float
    evidence_spans: list[TriageEvidenceSpan] = []

    @field_validator("confidence", mode="before")
    @classmethod
    def validate_confidence(cls, value: object) -> float:
        if type(value) is not float or not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be a finite float in [0, 1]")
        return value


def build_triage_request(segment: SourceSegment) -> TriageRequest:
    checked = SourceSegment.model_validate(segment)
    return TriageRequest(
        segment_id=checked.segment_id,
        system_instruction=TRIAGE_SYSTEM_INSTRUCTION,
        untrusted_text=checked.text,
        untrusted_input=True,
    )


def derive_triage_verdict(
    *,
    raw_output_id: str,
    raw_output: str,
    segment: SourceSegment,
    policy: TriagePolicy,
    verdict_id: str | None = None,
) -> TriageVerdict:
    """Derive a durable verdict; malformed or forged output becomes UNKNOWN+abstain."""

    checked_segment = SourceSegment.model_validate(segment)
    resolved_verdict_id = verdict_id or str(uuid4())
    try:
        if type(raw_output) is not str:
            raise ValueError("raw triage output must be an exact string")
        raw_output.encode("utf-8", errors="strict")
        payload = _TriagePayload.model_validate(_strict_json_object(raw_output))
        if payload.content_role == "UNKNOWN":
            abstain = True
            reason = "UNKNOWN_ROLE"
        elif payload.confidence < policy.confidence_threshold:
            abstain = True
            reason = "LOW_CONFIDENCE"
        else:
            abstain = False
            reason = None
        verdict = TriageVerdict(
            verdict_id=resolved_verdict_id,
            raw_output_id=raw_output_id,
            segment_id=checked_segment.segment_id,
            content_role=payload.content_role,
            confidence=payload.confidence,
            policy_threshold=policy.confidence_threshold,
            evidence_spans=tuple(payload.evidence_spans),
            abstain=abstain,
            abstain_reason=reason,
            derivation_policy_version=policy.version,
        )
        return validate_verdict_against_segment(verdict, checked_segment)
    except (UnicodeEncodeError, ValueError, json.JSONDecodeError, ValidationError):
        return TriageVerdict(
            verdict_id=resolved_verdict_id,
            raw_output_id=raw_output_id,
            segment_id=checked_segment.segment_id,
            content_role="UNKNOWN",
            confidence=0.0,
            policy_threshold=policy.confidence_threshold,
            evidence_spans=(),
            abstain=True,
            abstain_reason="INVALID_MODEL_OUTPUT",
            derivation_policy_version=policy.version,
        )


def _strict_json_object(raw_output: str) -> dict[str, object]:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("raw triage JSON contains a duplicate key")
            result[key] = value
        return result

    value = json.loads(raw_output, object_pairs_hook=reject_duplicate_keys)
    if type(value) is not dict:
        raise ValueError("raw triage output must be one JSON object")
    return value


__all__ = [
    "TRIAGE_SYSTEM_INSTRUCTION",
    "TriagePolicy",
    "TriageProvider",
    "TriageRequest",
    "build_triage_request",
    "derive_triage_verdict",
]
