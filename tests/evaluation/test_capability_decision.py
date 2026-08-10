from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from owcopilot.evaluation.capability_decision import (
    ArtifactBindings,
    CapabilityDecisionCard,
    DecisionContractError,
    FixedBudget,
    HeldoutRunManifest,
    MetricGate,
    validate_heldout_run,
)


def _hash(char: str) -> str:
    return char * 64


def _card() -> CapabilityDecisionCard:
    return CapabilityDecisionCard(
        capability_id="multi_agent.router",
        policy_version="router-v1",
        frozen_at=datetime(2026, 8, 5, 10, tzinfo=UTC),
        real_problem="Independent evidence partitions can be inspected concurrently.",
        reproducible_failure_ids=("dev-serial-latency",),
        baseline="single-agent",
        candidates=("orchestrator-workers",),
        enable_if=("at least two independent evidence partitions",),
        disable_if=("strictly serial dependency chain",),
        budget=FixedBudget(
            model_calls=6,
            tool_calls=12,
            retrieval_calls=4,
            total_tokens=8000,
            context_tokens=3000,
            deadline_ms=30_000,
            max_concurrency=3,
            candidate_window=20,
        ),
        primary_metric=MetricGate(name="task_success", direction="min", threshold=0.8, unit="rate"),
        guardrails=(
            MetricGate(name="duplicate_work", direction="max", threshold=0.0, unit="rate"),
        ),
        failure_semantics={"worker_timeout": "coverage_gap", "join_timeout": "typed_failure"},
        fallback="single-agent only when no side effect has started",
        bindings=ArtifactBindings(
            dataset_sha256=_hash("a"),
            revision_sha256=_hash("b"),
            policy_sha256=_hash("c"),
            model_sha256=_hash("d"),
            index_sha256=None,
        ),
        dev_case_ids=("dev-1",),
        heldout_case_ids=("on-1", "off-1", "fault-1"),
        enable_case_ids=("on-1",),
        disable_case_ids=("off-1",),
        fault_case_ids=("fault-1",),
    )


def _run(card: CapabilityDecisionCard) -> HeldoutRunManifest:
    return HeldoutRunManifest(
        decision_sha256=card.digest(),
        policy_version=card.policy_version,
        started_at=card.frozen_at + timedelta(minutes=1),
        case_ids=card.heldout_case_ids,
        budget=card.budget,
        bindings=card.bindings,
    )


def test_frozen_card_digest_is_stable_and_valid_run_matches() -> None:
    card = _card()
    assert card.digest() == _card().digest()
    validate_heldout_run(card, _run(card))


def test_dev_leak_or_missing_negative_partition_is_rejected() -> None:
    payload = _card().model_dump()
    payload["heldout_case_ids"] = ("dev-1", "on-1", "fault-1")
    with pytest.raises((ValidationError, DecisionContractError), match="disjoint"):
        CapabilityDecisionCard.model_validate(payload)

    payload = _card().model_dump()
    payload["disable_case_ids"] = ()
    with pytest.raises(ValidationError):
        CapabilityDecisionCard.model_validate(payload)


def test_heldout_cases_must_be_fully_and_exclusively_labelled() -> None:
    payload = _card().model_dump()
    payload["heldout_case_ids"] = (*payload["heldout_case_ids"], "unlabelled-1")
    with pytest.raises((ValidationError, DecisionContractError), match="complete held-out"):
        CapabilityDecisionCard.model_validate(payload)

    payload = _card().model_dump()
    payload["fault_case_ids"] = (*payload["fault_case_ids"], "on-1")
    with pytest.raises((ValidationError, DecisionContractError), match="pairwise disjoint"):
        CapabilityDecisionCard.model_validate(payload)


@pytest.mark.parametrize("drift", ["budget", "bindings", "policy", "decision", "case"])
def test_heldout_drift_is_rejected(drift: str) -> None:
    card = _card()
    payload = _run(card).model_dump()
    if drift == "budget":
        payload["budget"]["tool_calls"] += 1
    elif drift == "bindings":
        payload["bindings"]["dataset_sha256"] = _hash("e")
    elif drift == "policy":
        payload["policy_version"] = "router-v2"
    elif drift == "decision":
        payload["decision_sha256"] = _hash("f")
    else:
        payload["case_ids"] = ("dev-1",)

    with pytest.raises(DecisionContractError):
        validate_heldout_run(card, HeldoutRunManifest.model_validate(payload))
