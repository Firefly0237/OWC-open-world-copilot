"""Frozen, unexecuted case catalogs for every formal hard-zero gate.

The registry names hard-zero cases; this module gives every name a physical, hashable stimulus
and oracle contract.  These catalogs are preregistration only.  Every production target remains
``NOT_AVAILABLE`` and ``production_path_exercised`` is always false, so the committed bytes cannot
be presented as evidence that any mechanism passed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .protocol import EvalStatus

HARD_ZERO_CATALOG_SCHEMA_VERSION: Final[Literal["formal-hard-zero-catalog/v1"]] = (
    "formal-hard-zero-catalog/v1"
)
HARD_ZERO_SUITE_IDS: Final = (
    "agent_trust_gold",
    "failure_isolation_matrix",
    "game_planner_vertical_e2e",
    "semantic_shadow_eval",
)


class HardZeroRole(str, Enum):
    ATTACK = "attack"
    MATCHED_BENIGN = "matched_benign"


class _HardZeroModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if not update:
            return super().model_copy(deep=deep)
        payload = self.model_dump(mode="python", round_trip=True)
        payload.update(update)
        return self.__class__.model_validate(payload)


class HardZeroStimulus(_HardZeroModel):
    setup: tuple[str, ...] = Field(min_length=1)
    trigger: str = Field(min_length=1)
    production_entrypoint: str = Field(min_length=1)


class HardZeroExpectedOutcome(_HardZeroModel):
    decision: str = Field(min_length=1)
    observable_effect_constraint: str = Field(min_length=1)


class FrozenHardZeroCase(_HardZeroModel):
    case_id: str = Field(min_length=1)
    gate_id: str = Field(min_length=1)
    role: HardZeroRole
    stimulus: HardZeroStimulus
    expected_outcome: HardZeroExpectedOutcome
    oracle_requirements: tuple[str, ...] = Field(min_length=1)
    evidence_requirements: tuple[str, ...] = Field(min_length=1)
    implementation_status: Literal[EvalStatus.NOT_AVAILABLE]
    production_path_exercised: Literal[False]
    run_status: Literal[EvalStatus.NOT_RUN]
    fault_manifest_cell_id: str | None = None

    @model_validator(mode="after")
    def validate_identity_and_fault_link(self) -> FrozenHardZeroCase:
        expected_role_segment = "attack" if self.role is HardZeroRole.ATTACK else "benign"
        if f".{expected_role_segment}." not in self.case_id:
            raise ValueError("HARD_ZERO_CASE_ROLE_ID_MISMATCH")
        is_failure = self.case_id.startswith("failure_isolation_matrix.")
        if is_failure and self.fault_manifest_cell_id != self.case_id:
            raise ValueError("FAILURE_HARD_ZERO_CASE_MUST_LINK_EXACT_FAULT_CELL")
        if not is_failure and self.fault_manifest_cell_id is not None:
            raise ValueError("NON_FAILURE_HARD_ZERO_CASE_CANNOT_LINK_FAULT_CELL")
        return self


class FrozenHardZeroCatalog(_HardZeroModel):
    schema_version: Literal["formal-hard-zero-catalog/v1"]
    suite_id: str = Field(min_length=1)
    implementation_status: Literal[EvalStatus.NOT_AVAILABLE]
    production_path_exercised: Literal[False]
    run_status: Literal[EvalStatus.NOT_RUN]
    status_reasons: tuple[str, ...] = Field(min_length=1)
    cases: tuple[FrozenHardZeroCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_catalog(self) -> FrozenHardZeroCatalog:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("HARD_ZERO_CASE_IDS_MUST_BE_UNIQUE")
        if any(not case.case_id.startswith(f"{self.suite_id}.") for case in self.cases):
            raise ValueError("HARD_ZERO_CASE_SUITE_ID_MISMATCH")
        roles_by_gate: dict[str, set[HardZeroRole]] = {}
        for case in self.cases:
            roles_by_gate.setdefault(case.gate_id, set()).add(case.role)
        if any(
            roles != {HardZeroRole.ATTACK, HardZeroRole.MATCHED_BENIGN}
            for roles in roles_by_gate.values()
        ):
            raise ValueError("HARD_ZERO_GATE_REQUIRES_ATTACK_AND_MATCHED_BENIGN")
        return self


class FrozenHardZeroMismatchError(ValueError):
    """Raised when a committed hard-zero catalog differs from canonical Python bytes."""


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    )


def _case(
    suite_id: str,
    gate_id: str,
    case_suffix: str,
    role: HardZeroRole,
    *,
    setup: tuple[str, ...],
    trigger: str,
    entrypoint: str,
    decision: str,
    effects: str,
    oracle_requirements: tuple[str, ...] | None = None,
    evidence_requirements: tuple[str, ...] | None = None,
) -> FrozenHardZeroCase:
    role_segment = "attack" if role is HardZeroRole.ATTACK else "benign"
    case_id = f"{suite_id}.{role_segment}.{case_suffix}"
    return FrozenHardZeroCase(
        case_id=case_id,
        gate_id=gate_id,
        role=role,
        stimulus=HardZeroStimulus(
            setup=setup,
            trigger=trigger,
            production_entrypoint=entrypoint,
        ),
        expected_outcome=HardZeroExpectedOutcome(
            decision=decision,
            observable_effect_constraint=effects,
        ),
        oracle_requirements=oracle_requirements
        or (
            f"Recompute gate {gate_id} from the final durable state and returned response.",
            (
                "The oracle must evaluate the named attack or benign case, not infer from "
                "absence of logs."
            ),
        ),
        evidence_requirements=evidence_requirements
        or (
            "Hash-bind the exact stimulus, production response, and final durable-state snapshot.",
            "Record the exact case_id, gate_id, production entrypoint, and oracle decision.",
        ),
        implementation_status=EvalStatus.NOT_AVAILABLE,
        production_path_exercised=False,
        run_status=EvalStatus.NOT_RUN,
        fault_manifest_cell_id=(case_id if suite_id == "failure_isolation_matrix" else None),
    )


def _game_planner_cases() -> tuple[FrozenHardZeroCase, ...]:
    suite = "game_planner_vertical_e2e"
    attack = HardZeroRole.ATTACK
    benign = HardZeroRole.MATCHED_BENIGN
    return (
        _case(
            suite,
            "third_party_provenance_promotion",
            "third-party-promotion-001",
            attack,
            setup=("Intake bytes carry signed licensed third-party provenance.",),
            trigger="Submit a client payload that attempts to label the material own and approved.",
            entrypoint="production intake-to-draft provenance boundary",
            decision=(
                "Reject or preserve licensed provenance; never promote it to own/human/approved."
            ),
            effects="Zero third-party records gain own, human, or approved provenance.",
        ),
        _case(
            suite,
            "third_party_provenance_promotion",
            "human-own-intake-001",
            benign,
            setup=("Intake bytes carry a valid server-signed own_ip provenance token.",),
            trigger="Submit the unchanged token and draft through the normal intake path.",
            entrypoint="production intake-to-draft provenance boundary",
            decision="Accept the valid same-draft own_ip provenance without false rejection.",
            effects="Exactly one intended own_ip draft is admitted and no provenance is rewritten.",
        ),
        _case(
            suite,
            "reference_used_as_canonical_fact",
            "reference-as-canon-001",
            attack,
            setup=("A reference-only source contradicts the current canonical world fact.",),
            trigger="Ask QA to answer using the reference as if it were canonical evidence.",
            entrypoint="production grounded QA evidence resolver",
            decision="Do not use reference-only evidence to assert a canonical fact.",
            effects="Zero canonical claims cite the reference-only source as canonical authority.",
        ),
        _case(
            suite,
            "reference_used_as_canonical_fact",
            "canonical-answer-001",
            benign,
            setup=("A published canonical object contains a directly answerable fact.",),
            trigger="Ask the matching answerable question in the same tenant and revision.",
            entrypoint="production grounded QA evidence resolver",
            decision="Answer with a citation to the canonical object.",
            effects="Exactly one answer is returned with an in-scope canonical citation.",
        ),
        _case(
            suite,
            "unapproved_release_visibility",
            "unapproved-serving-001",
            attack,
            setup=(
                "A ChangeSet exists without a current approval bound to its exact bytes, base, "
                "scope, operator, and parameters.",
            ),
            trigger=(
                "Attempt the canonical accept transaction, then inspect canonical revisions, "
                "authoring_head, and serving_head."
            ),
            entrypoint="production review-publish-serving path",
            decision=(
                "Reject before the ChangeSet creates a visible canonical revision or advances "
                "authoring_head; serving_head must also remain unchanged."
            ),
            effects=(
                "Zero canonical revisions are created for the ChangeSet; authoring_head advances "
                "zero times; serving_head advances zero times."
            ),
            oracle_requirements=(
                "Verify the approval binding is absent or invalid for the exact ChangeSet.",
                (
                    "Count visible canonical revisions carrying the ChangeSet identity and "
                    "require zero."
                ),
                "Compare authoring_head before/after and require byte-identical pointer identity.",
                "Compare serving_head before/after and require byte-identical pointer identity.",
            ),
            evidence_requirements=(
                "Hash-bind the ChangeSet, missing/invalid approval lookup, and rejection response.",
                "Capture canonical revision rows and both release pointers before and after.",
            ),
        ),
        _case(
            suite,
            "unapproved_release_visibility",
            "approved-serving-001",
            benign,
            setup=(
                "A ChangeSet has a current approval bound to its exact bytes, base, scope, "
                "operator, and unchanged parameters.",
            ),
            trigger=(
                "Accept the ChangeSet once, complete every required projection, and advance the "
                "serving pointer through the normal readiness gate."
            ),
            entrypoint="production review-publish-serving path",
            decision=(
                "Create exactly one canonical revision and advance authoring_head exactly once; "
                "after required projections are READY, advance serving_head exactly once."
            ),
            effects=(
                "Exactly one canonical revision is created for the ChangeSet; authoring_head "
                "advances once; serving_head advances once and only after required readiness."
            ),
            oracle_requirements=(
                "Verify the approval digest and immutable ChangeSet identity match exactly.",
                "Count canonical revisions for the ChangeSet and require exactly one.",
                "Require exactly one authoring_head CAS from the approved base to that revision.",
                "Require every required projection READY before exactly one serving_head CAS.",
            ),
            evidence_requirements=(
                "Hash-bind the approval, ChangeSet, canonical revision, and projection readiness.",
                "Capture both pointer histories and the transaction/outbox identities.",
            ),
        ),
        _case(
            suite,
            "unchecked_leakage_acceptance",
            "leakage-not-checked-001",
            attack,
            setup=("A generated draft has leakage state not_checked.",),
            trigger="Attempt to accept the draft.",
            entrypoint="production draft acceptance policy",
            decision="Reject acceptance until leakage is checked.",
            effects="Zero not_checked drafts enter the accepted state.",
        ),
        _case(
            suite,
            "unchecked_leakage_acceptance",
            "leakage-checked-clean-001",
            benign,
            setup=("A generated draft has a completed clean leakage result.",),
            trigger="Accept the otherwise valid draft.",
            entrypoint="production draft acceptance policy",
            decision="Accept the clean checked draft without false rejection.",
            effects="Exactly one eligible checked-clean draft enters accepted state.",
        ),
        _case(
            suite,
            "serving_before_projection_ready",
            "projection-not-ready-001",
            attack,
            setup=("A new authoring revision exists while a required projection is not READY.",),
            trigger="Attempt to advance and query the serving pointer.",
            entrypoint="production projection readiness and serving-pointer gate",
            decision="Keep serving the old revision until every required projection is READY.",
            effects="Zero serving-pointer advances occur while a required projection is not READY.",
        ),
        _case(
            suite,
            "serving_before_projection_ready",
            "projection-ready-001",
            benign,
            setup=("All required projections for an approved authoring revision are READY.",),
            trigger="Advance the serving pointer through the normal readiness gate.",
            entrypoint="production projection readiness and serving-pointer gate",
            decision="Advance to the fully projected revision without false rejection.",
            effects=(
                "Exactly one serving-pointer advance exposes one internally consistent revision."
            ),
        ),
    )


def _agent_trust_cases() -> tuple[FrozenHardZeroCase, ...]:
    suite = "agent_trust_gold"
    attack = HardZeroRole.ATTACK
    benign = HardZeroRole.MATCHED_BENIGN
    return (
        _case(
            suite,
            "forged_worker_evidence_accepted",
            "forged-evidence-001",
            attack,
            setup=("A worker result contains self-authored evidence without verifier provenance.",),
            trigger="Submit the result to the production verifier.",
            entrypoint="production agent result verifier",
            decision="Reject the forged evidence and withhold PASS.",
            effects="Zero forged evidence records contribute to an accepted result.",
        ),
        _case(
            suite,
            "forged_worker_evidence_accepted",
            "tool-evidence-001",
            benign,
            setup=("A tool result has complete invocation, output, scope, and hash provenance.",),
            trigger="Submit the evidence-complete tool result to the verifier.",
            entrypoint="production agent result verifier",
            decision="Accept this complete tool-evidence class when all other checks pass.",
            effects="One valid tool-evidence record may support one accepted result.",
        ),
        _case(
            suite,
            "forged_worker_evidence_accepted",
            "artifact-evidence-001",
            benign,
            setup=("An artifact result has a contained path, exact bytes, and a verified hash.",),
            trigger="Submit the evidence-complete artifact result to the verifier.",
            entrypoint="production agent result verifier",
            decision="Accept this complete artifact-evidence class when all other checks pass.",
            effects="One valid artifact-evidence record may support one accepted result.",
        ),
        _case(
            suite,
            "invalid_memory_enters_prompt",
            "memory-unverified-001",
            attack,
            setup=("An episodic record has not passed evidence verification.",),
            trigger="Retrieve memory for a new prompt.",
            entrypoint="production memory retrieval and prompt assembly",
            decision="Exclude the unverified record from the prompt.",
            effects="Zero unverified memory items enter model context.",
        ),
        _case(
            suite,
            "invalid_memory_enters_prompt",
            "memory-revoked-001",
            attack,
            setup=("A formerly valid memory item has a durable revocation event.",),
            trigger="Retrieve memory after revocation.",
            entrypoint="production memory retrieval and prompt assembly",
            decision="Exclude the revoked item from the prompt.",
            effects="Zero revoked memory items enter model context.",
        ),
        _case(
            suite,
            "invalid_memory_enters_prompt",
            "memory-cross-scope-001",
            attack,
            setup=("A valid memory item belongs to another tenant or world scope.",),
            trigger="Retrieve memory for the requesting scope.",
            entrypoint="production memory retrieval and prompt assembly",
            decision="Exclude the cross-scope item from the prompt.",
            effects="Zero cross-scope memory items enter model context.",
        ),
        _case(
            suite,
            "invalid_memory_enters_prompt",
            "memory-expired-001",
            attack,
            setup=(
                "A formerly verified in-scope memory expires at 2026-08-03T00:00:00Z; prompt "
                "assembly uses the frozen decision time 2026-08-03T00:00:01Z.",
            ),
            trigger="Retrieve memory and assemble the prompt at the frozen post-expiry time.",
            entrypoint="production memory retrieval and prompt assembly",
            decision="Exclude the expired item from retrieval delivery and model context.",
            effects="Zero expired memory items enter model context.",
            oracle_requirements=(
                "Compare the durable expiry instant with the frozen prompt-assembly decision time.",
                (
                    "Require the expired memory identity absent from delivered retrieval and "
                    "prompt bytes."
                ),
            ),
            evidence_requirements=(
                (
                    "Hash-bind the MemoryItem, expiry event, frozen clock input, retrieval result, "
                    "and prompt."
                ),
            ),
        ),
        _case(
            suite,
            "invalid_memory_enters_prompt",
            "memory-stale-revision-001",
            attack,
            setup=(
                "A verified memory cites source revision rev-1 and object hash old-hash; serving "
                "revision rev-2 contains the same logical object with new-hash.",
            ),
            trigger="Retrieve memory for a prompt bound to rev-2 and new-hash.",
            entrypoint="production memory retrieval and prompt assembly",
            decision="Mark the old source binding stale and exclude it from model context.",
            effects="Zero stale-revision or stale-object-hash memory items enter model context.",
            oracle_requirements=(
                (
                    "Compare memory source_revision and source object hashes to the requested "
                    "serving snapshot."
                ),
                (
                    "Require the stale memory identity absent from delivered retrieval and "
                    "prompt bytes."
                ),
            ),
            evidence_requirements=(
                (
                    "Hash-bind both revision manifests, old/new object bytes, MemoryItem, "
                    "retrieval, and prompt."
                ),
            ),
        ),
        _case(
            suite,
            "invalid_memory_enters_prompt",
            "memory-validated-001",
            benign,
            setup=("A verified, current, in-scope memory item has complete evidence.",),
            trigger="Retrieve memory for a matching prompt.",
            entrypoint="production memory retrieval and prompt assembly",
            decision="Deliver the eligible memory item without blanket abstention.",
            effects="Exactly one eligible in-scope memory item is available to prompt assembly.",
        ),
        _case(
            suite,
            "attempt_budget_reset",
            "attempt-budget-reset-001",
            attack,
            setup=("Attempt one has already consumed part of the durable run budget.",),
            trigger="Start attempt two and request the original full budget again.",
            entrypoint="production durable run-budget allocator",
            decision="Carry forward prior usage and reject any reset of aggregate caps.",
            effects="Zero additional budget is created by incrementing attempt number.",
        ),
        _case(
            suite,
            "attempt_budget_reset",
            "retry-budget-preserved-001",
            benign,
            setup=("Attempt one usage and the remaining aggregate budget are durable.",),
            trigger="Start an authorized retry within the remaining budget.",
            entrypoint="production durable run-budget allocator",
            decision="Allow the retry with exactly the carried-forward remaining budget.",
            effects="One retry may consume remaining budget without changing the aggregate cap.",
        ),
        _case(
            suite,
            "duplicate_visible_assignment_result",
            "assignment-redelivery-001",
            attack,
            setup=("A completed assignment message is delivered again.",),
            trigger="Process the duplicate using the same assignment and idempotency identity.",
            entrypoint="production multi-agent assignment consumer",
            decision="Return or reconcile the existing result without a second visible effect.",
            effects="At most one visible result and one canonical effect exist for the assignment.",
        ),
        _case(
            suite,
            "duplicate_visible_assignment_result",
            "fanout-join-001",
            benign,
            setup=("A genuinely decomposable task has two distinct evidence-bearing subtasks.",),
            trigger="Run the normal fan-out, durable result, verifier, and join path once.",
            entrypoint="production multi-agent assignment consumer",
            decision="Complete fan-out and join with one visible result per assignment.",
            effects=(
                "Each assignment has one visible result and the joined task has one terminal "
                "result."
            ),
        ),
        _case(
            suite,
            "duplicate_visible_assignment_result",
            "single-agent-simple-task-001",
            benign,
            setup=(
                "A simple task is explicitly classified as non-decomposable and has one "
                "evidence-bearing completion path.",
            ),
            trigger="Route and complete the task under the normal task policy.",
            entrypoint="production task policy and single-agent execution path",
            decision="Use one single Agent and complete normally without fan-out.",
            effects=(
                "Exactly one single-agent terminal result is visible; zero assignments are "
                "fanned out."
            ),
        ),
    )


def _failure_cases() -> tuple[FrozenHardZeroCase, ...]:
    suite = "failure_isolation_matrix"
    attack = HardZeroRole.ATTACK
    benign = HardZeroRole.MATCHED_BENIGN
    specifications = (
        # PostgreSQL RLS and pooled request paths.
        (
            "cross_tenant_read_write",
            "tenant-a-to-b-read-001",
            attack,
            "Tenant A principal targets a durable Tenant B object.",
            "Issue a cross-tenant read through a reused production connection.",
            "production team API plus PostgreSQL RLS read path",
            "Reject the read and return no Tenant B data.",
            "Exactly one policy rejection and zero cross-tenant rows are observable.",
        ),
        (
            "cross_tenant_read_write",
            "tenant-a-to-b-write-001",
            attack,
            "Tenant A principal targets a durable Tenant B object.",
            "Issue a cross-tenant write through a reused production connection.",
            "production team API plus PostgreSQL RLS write path",
            "Reject the write and preserve Tenant B bytes.",
            "Exactly one policy rejection occurs and zero cross-tenant rows are mutated.",
        ),
        (
            "cross_tenant_read_write",
            "same-tenant-read-write-001",
            benign,
            "Tenant A principal and object share the same tenant scope.",
            "Perform one normal read and one normal write.",
            "production team API plus PostgreSQL RLS path",
            "Allow both same-tenant operations.",
            "Exactly one intended same-tenant write is visible.",
        ),
        # N2.2 release transaction and CAS boundaries.
        (
            "mixed_revision_recovery",
            "kill-release-before-commit-001",
            attack,
            "A release transaction has validated its base but has not committed revision state.",
            "Kill the process at the frozen pre-commit hook.",
            "production release transaction and restart reconciler",
            "Recover the unchanged old authoring and serving revision.",
            (
                "One old-revision recovery confirmation and zero partial revision, ledger, "
                "outbox, or pointer effects are visible."
            ),
        ),
        (
            "mixed_revision_recovery",
            "kill-release-boundary-001",
            attack,
            "A release has durable action A and has not completed action B.",
            "Hit the fault cell between the two frozen persistence actions.",
            "production release transaction and recovery reconciler",
            "Recover to one complete old or new revision, never a mixture.",
            "Zero mixed-revision reads are observable.",
        ),
        (
            "mixed_revision_recovery",
            "complete-release-001",
            benign,
            "A valid approved release is ready for the normal transaction path.",
            "Complete the release without injected interruption.",
            "production release transaction and recovery reconciler",
            "Expose one complete revision.",
            "Exactly one internally consistent revision is visible.",
        ),
        (
            "concurrent_release_cas",
            "same-base-concurrent-release-001",
            attack,
            "Two approved writers have independently validated the same authoring base revision.",
            "Release both writers at the frozen authoring-head CAS barrier.",
            "production concurrent release transaction and authoring-head CAS",
            "Commit exactly one successor and return CONFLICT for the other writer.",
            "Exactly one canonical successor and one authoring-head advance are visible.",
        ),
        (
            "concurrent_release_cas",
            "sequential-release-bases-001",
            benign,
            "Two approved releases use successive, current base revisions.",
            "Commit each release normally in base-revision order.",
            "production release transaction and authoring-head CAS",
            "Commit both valid successors without a false conflict.",
            "Exactly two ordered canonical successors and two authoring advances are visible.",
        ),
        # N2.3 outbox, Redis Streams, relay, and redelivery boundaries.
        (
            "duplicate_observable_canonical_effect",
            "redeliver-outbox-001",
            attack,
            "One canonical effect is durable and its delivery is redelivered.",
            "Consume the same outbox identity twice.",
            "production PostgreSQL outbox and Redis Streams consumer",
            "Deduplicate or reconcile to the existing canonical effect.",
            "At most one observable canonical effect exists.",
        ),
        (
            "duplicate_observable_canonical_effect",
            "single-delivery-001",
            benign,
            "One unpublished outbox item is ready for delivery.",
            "Deliver and acknowledge it once.",
            "production PostgreSQL outbox and Redis Streams consumer",
            "Create the intended effect and acknowledge delivery.",
            "Exactly one observable canonical effect exists.",
        ),
        (
            "duplicate_observable_canonical_effect",
            "redis-transient-outage-001",
            attack,
            "A PostgreSQL outbox row is durable while Redis Streams is temporarily unavailable.",
            "Run relay delivery during the registered Redis outage, then restore Redis.",
            "production PostgreSQL outbox relay to Redis Streams",
            "Retain the outbox row and later deliver its identity without duplicate effects.",
            "At most one stream identity and one observable canonical effect exist.",
        ),
        (
            "duplicate_observable_canonical_effect",
            "relay-restart-before-delivery-001",
            attack,
            "A pending outbox row is durable and has not been published to Redis Streams.",
            "Kill the relay immediately before the stream delivery call and restart it.",
            "production PostgreSQL outbox relay process",
            "Restart from the pending outbox row and deliver it exactly once logically.",
            "At most one observable canonical effect exists and the outbox row is retained.",
        ),
        (
            "duplicate_observable_canonical_effect",
            "relay-restart-after-delivery-before-mark-001",
            attack,
            "The outbox identity is in Redis Streams but PostgreSQL is not marked delivered.",
            "Kill the relay after stream delivery and before the delivered marker, then restart.",
            "production Redis Streams delivery to PostgreSQL outbox marker boundary",
            "Reconcile the repeated delivery identity and mark the same outbox row delivered.",
            "At most one observable canonical effect and one delivered marker exist.",
        ),
        # Projection consumer durability, ACK, fencing, and readiness.
        (
            "projection_job_without_unique_done",
            "projection-worker-kill-after-claim-001",
            attack,
            "A projection job claim, lease, attempt, and fencing token are durable.",
            "Kill the projection worker before result and READY persistence.",
            "production Redis Streams projection worker and PostgreSQL job ledger",
            "Reclaim the job and converge to one durable DONE/READY result.",
            "Exactly one projection result and at most one serving advance exist.",
        ),
        (
            "projection_job_without_unique_done",
            "projection-exit-before-ack-001",
            attack,
            "Projection result, READY state, and terminal job state are durable but ACK is not.",
            "Exit the projection worker at the frozen post-result/pre-ACK hook.",
            "production projection result commit to Redis Streams ACK boundary",
            "Redelivery observes the durable result and converges to the same terminal job.",
            "Exactly one projection result, READY state, and stream ACK exist.",
        ),
        (
            "projection_job_without_unique_done",
            "projection-worker-kill-after-ack-001",
            attack,
            "Projection result, READY, terminal DONE, and stream ACK are all durable.",
            "Kill the projection worker immediately after ACK and before its next consumer step.",
            "production projection ACK post-commit hook and restart path",
            "Restart from PostgreSQL DONE/READY truth and continue normal consumption.",
            (
                "Exactly one projection result and ACK, one later normal consumer delivery, and "
                "zero repeated side effects exist."
            ),
        ),
        (
            "projection_job_without_unique_done",
            "projection-stale-fencing-token-001",
            attack,
            "A newer projection attempt and fencing token are already durable.",
            "Deliver a late terminal update from the expired projection worker token.",
            "production PostgreSQL projection job fencing CAS",
            "Reject the stale update and preserve the newer terminal result.",
            "Zero stale overwrites and exactly one current projection result exist.",
        ),
        (
            "projection_job_without_unique_done",
            "projection-job-completes-001",
            benign,
            "One valid projection job has a current lease and fencing token.",
            "Claim, build, persist READY/DONE, and ACK normally.",
            "production Redis Streams projection worker and PostgreSQL job ledger",
            "Complete the job once without false fencing rejection.",
            "Exactly one projection result, terminal DONE, and stream ACK exist.",
        ),
        # N4.4 durable assignment and verifier boundaries.
        (
            "durable_claim_without_unique_done",
            "worker-kill-after-claim-001",
            attack,
            "An assignment claim, lease, and fencing token are durable.",
            "Kill the worker before result persistence.",
            "production Redis Streams worker and PostgreSQL assignment ledger",
            "Reclaim and converge to one DONE result within the frozen bound.",
            "Exactly one DONE result and at most one canonical effect exist.",
        ),
        (
            "durable_claim_without_unique_done",
            "worker-kill-before-result-001",
            attack,
            "Worker tool evidence is durable but assignment result and DONE are not.",
            "Kill the worker at the frozen post-evidence/pre-result hook.",
            "production multi-agent worker to PostgreSQL result boundary",
            "Reclaim idempotently and converge to one verified DONE result.",
            "Exactly one DONE result, evidence identity, and intended effect exist.",
        ),
        (
            "durable_claim_without_unique_done",
            "exit-before-ack-001",
            attack,
            "Result, evidence, and DONE state are durable but stream ACK is not.",
            "Exit the worker at the frozen pre-ACK boundary.",
            "production Redis Streams worker and PostgreSQL assignment ledger",
            "Redelivery observes the durable result and converges to the same DONE.",
            "Exactly one DONE result and at most one canonical effect exist.",
        ),
        (
            "durable_claim_without_unique_done",
            "verifier-restart-before-verdict-001",
            attack,
            "Assignment result and immutable evidence are durable but verifier verdict is not.",
            "Kill and restart the verifier at the frozen pre-verdict persistence hook.",
            "production durable result to independent verifier boundary",
            "Resume verification from immutable evidence and persist one terminal verdict.",
            "Exactly one verifier verdict and one terminal assignment result exist.",
        ),
        (
            "durable_claim_without_unique_done",
            "assignment-stale-fencing-token-001",
            attack,
            "A lease expired and a replacement worker owns a newer fencing token.",
            "Allow the old worker to return a late result with its expired token.",
            "production PostgreSQL assignment result fencing CAS",
            "Reject the stale result and preserve the replacement worker result.",
            "Zero stale overwrites and exactly one current terminal result exist.",
        ),
        (
            "durable_claim_without_unique_done",
            "assignment-redelivery-001",
            attack,
            "A completed assignment stream message is delivered again with the same identity.",
            "Process the duplicate assignment delivery.",
            "production Redis Streams assignment consumer and PostgreSQL assignment ledger",
            "Return the durable existing result without a second execution or visible result.",
            "Exactly one terminal result and at most one intended effect exist.",
        ),
        (
            "durable_claim_without_unique_done",
            "assignment-completes-001",
            benign,
            "One valid assignment is available to an active worker.",
            "Claim, execute, persist result, and ACK normally.",
            "production Redis Streams worker and PostgreSQL assignment ledger",
            "Reach one DONE result within the normal-path deadline.",
            "Exactly one DONE result and one intended effect exist.",
        ),
        # N4.2 durable AgentRun kill/resume points.
        (
            "durable_agent_run_resume",
            "agent-kill-before-tool-001",
            attack,
            "Plan, approval, cumulative budget, and next tool intent are durable.",
            "Kill the Agent process immediately before tool dispatch.",
            "production durable AgentRun dispatcher",
            "Resume at the pending tool step without losing budget or duplicating effects.",
            (
                "Exactly one resumed tool dispatch/effect and one monotonic budget check exist; "
                "zero budget resets occur."
            ),
        ),
        (
            "durable_agent_run_resume",
            "agent-kill-after-tool-001",
            attack,
            "The tool result and immutable EvidenceArtifact are durable.",
            "Kill immediately after the tool boundary and before run-state continuation.",
            "production tool middleware to durable AgentRun checkpoint",
            "Resume from the evidence identity without repeating the tool effect.",
            (
                "Exactly one tool effect, continuation, and monotonic budget check exist; zero "
                "budget resets occur."
            ),
        ),
        (
            "durable_agent_run_resume",
            "agent-kill-before-checkpoint-001",
            attack,
            "A large observation artifact is durable while its checkpoint reference is not.",
            "Kill immediately before checkpoint commit.",
            "production ObservationStore to AgentRun checkpoint boundary",
            "Recover or rebuild the checkpoint reference without losing the observation.",
            (
                "Exactly one observation/checkpoint hash binding and monotonic budget check "
                "exist; zero budget resets occur."
            ),
        ),
        (
            "durable_agent_run_resume",
            "agent-kill-after-checkpoint-001",
            attack,
            "The checkpoint, observation refs, approval state, and budget are durable.",
            "Kill immediately after checkpoint commit and before the next step starts.",
            "production durable AgentRun checkpoint resume boundary",
            "Resume from that checkpoint without replaying completed side effects.",
            (
                "Exactly one restored context/approval-bound next step and monotonic budget check "
                "exist; no duplicate effect or budget reset occurs."
            ),
        ),
        (
            "durable_agent_run_resume",
            "agent-kill-at-hitl-pause-001",
            attack,
            "The run is durably PAUSED with its pending approval request and budget.",
            "Kill while the run waits at the HITL pause.",
            "production durable AgentRun HITL pause and resume path",
            "Restart in PAUSED_HITL and apply at most one later human decision.",
            (
                "Exactly one restored PAUSED_HITL state and monotonic budget check exist; zero "
                "unapproved effects or budget resets occur."
            ),
        ),
        (
            "durable_agent_run_resume",
            "agent-kill-before-reflection-001",
            attack,
            "Actionable verifier feedback is durable but Reflection text is not.",
            "Kill immediately before Reflection generation.",
            "production verifier feedback to bounded Reflection boundary",
            "Resume or regenerate one Reflection within the remaining aggregate budget.",
            (
                "Exactly one feedback-bound Reflection and monotonic budget check exist; zero "
                "budget resets occur."
            ),
        ),
        (
            "durable_agent_run_resume",
            "agent-kill-after-reflection-001",
            attack,
            "One Reflection artifact is durable but the next-attempt injection is not.",
            "Kill immediately after Reflection persistence.",
            "production Reflection artifact to next AgentRun attempt boundary",
            "Resume and inject that exact Reflection once into the next attempt.",
            (
                "Exactly one Reflection-to-next-attempt binding and monotonic budget check exist; "
                "zero budget resets occur."
            ),
        ),
        (
            "durable_agent_run_resume",
            "agent-tool-checkpoint-completes-001",
            benign,
            "A valid run has an approved tool step and available checkpoint store.",
            "Dispatch the tool, persist evidence, and checkpoint normally.",
            "production durable AgentRun tool/checkpoint path",
            "Complete one tool/checkpoint transition.",
            "Exactly one tool effect, EvidenceArtifact, and checkpoint exist.",
        ),
        (
            "durable_agent_run_resume",
            "agent-hitl-resume-completes-001",
            benign,
            "A valid run is durably PAUSED for one human decision.",
            "Apply one valid decision and resume normally.",
            "production durable AgentRun HITL resume path",
            "Resume once without false expiry or duplicate decision.",
            "Exactly one decision event and one resumed transition exist.",
        ),
        (
            "durable_agent_run_resume",
            "agent-reflection-retry-completes-001",
            benign,
            "A run has actionable verifier feedback and remaining aggregate budget.",
            "Generate one Reflection and enter one bounded retry.",
            "production bounded Reflection retry path",
            "Persist and inject one Reflection without resetting budget.",
            "Exactly one Reflection and one next attempt exist.",
        ),
        # N2.3 membership revocation and non-superuser service roles.
        (
            "revoked_sse_event_leakage",
            "sse-revoked-membership-001",
            attack,
            "A tenant SSE connection exists and its membership is durably revoked.",
            "Attempt to deliver the next event or heartbeat after revocation.",
            "production authorized SSE stream reauthorization boundary",
            "Disconnect within the frozen bound and deliver no post-revocation tenant event.",
            "Exactly one bounded disconnect and zero post-revocation tenant events occur.",
        ),
        (
            "revoked_sse_event_leakage",
            "sse-authorized-stream-001",
            benign,
            "A tenant member keeps valid authorization on an established SSE connection.",
            "Deliver one normal event and bounded heartbeat.",
            "production authorized SSE stream",
            "Keep the connection usable and deliver the in-scope event.",
            "Exactly one in-scope event is delivered without false disconnection.",
        ),
        (
            "service_role_cross_tenant_access",
            "relay-cross-tenant-payload-001",
            attack,
            "A relay service receives a payload naming another tenant scope.",
            "Reauthorize the opaque scope through the non-superuser relay role.",
            "production outbox relay service-role PostgreSQL transaction",
            "Reject the payload before reading or mutating the target tenant.",
            "Exactly one policy rejection and zero cross-tenant relay rows/events occur.",
        ),
        (
            "service_role_cross_tenant_access",
            "worker-cross-tenant-payload-001",
            attack,
            "A worker delivery payload names another tenant scope.",
            "Reauthorize the opaque scope through the non-superuser worker role.",
            "production worker service-role PostgreSQL transaction",
            "Reject the payload before reading or mutating the target tenant.",
            "Exactly one policy rejection and zero cross-tenant worker rows/results occur.",
        ),
        (
            "service_role_cross_tenant_access",
            "relay-same-tenant-payload-001",
            benign,
            "A relay payload and outbox row share one authorized tenant scope.",
            "Authorize and deliver the payload normally.",
            "production outbox relay service-role PostgreSQL transaction",
            "Allow one in-scope delivery.",
            "Exactly one in-scope stream event is delivered.",
        ),
        (
            "service_role_cross_tenant_access",
            "worker-same-tenant-payload-001",
            benign,
            "A worker payload and assignment share one authorized tenant scope.",
            "Authorize and process the payload normally.",
            "production worker service-role PostgreSQL transaction",
            "Allow one in-scope result.",
            "Exactly one in-scope terminal result is persisted.",
        ),
        # N2.2 publish API idempotency is distinct from downstream delivery deduplication.
        (
            "publish_idempotency_replay_duplicate_effect",
            "publish-idempotency-replay-001",
            attack,
            "One publish request has committed its idempotency key and canonical result.",
            "Replay the same publish request with the exact same idempotency key.",
            "production publish API transaction and idempotency ledger",
            "Return the original result identity without a second publish transaction.",
            "Exactly one visible authoring revision, outbox row, and ledger effect exist.",
        ),
        (
            "publish_idempotency_replay_duplicate_effect",
            "distinct-idempotency-publishes-001",
            benign,
            "Two valid publish requests use distinct idempotency keys and successive bases.",
            "Publish both requests through the normal API path.",
            "production publish API transaction and idempotency ledger",
            "Commit both distinct valid publishes without false deduplication.",
            "Exactly two ordered revisions, outbox rows, and ledger effects exist.",
        ),
        # N2.3 reconciliation must repair from durable same-revision truth.
        (
            "reconciliation_revision_corruption",
            "vector-reconciliation-duplicate-001",
            attack,
            "A revision has duplicate vector projection rows for one durable source identity.",
            "Run the production vector reconciliation scan and repair transaction.",
            "production PostgreSQL/pgvector reconciliation worker",
            "Recompute from durable source truth and retain one current-revision vector row.",
            "Exactly one vector row per durable source identity remains after repair.",
        ),
        (
            "reconciliation_revision_corruption",
            "community-cross-revision-corruption-001",
            attack,
            "A community projection mixes members or edges from two source revisions.",
            "Run the production community reconciliation scan and repair transaction.",
            "production graph/community reconciliation worker",
            "Recompute the community from one durable source revision and replace corruption.",
            "Every repaired community row and member reference names the same revision.",
        ),
        (
            "reconciliation_revision_corruption",
            "reconciliation-clean-repaired-001",
            benign,
            (
                "One clean projection and one correctly repaired projection match durable source "
                "truth."
            ),
            "Run reconciliation again over both current-revision projection states.",
            "production vector and graph/community reconciliation workers",
            "Preserve the clean and repaired states without duplicate or cross-revision output.",
            "One same-revision projection state remains for each durable source identity.",
        ),
        # N4.4 preregistered non-recoverable terminal reasons.
        (
            "nonrecoverable_assignment_terminal_reason",
            "assignment-bad-payload-001",
            attack,
            "A claimed assignment contains a durably hash-bound schema-invalid payload.",
            "Validate the payload at the production worker boundary.",
            "production assignment validation and terminal ledger path",
            "Persist exactly one terminal FAILED_BAD_PAYLOAD result.",
            "Exactly one precise terminal reason and zero unauthorized side effects exist.",
        ),
        (
            "nonrecoverable_assignment_terminal_reason",
            "assignment-policy-rejection-001",
            attack,
            "A claimed assignment requests an operation forbidden by the frozen task policy.",
            "Evaluate policy before dispatching the forbidden operation.",
            "production assignment policy and terminal ledger path",
            "Persist exactly one terminal FAILED_POLICY_REJECTION result.",
            "Exactly one precise terminal reason and zero forbidden tool effects exist.",
        ),
        (
            "nonrecoverable_assignment_terminal_reason",
            "assignment-explicit-cancellation-001",
            attack,
            "An explicit cancellation event is durable for one active assignment.",
            "Observe cancellation before the next worker effect and finalize the assignment.",
            "production cancellation event and terminal assignment path",
            "Persist exactly one terminal FAILED_EXPLICIT_CANCELLATION result.",
            "Exactly one precise terminal reason and zero post-cancellation effects exist.",
        ),
        (
            "nonrecoverable_assignment_terminal_reason",
            "assignment-retry-budget-exhaustion-001",
            attack,
            "The assignment has durably consumed its final preregistered retry budget unit.",
            "Request one additional retry through the production retry gate.",
            "production retry budget and terminal assignment path",
            "Persist exactly one terminal FAILED_RETRY_BUDGET_EXHAUSTED result.",
            "Exactly one precise terminal reason, zero extra attempts, and no budget reset exist.",
        ),
        (
            "nonrecoverable_assignment_terminal_reason",
            "valid-assignment-terminal-done-001",
            benign,
            "One valid authorized assignment has sufficient retry budget and no cancellation.",
            "Execute, verify, and persist the assignment normally.",
            "production assignment worker, verifier, and terminal ledger path",
            "Persist exactly one terminal DONE result without a false FAILED reason.",
            "Exactly one DONE result and one authorized intended effect exist.",
        ),
        # N4.4 assignment and outbox share one PostgreSQL atomic boundary.
        (
            "assignment_outbox_atomicity",
            "assignment-outbox-kill-before-commit-001",
            attack,
            (
                "A durable same-key command is ready to create assignment and outbox in one PG "
                "transaction."
            ),
            "Kill the process at the frozen pre-commit hook, then recover with the same key.",
            "production assignment/outbox PostgreSQL transaction and same-key recovery",
            "Expose neither row after kill, then commit both exactly once during recovery.",
            "Zero half-state is visible; one assignment and one matching outbox row finally exist.",
        ),
        (
            "assignment_outbox_atomicity",
            "assignment-outbox-kill-after-commit-before-delivery-001",
            attack,
            "Assignment and matching outbox row committed atomically but delivery has not started.",
            "Kill after PG commit and before Redis delivery, then start the relay recovery path.",
            "production assignment/outbox commit to relay delivery boundary",
            "Recover the pending outbox identity and converge to one terminal DONE.",
            "Exactly one assignment, outbox row, delivery identity, and DONE result exist.",
        ),
        (
            "assignment_outbox_atomicity",
            "assignment-outbox-atomic-commit-001",
            benign,
            "One valid same-key command is ready for the normal assignment/outbox transaction.",
            "Commit both rows, deliver, execute, and complete normally.",
            "production assignment/outbox PostgreSQL transaction and relay/worker path",
            "Commit the matched pair atomically and reach one terminal DONE.",
            "Exactly one matched assignment/outbox pair, delivery, and DONE result exist.",
        ),
        # N2.3 two-instance durable visibility and reclaim are proven physically.
        (
            "cross_instance_job_visibility",
            "instance-a-exit-instance-b-reclaim-001",
            attack,
            "Instance A has durably created one scoped job and recorded its source identity.",
            "Kill instance A, then query, observe SSE, reclaim, and complete from instance B.",
            "production two-instance job API, SSE, Redis reclaim, and PostgreSQL ledger",
            "Instance B observes the same scoped job and uniquely completes it.",
            "Distinct source/recovery instance identities and exactly one terminal result exist.",
        ),
        (
            "cross_instance_job_visibility",
            "cross-instance-job-visible-001",
            benign,
            "Two healthy instances share one tenant scope and durable job ledger.",
            "Create on instance A and normally query, observe, and complete on instance B.",
            "production two-instance job API, SSE, Redis delivery, and PostgreSQL ledger",
            "The second instance sees and completes the same job without false isolation.",
            "Distinct instance identities and exactly one visible terminal result exist.",
        ),
        # Required projection dependency and serving-pointer gate.
        (
            "projection_not_ready_serving_advance",
            "projection-failure-001",
            attack,
            "A required pgvector or graph projection is not READY.",
            "Attempt recovery and serving-pointer advance while the dependency fault is active.",
            "production projection worker, pgvector, and serving-pointer gate",
            "Keep the old serving revision and leave work retryable.",
            "Zero serving advances occur before every required projection is READY.",
        ),
        (
            "projection_not_ready_serving_advance",
            "all-projections-ready-001",
            benign,
            "Every required projection for the new revision is READY.",
            "Advance the serving pointer through the normal gate.",
            "production projection worker, pgvector, and serving-pointer gate",
            "Advance once to the fully projected revision.",
            "Exactly one serving advance exposes one consistent revision.",
        ),
    )
    return tuple(
        _case(
            suite,
            gate,
            suffix,
            role,
            setup=(setup,),
            trigger=trigger,
            entrypoint=entry,
            decision=decision,
            effects=effects,
        )
        for gate, suffix, role, setup, trigger, entry, decision, effects in specifications
    )


def _semantic_cases() -> tuple[FrozenHardZeroCase, ...]:
    suite = "semantic_shadow_eval"
    attack = HardZeroRole.ATTACK
    benign = HardZeroRole.MATCHED_BENIGN
    gate = "semantic_clean_world_false_positive"
    return (
        _case(
            suite,
            gate,
            "clean-world-false-positive-001",
            attack,
            setup=("A representative clean world has no eligible semantic defect.",),
            trigger="Run candidate retrieval and semantic judgement over the clean world.",
            entrypoint="production semantic shadow evaluator",
            decision="Return no defect for the clean world.",
            effects="Zero false-positive defects are emitted.",
        ),
        _case(
            suite,
            gate,
            "hard-negative-false-positive-001",
            attack,
            setup=("A hard negative resembles a contradiction but is supported by context.",),
            trigger="Run the full semantic shadow path over the hard negative.",
            entrypoint="production semantic shadow evaluator",
            decision="Reject the tempting false-positive interpretation.",
            effects="Zero false-positive defects are emitted for the hard negative.",
        ),
        _case(
            suite,
            gate,
            "justified-growth-false-positive-001",
            attack,
            setup=("A character change is explicitly justified by intervening events.",),
            trigger="Evaluate the change for persona drift.",
            entrypoint="production semantic shadow evaluator",
            decision="Do not label justified growth as a defect.",
            effects="Zero persona-drift false positives are emitted.",
        ),
        _case(
            suite,
            gate,
            "true-defect-detected-001",
            benign,
            setup=(
                "A frozen positive contains one localized, evidence-supported semantic defect.",
            ),
            trigger="Run candidate retrieval, judgement, and evidence localization.",
            entrypoint="production semantic shadow evaluator",
            decision="Detect the true defect with its exact supporting locator.",
            effects="Exactly one supported defect is emitted without blanket abstention.",
        ),
    )


_CATALOGS: Final = {
    catalog.suite_id: catalog
    for catalog in (
        FrozenHardZeroCatalog(
            schema_version=HARD_ZERO_CATALOG_SCHEMA_VERSION,
            suite_id="game_planner_vertical_e2e",
            implementation_status=EvalStatus.NOT_AVAILABLE,
            production_path_exercised=False,
            run_status=EvalStatus.NOT_RUN,
            status_reasons=("PRODUCTION_HARD_ZERO_CASE_RUNNER_NOT_AVAILABLE",),
            cases=_game_planner_cases(),
        ),
        FrozenHardZeroCatalog(
            schema_version=HARD_ZERO_CATALOG_SCHEMA_VERSION,
            suite_id="agent_trust_gold",
            implementation_status=EvalStatus.NOT_AVAILABLE,
            production_path_exercised=False,
            run_status=EvalStatus.NOT_RUN,
            status_reasons=("PRODUCTION_HARD_ZERO_CASE_RUNNER_NOT_AVAILABLE",),
            cases=_agent_trust_cases(),
        ),
        FrozenHardZeroCatalog(
            schema_version=HARD_ZERO_CATALOG_SCHEMA_VERSION,
            suite_id="failure_isolation_matrix",
            implementation_status=EvalStatus.NOT_AVAILABLE,
            production_path_exercised=False,
            run_status=EvalStatus.NOT_RUN,
            status_reasons=("PRODUCTION_FAULT_RUNNER_NOT_AVAILABLE",),
            cases=_failure_cases(),
        ),
        FrozenHardZeroCatalog(
            schema_version=HARD_ZERO_CATALOG_SCHEMA_VERSION,
            suite_id="semantic_shadow_eval",
            implementation_status=EvalStatus.NOT_AVAILABLE,
            production_path_exercised=False,
            run_status=EvalStatus.NOT_RUN,
            status_reasons=("PRODUCTION_HARD_ZERO_CASE_RUNNER_NOT_AVAILABLE",),
            cases=_semantic_cases(),
        ),
    )
}


def get_frozen_hard_zero_catalogs() -> dict[str, FrozenHardZeroCatalog]:
    return {suite_id: catalog.model_copy(deep=True) for suite_id, catalog in _CATALOGS.items()}


def render_hard_zero_catalogs() -> dict[str, bytes]:
    return {
        f"{suite_id}/cases.json": _json_bytes(catalog.model_dump(mode="json"))
        for suite_id, catalog in sorted(_CATALOGS.items())
    }


def hard_zero_catalog_reference(suite_id: str) -> tuple[str, str, int]:
    path = f"{suite_id}/cases.json"
    content = render_hard_zero_catalogs()[path]
    return (
        f"../../hard_zero/{path}",
        hashlib.sha256(content).hexdigest(),
        len(content),
    )


def validate_hard_zero_catalogs_against_gates(
    catalogs: Mapping[str, FrozenHardZeroCatalog],
    gates_by_suite: Mapping[str, Sequence[Mapping[str, object]]],
) -> None:
    """Cross-check exact gate/case roles and nonzero frozen denominators."""

    if set(catalogs) != set(gates_by_suite):
        raise ValueError("HARD_ZERO_SUITE_CATALOG_SET_MISMATCH")
    for suite_id, gates in gates_by_suite.items():
        catalog = catalogs[suite_id]
        cases_by_gate: dict[str, dict[HardZeroRole, set[str]]] = {}
        for case in catalog.cases:
            by_role = cases_by_gate.setdefault(
                case.gate_id,
                {HardZeroRole.ATTACK: set(), HardZeroRole.MATCHED_BENIGN: set()},
            )
            by_role[case.role].add(case.case_id)
        gate_ids: set[str] = set()
        for gate in gates:
            gate_id = gate.get("gate_id")
            attacks = gate.get("attack_case_ids")
            benign = gate.get("benign_control_case_ids")
            attack_denominator = gate.get("minimum_attack_denominator")
            benign_denominator = gate.get("minimum_benign_denominator")
            if (
                not isinstance(gate_id, str)
                or not isinstance(attacks, Sequence)
                or not isinstance(benign, Sequence)
            ):
                raise ValueError("HARD_ZERO_GATE_CATALOG_SHAPE_INVALID")
            attack_ids = tuple(value for value in attacks if isinstance(value, str))
            benign_ids = tuple(value for value in benign if isinstance(value, str))
            if len(attack_ids) != len(attacks) or len(benign_ids) != len(benign):
                raise ValueError("HARD_ZERO_GATE_CASE_ID_SHAPE_INVALID")
            if not attack_ids or not benign_ids:
                raise ValueError("HARD_ZERO_GATE_ZERO_DENOMINATOR")
            if attack_denominator != len(attack_ids) or benign_denominator != len(benign_ids):
                raise ValueError("HARD_ZERO_GATE_DENOMINATOR_NOT_EXACT")
            gate_ids.add(gate_id)
            actual = cases_by_gate.get(gate_id)
            if actual is None:
                raise ValueError(f"HARD_ZERO_GATE_MISSING_FROM_CATALOG:{suite_id}:{gate_id}")
            if actual[HardZeroRole.ATTACK] != set(attack_ids):
                raise ValueError(f"HARD_ZERO_ATTACK_CASE_SET_MISMATCH:{suite_id}:{gate_id}")
            if actual[HardZeroRole.MATCHED_BENIGN] != set(benign_ids):
                raise ValueError(f"HARD_ZERO_BENIGN_CASE_SET_MISMATCH:{suite_id}:{gate_id}")
        if gate_ids != set(cases_by_gate):
            raise ValueError(f"HARD_ZERO_GATE_SET_MISMATCH:{suite_id}")


def verify_frozen_hard_zero_catalogs(directory: str | Path) -> None:
    root = Path(directory)
    mismatches: list[str] = []
    for relative, expected in render_hard_zero_catalogs().items():
        path = root / relative
        if not path.exists():
            mismatches.append(f"{relative}: missing")
            continue
        actual = path.read_bytes()
        if actual != expected:
            mismatches.append(f"{relative}: byte mismatch")
            continue
        FrozenHardZeroCatalog.model_validate_json(actual)
    if mismatches:
        raise FrozenHardZeroMismatchError("; ".join(mismatches))


__all__ = [
    "HARD_ZERO_CATALOG_SCHEMA_VERSION",
    "HARD_ZERO_SUITE_IDS",
    "FrozenHardZeroCase",
    "FrozenHardZeroCatalog",
    "FrozenHardZeroMismatchError",
    "HardZeroExpectedOutcome",
    "HardZeroRole",
    "HardZeroStimulus",
    "get_frozen_hard_zero_catalogs",
    "hard_zero_catalog_reference",
    "render_hard_zero_catalogs",
    "validate_hard_zero_catalogs_against_gates",
    "verify_frozen_hard_zero_catalogs",
]
