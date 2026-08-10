"""Pre-registered capability decisions and held-out run compatibility checks.

The contract prevents a complex Agent/RAG route from being justified after seeing its result.
Thresholds are selected on dev data, the card is frozen, and a held-out run must use exactly the
same policy, artifact bindings and total budget.  This module records the decision; individual
evaluation suites remain responsible for computing their metrics.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DecisionContractError(ValueError):
    """A decision card or held-out run violates the preregistration contract."""


class FixedBudget(BaseModel):
    """Total, not per-agent/per-stage, resource ceiling for one compared task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    retrieval_calls: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    context_tokens: int = Field(ge=0)
    deadline_ms: int = Field(gt=0)
    max_concurrency: int = Field(ge=1)
    candidate_window: int = Field(ge=0)


class ArtifactBindings(BaseModel):
    """Hashes that make the compared data/runtime policy reproducible."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    index_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class MetricGate(BaseModel):
    """One primary or guardrail metric fixed before held-out execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    direction: Literal["min", "max"]
    threshold: float
    unit: str = Field(min_length=1)


class CapabilityDecisionCard(BaseModel):
    """Frozen rationale, routing rule and experiment contract for one capability."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["capability-decision/v1"] = "capability-decision/v1"
    status: Literal["frozen"] = "frozen"
    capability_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]+$")
    policy_version: str = Field(min_length=1)
    frozen_at: datetime
    real_problem: str = Field(min_length=1)
    reproducible_failure_ids: tuple[str, ...] = Field(min_length=1)
    baseline: str = Field(min_length=1)
    candidates: tuple[str, ...] = Field(min_length=1)
    enable_if: tuple[str, ...] = Field(min_length=1)
    disable_if: tuple[str, ...] = Field(min_length=1)
    budget: FixedBudget
    primary_metric: MetricGate
    guardrails: tuple[MetricGate, ...] = Field(min_length=1)
    failure_semantics: dict[str, str] = Field(min_length=1)
    fallback: str = Field(min_length=1)
    bindings: ArtifactBindings
    dev_case_ids: tuple[str, ...] = Field(min_length=1)
    heldout_case_ids: tuple[str, ...] = Field(min_length=3)
    enable_case_ids: tuple[str, ...] = Field(min_length=1)
    disable_case_ids: tuple[str, ...] = Field(min_length=1)
    fault_case_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_partition(self) -> CapabilityDecisionCard:
        dev = set(self.dev_case_ids)
        heldout = set(self.heldout_case_ids)
        if len(dev) != len(self.dev_case_ids) or len(heldout) != len(self.heldout_case_ids):
            raise DecisionContractError("dev/held-out case ids must be unique")
        if dev & heldout:
            raise DecisionContractError("dev and held-out cases must be disjoint")
        partitions = {
            "enable": set(self.enable_case_ids),
            "disable": set(self.disable_case_ids),
            "fault": set(self.fault_case_ids),
        }
        labelled = set().union(*partitions.values())
        if labelled != heldout:
            raise DecisionContractError(
                "enable/disable/fault cases must partition the complete held-out set"
            )
        if any(
            partitions[left] & partitions[right]
            for left, right in (("enable", "disable"), ("enable", "fault"), ("disable", "fault"))
        ):
            raise DecisionContractError(
                "enable/disable/fault held-out partitions must be pairwise disjoint"
            )
        return self

    def digest(self) -> str:
        """Stable digest used by a held-out manifest to bind this exact frozen decision."""
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class HeldoutRunManifest(BaseModel):
    """The immutable inputs of one held-out run; scores live in its result artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1)
    started_at: datetime
    case_ids: tuple[str, ...] = Field(min_length=1)
    budget: FixedBudget
    bindings: ArtifactBindings


def validate_heldout_run(card: CapabilityDecisionCard, run: HeldoutRunManifest) -> None:
    """Reject tuning, data leakage or budget drift between a frozen card and held-out run."""
    if run.started_at <= card.frozen_at:
        raise DecisionContractError("held-out run must start after the decision was frozen")
    if run.decision_sha256 != card.digest():
        raise DecisionContractError("held-out run is not bound to this decision card")
    if run.policy_version != card.policy_version:
        raise DecisionContractError("policy version changed after preregistration")
    if run.budget != card.budget:
        raise DecisionContractError("held-out total budget differs from the frozen budget")
    if run.bindings != card.bindings:
        raise DecisionContractError("dataset/model/index/revision/policy bindings changed")
    if not set(run.case_ids) <= set(card.heldout_case_ids):
        raise DecisionContractError("held-out run contains unregistered or dev cases")


__all__ = [
    "ArtifactBindings",
    "CapabilityDecisionCard",
    "DecisionContractError",
    "FixedBudget",
    "HeldoutRunManifest",
    "MetricGate",
    "validate_heldout_run",
]
