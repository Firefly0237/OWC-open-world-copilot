"""Persistent author intent/review actions; no generation, main-candidate publish or canon write."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...content.hash import content_hash
from ...content.store import ContentStore
from ...patches.models import PatchCandidate, PatchOp
from ...pipeline.project import ProjectContext
from ...storage.world_lock import world_write_lock
from ...workflow.intent_models import IntentContract, IntentFieldLock, IntentFormalRule
from ...workflow.intent_review import (
    intent_baseline_bundle,
    resolve_intent_path,
    review_intent_patch,
    validate_intent_rule,
)
from ...workflow.intent_store import IntentActorKind, IntentStore, SourceValidator
from ...workflow.models import Artifact, TaskRecord, new_id
from .workflow import artifact_is_stale, open_workflow_store


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IntentContractInput(_Input):
    expected_revision: int = Field(ge=1, strict=True)
    baseline_artifact_id: str = Field(min_length=1)
    baseline_artifact_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    allowed_paths: list[str] = Field(min_length=1, max_length=100)
    hard_locks: list[IntentFieldLock] = Field(default_factory=list, max_length=100)
    formal_rules: list[IntentFormalRule] = Field(default_factory=list, max_length=30)
    soft_text: list[str] = Field(default_factory=list, max_length=30)
    target_error_fingerprints: list[str] = Field(default_factory=list, max_length=30)
    modification_goal: str = Field(default="", max_length=8000)
    supersedes_contract_id: str | None = None
    expected_parent_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    change_reason: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def explicit_goal_change(self) -> Self:
        if self.supersedes_contract_id and (
            not self.change_reason.strip() or not self.expected_parent_hash
        ):
            raise ValueError("改变意图需要supersedes、父合同hash与change_reason")
        if not self.target_error_fingerprints and not self.modification_goal.strip():
            raise ValueError("必须提供目标错误或修改目标原话")
        return self


class _IntentBoundInput(_Input):
    expected_revision: int = Field(ge=1, strict=True)
    expected_contract_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class IntentConfirmInput(_IntentBoundInput):
    actor_kind: IntentActorKind = "author"


class IntentPatchOperationInput(_Input):
    op: PatchOp
    path: str
    value: Any = None


class IntentCandidateInput(_Input):
    ops: list[IntentPatchOperationInput] = Field(min_length=1, max_length=16)
    rationale: str = Field(default="", max_length=4000)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=30)


class IntentCandidateReviewInput(_IntentBoundInput):
    candidate: IntentCandidateInput


class IntentAuthorDecisionInput(_Input):
    expected_revision: int = Field(ge=1, strict=True)
    expected_review_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["accepted", "rejected"]
    note: str = Field(default="", max_length=4000)
    actor_kind: IntentActorKind = "author"


def _store(root: str | Path) -> IntentStore:
    return IntentStore(open_workflow_store(root))


def _source_validator(root: str | Path) -> SourceValidator:
    def validate(task: TaskRecord, artifact: Artifact, contract: IntentContract) -> None:
        if artifact_is_stale(task, artifact):
            raise ValueError("基线产物已经过期，不能确认或接受旧来源")
        frozen = intent_baseline_bundle(task, artifact)
        current = ContentStore(root).load_scoped(
            world_id=task.request.world_id, version=task.base_version
        )
        if content_hash(current) != task.base_hash:
            raise ValueError("项目基线已改变；旧合同不可继续确认或接受")
        for path in [*contract.allowed_paths, *(lock.path for lock in contract.hard_locks)]:
            try:
                resolve_intent_path(frozen, path)
            except (KeyError, IndexError) as exc:
                raise ValueError("意图范围或硬字段锁不存在于来源快照：" + path) from exc
        for rule in contract.formal_rules:
            validate_intent_rule(frozen, rule)

    return validate


def create_intent_contract_action(
    root: str | Path,
    project: str,
    task_id: str,
    request: IntentContractInput,
) -> dict[str, Any]:
    with world_write_lock(root):
        excluded = {
            "expected_revision",
            "supersedes_contract_id",
            "expected_parent_hash",
            "change_reason",
        }
        return _store(root).propose(
            project,
            task_id,
            expected_revision=request.expected_revision,
            fields=request.model_dump(mode="json", exclude=excluded),
            supersedes_contract_id=request.supersedes_contract_id,
            expected_parent_hash=request.expected_parent_hash,
            change_reason=request.change_reason,
            validate=_source_validator(root),
        )


def confirm_intent_contract_action(
    root: str | Path,
    project: str,
    contract_id: str,
    request: IntentConfirmInput,
) -> dict[str, Any]:
    with world_write_lock(root):
        return _store(root).confirm(
            project,
            contract_id,
            expected_revision=request.expected_revision,
            expected_contract_hash=request.expected_contract_hash,
            validate=_source_validator(root),
            actor_kind=request.actor_kind,
        )


def list_intent_contracts_action(root: str | Path, project: str, task_id: str) -> dict[str, Any]:
    return {"contracts": _store(root).list_contracts(project, task_id)}


def get_intent_contract_action(root: str | Path, project: str, contract_id: str) -> dict[str, Any]:
    return _store(root).get_contract(project, contract_id)


def create_intent_candidate_review_action(
    root: str | Path,
    project: str,
    contract_id: str,
    request: IntentCandidateReviewInput,
) -> dict[str, Any]:
    with world_write_lock(root):
        store = _store(root)
        validate = _source_validator(root)
        contract, task, baseline = store.review_source(
            project,
            contract_id,
            expected_revision=request.expected_revision,
            expected_contract_hash=request.expected_contract_hash,
            validate=validate,
        )
        candidate = PatchCandidate.model_validate(
            {
                **request.candidate.model_dump(mode="json"),
                "id": new_id(),
            }
        )
        # ProjectContext is a real production audit composition; it is not a context manager.
        context = ProjectContext.open(
            root, world_id=task.request.world_id, version=task.base_version
        )
        try:
            if content_hash(context.bundle) != task.base_hash:
                raise ValueError("审计上下文与冻结项目不一致")
            report = review_intent_patch(
                contract,
                task=task,
                baseline=baseline,
                candidate=candidate,
                runner=context.audit_runner,
            )
        finally:
            context.close()
        return store.save_review(
            project,
            contract,
            expected_contract_hash=request.expected_contract_hash,
            candidate=candidate,
            report=report,
            validate=validate,
        )


def get_intent_candidate_review_action(
    root: str | Path, project: str, review_id: str
) -> dict[str, Any]:
    return _store(root).get_review(project, review_id)


def decide_intent_candidate_action(
    root: str | Path,
    project: str,
    review_id: str,
    request: IntentAuthorDecisionInput,
) -> dict[str, Any]:
    with world_write_lock(root):
        return _store(root).decide(
            project,
            review_id,
            expected_revision=request.expected_revision,
            expected_review_hash=request.expected_review_hash,
            decision=request.decision,
            note=request.note,
            validate=_source_validator(root),
            actor_kind=request.actor_kind,
        )
