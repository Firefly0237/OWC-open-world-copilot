"""Reviewer calibration: does the critic's verdict agree with the human's final decision?

The critic is a quality *signal*, not a gate — a human still signs off (see ``critic.py``). This
module tracks how often the two disagree, and surfaces the dangerous corner: the critic said
"pass" but the human still **rejected** the draft (a *false pass* — autonomous quality looked fine,
yet a human bounced it). It reads the critic's final verdict/score from each resolved review item's
``refine_trail`` and pairs it with the human's accepted/rejected decision. Pure and deterministic —
it makes no model calls; it just reports on decisions already recorded.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, Field

from .critic import CritiqueResult  # noqa: TC001 (used at runtime in primary_failing_dimension)
from .review_queue import ReviewItem

_HUMAN_ACCEPT = "accepted"
_HUMAN_REJECT = "rejected"
# Below this many resolved samples the rates are too noisy to act on; the report says so rather than
# letting a 1-of-1 false pass read as a confident "100%". A rule of thumb for a stable proportion.
_MIN_SUFFICIENT_SAMPLE = 20


def _wilson_interval(successes: int, total: int, *, z: float = 1.96) -> list[float] | None:
    """95% Wilson score interval for a proportion — honest error bars that stay in [0, 1] and stay
    wide for small n (a normal-approx interval would lie about tiny samples). Deterministic."""
    if total <= 0:
        return None
    phat = successes / total
    denom = 1.0 + z * z / total
    center = (phat + z * z / (2 * total)) / denom
    margin = (z / denom) * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total))
    return [round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4)]


def primary_failing_dimension(result: CritiqueResult) -> str:
    """Extract the most critical failing dimension from a CritiqueResult.

    Returns 'general' when parse_ok=False or no dimensions present.
    Priority: first blocker > first minor > first non-ok > 'general'.

    IN-B1 M2: used by the refine loop to stamp critic_primary_dim on ReviewItem.
    """
    if not result.parse_ok or not result.dimensions:
        return "general"
    blockers = [d for d in result.dimensions if d.severity == "blocker"]
    if blockers:
        return blockers[0].dimension
    minors = [d for d in result.dimensions if d.severity == "minor"]
    if minors:
        return minors[0].dimension
    non_ok = [d for d in result.dimensions if d.severity != "ok"]
    return non_ok[0].dimension if non_ok else "general"


class CalibrationMatrix(BaseModel):
    critic_pass_human_accept: int = 0  # agreement: critic & human both liked it
    critic_pass_human_reject: int = 0  # FALSE PASS — critic missed what the human caught
    critic_revise_human_accept: int = 0  # critic harsher than the human
    critic_revise_human_reject: int = 0  # agreement: both wanted changes


class FalsePassItem(BaseModel):
    """A draft the critic passed but the human rejected — the blind spot worth reviewing."""

    item_id: str
    item_type: str
    object_ref: str
    critic_score: float | None = None
    dimension: str = "general"  # IN-B1 M2: primary failing dimension; default "general" for compat


class CalibrationReport(BaseModel):
    sample_size: int = 0  # resolved items that carried a usable critic verdict
    matrix: CalibrationMatrix = Field(default_factory=CalibrationMatrix)
    false_pass_rate: float | None = None  # critic_pass_human_reject / all critic "pass"
    false_revise_rate: float | None = None  # critic_revise_human_accept / all critic "revise"
    agreement_rate: float | None = None  # (pass&accept + revise&reject) / sample_size
    mean_score_accepted: float | None = None
    mean_score_rejected: float | None = (
        None  # should sit below accepted if the score tracks quality
    )
    false_pass_rate_ci: list[float] | None = None  # 95% Wilson interval over critic-"pass" count
    sufficient_sample: bool = False  # False when too few samples to trust the rates (see threshold)
    min_sufficient_sample: int = _MIN_SUFFICIENT_SAMPLE
    by_type: dict[str, CalibrationMatrix] = Field(default_factory=dict)
    false_pass_items: list[FalsePassItem] = Field(default_factory=list)
    skipped_no_verdict: int = 0  # resolved single-shot / unparsable-critique items (no signal)


def primary_dim_from_trail(
    refine_trail: list[dict[str, Any]],
) -> str | None:
    """Extract the primary failing dimension from the last round of a refine trail.

    IN-B1 M2: Returns the 'primary_dim' field from the last trail step when the critique was
    parseable (auto_review_ok=True). Returns None when no trail or critique was unparseable.
    Callers use None to mean "unknown" and fall back to 'general' when stamping ReviewItem.
    """
    if not refine_trail:
        return None
    last = refine_trail[-1]
    if not isinstance(last, dict) or last.get("auto_review_ok") is False:
        return None
    dim = last.get("primary_dim")
    return str(dim) if isinstance(dim, str) else None


def critic_from_trail(
    refine_trail: list[dict[str, Any]],
) -> tuple[str | None, float | None]:
    """The critic's last-round verdict + score from a refine trail, or (None, None) when no critic
    ran or its reply could not be parsed (``auto_review_ok`` False) — an unparsable critique is not
    a real verdict, so it must not be recorded as a calibration data point. Generation actions call
    this to stamp the final verdict/score onto the review item at draft time."""
    if not refine_trail:
        return None, None
    last = refine_trail[-1]
    if not isinstance(last, dict) or last.get("auto_review_ok") is False:
        return None, None
    verdict_raw = last.get("verdict")
    verdict = str(verdict_raw) if verdict_raw in ("pass", "revise") else None
    score_raw = last.get("score")
    score = float(score_raw) if isinstance(score_raw, (int, float)) else None
    return verdict, score


def build_calibration_report(resolved: list[ReviewItem]) -> CalibrationReport:
    """Pair each resolved review item's recorded critic verdict with the human decision."""
    report = CalibrationReport()
    accepted_scores: list[float] = []
    rejected_scores: list[float] = []
    for item in resolved:
        if item.status not in (_HUMAN_ACCEPT, _HUMAN_REJECT):
            continue
        verdict = item.critic_verdict if item.critic_verdict in ("pass", "revise") else None
        score = item.critic_score
        if verdict is None:
            report.skipped_no_verdict += 1
            continue
        report.sample_size += 1
        human_accept = item.status == _HUMAN_ACCEPT
        cell = report.by_type.setdefault(item.item_type.value, CalibrationMatrix())
        for target in (report.matrix, cell):
            if verdict == "pass" and human_accept:
                target.critic_pass_human_accept += 1
            elif verdict == "pass":
                target.critic_pass_human_reject += 1
            elif human_accept:
                target.critic_revise_human_accept += 1
            else:
                target.critic_revise_human_reject += 1
        if score is not None:
            (accepted_scores if human_accept else rejected_scores).append(score)
        if verdict == "pass" and not human_accept:
            # IN-B1 M2: read primary dimension from ReviewItem (written by refine loop)
            primary_dim = getattr(item, "critic_primary_dim", None) or "general"
            report.false_pass_items.append(
                FalsePassItem(
                    item_id=item.id,
                    item_type=item.item_type.value,
                    object_ref=item.object_ref,
                    critic_score=score,
                    dimension=primary_dim,  # IN-B1 M2
                )
            )
    _finalize_rates(report, accepted_scores, rejected_scores)
    return report


def _finalize_rates(
    report: CalibrationReport, accepted_scores: list[float], rejected_scores: list[float]
) -> None:
    m = report.matrix
    pass_total = m.critic_pass_human_accept + m.critic_pass_human_reject
    revise_total = m.critic_revise_human_accept + m.critic_revise_human_reject
    if pass_total:
        report.false_pass_rate = m.critic_pass_human_reject / pass_total
        report.false_pass_rate_ci = _wilson_interval(m.critic_pass_human_reject, pass_total)
    if revise_total:
        report.false_revise_rate = m.critic_revise_human_accept / revise_total
    if report.sample_size:
        agree = m.critic_pass_human_accept + m.critic_revise_human_reject
        report.agreement_rate = agree / report.sample_size
    report.sufficient_sample = report.sample_size >= _MIN_SUFFICIENT_SAMPLE
    if accepted_scores:
        report.mean_score_accepted = sum(accepted_scores) / len(accepted_scores)
    if rejected_scores:
        report.mean_score_rejected = sum(rejected_scores) / len(rejected_scores)
