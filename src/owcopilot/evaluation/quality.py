"""Deterministic quality rubric for generated Quest artifacts.

This is intentionally separate from consistency validators:
validators decide whether an artifact is safe to land; this rubric gives designers a cheap,
explainable first-pass quality signal. It is not a replacement for human review or LLM-as-judge.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from ..core.state import ValidationIssue

_ACTION_WORDS = {
    "aid",
    "bring",
    "collect",
    "defend",
    "deliver",
    "escort",
    "find",
    "guard",
    "help",
    "investigate",
    "locate",
    "protect",
    "recover",
    "rescue",
    "return",
    "track",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


class QualityReport(BaseModel):
    """Small, serialisable rubric result."""

    score: float = Field(ge=0.0, le=1.0)
    passed: bool
    checks: dict[str, float]
    warnings: list[str] = Field(default_factory=list)


def evaluate_quest_quality(
    artifact: dict[str, Any], issues: list[ValidationIssue] | None = None
) -> QualityReport:
    """Return a deterministic quality score in [0, 1].

    The rubric deliberately checks simple, defensible signals: field completeness, objective
    clarity, reward presence, prerequisite hygiene, and whether consistency errors remain.
    """
    issues = issues or []
    checks: dict[str, float] = {
        "required_fields": _required_fields_score(artifact),
        "objective_clarity": _objective_clarity_score(str(artifact.get("objective", ""))),
        "reward_present": 1.0 if str(artifact.get("reward", "")).strip() else 0.4,
        "prerequisite_hygiene": _prerequisite_hygiene_score(artifact),
        "consistency_clean": 0.0 if any(i.severity == "error" for i in issues) else 1.0,
    }
    weights = {
        "required_fields": 0.30,
        "objective_clarity": 0.25,
        "reward_present": 0.10,
        "prerequisite_hygiene": 0.15,
        "consistency_clean": 0.20,
    }
    score = sum(checks[k] * weights[k] for k in checks)
    warnings = _warnings(artifact, checks, issues)
    return QualityReport(
        score=round(score, 3),
        passed=score >= 0.70 and not warnings,
        checks=checks,
        warnings=warnings,
    )


def _required_fields_score(artifact: dict[str, Any]) -> float:
    required = ("title", "giver_npc", "location", "objective")
    present = sum(1 for k in required if str(artifact.get(k, "")).strip())
    return present / len(required)


def _objective_clarity_score(objective: str) -> float:
    toks = _TOKEN_RE.findall(objective.lower())
    if not toks:
        return 0.0
    has_action = bool(set(toks) & _ACTION_WORDS)
    length_score = min(len(toks) / 8.0, 1.0)
    return round((0.6 if has_action else 0.2) + 0.4 * length_score, 3)


def _prerequisite_hygiene_score(artifact: dict[str, Any]) -> float:
    title = str(artifact.get("title", "")).strip().lower()
    prereqs = artifact.get("prerequisites") or []
    if not isinstance(prereqs, list):
        return 0.0
    cleaned = [str(p).strip().lower() for p in prereqs if str(p).strip()]
    if len(cleaned) != len(set(cleaned)):
        return 0.4
    if title and title in cleaned:
        return 0.0
    if len(cleaned) > 5:
        return 0.6
    return 1.0


def _warnings(
    artifact: dict[str, Any], checks: dict[str, float], issues: list[ValidationIssue]
) -> list[str]:
    out: list[str] = []
    if checks["required_fields"] < 1.0:
        out.append("missing required quest fields")
    if checks["objective_clarity"] < 0.65:
        out.append("objective is too vague or lacks a clear action verb")
    if checks["reward_present"] < 1.0:
        out.append("reward is empty or weakly specified")
    if checks["prerequisite_hygiene"] < 1.0:
        out.append("prerequisites contain duplicates, self-reference, or excessive length")
    if any(i.severity == "error" for i in issues):
        out.append("consistency errors remain")
    if not str(artifact.get("title", "")).strip().istitle():
        out.append("title may need designer polish")
    return out
