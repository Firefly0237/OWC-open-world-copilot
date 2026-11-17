"""Source-bound requirements and deterministic checks over actual narrative outputs.

Structured author input can establish a machine obligation. Model interpretations
and unrestricted prose cannot. Business-role declarations bind identities and
sources; their agreement does not prove the meaning of generated dialogue.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from ..content.hash import content_hash
from ..content.models import ContentBundle, DialogueTree, Quest
from ..patches.pointer import pointer_parts
from .models import EventPlan, TaskRecord

Scenario = Literal["single_writer", "idea", "reference", "story_to_playable", "local_repair"]
RequirementAuthority = Literal["author_input", "development_input", "model_proposal"]
BusinessRole = Literal[
    "participant", "supplier", "debtor", "creditor", "buyer", "seller", "beneficiary"
]
CheckStatus = Literal["passed", "failed", "unknown"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RequirementSourceBinding(_Model):
    id: str = Field(min_length=1, max_length=200)
    kind: Literal[
        "author_input",
        "development_input",
        "model_proposal",
        "project_entity",
        "reference_excerpt",
        "draft_artifact",
    ]
    source_id: str = Field(min_length=1, max_length=400)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    locator: str = Field(default="", max_length=2000)


class BusinessRoleBinding(_Model):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,79}$")
    role: BusinessRole
    entity_id: str = Field(min_length=1, max_length=200)
    source_refs: list[str] = Field(min_length=1, max_length=20)

    @field_validator("source_refs")
    @classmethod
    def distinct_sources(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(not value.strip() for value in values):
            raise ValueError("业务角色的来源引用必须非空且不能重复")
        return values


class BusinessRelationBinding(_Model):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,79}$")
    predicate: Literal["supplies_to", "owes", "purchases_from"]
    subject_entity_id: str = Field(min_length=1, max_length=200)
    object_entity_id: str = Field(min_length=1, max_length=200)
    source_refs: list[str] = Field(min_length=1, max_length=20)


class BusinessTextEvidence(_Model):
    """A literal span in this role's actual output, with a separately stated interpretation."""

    path: str = Field(min_length=1, max_length=1000)
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)
    quote: str = Field(min_length=1, max_length=4000)
    interpretation: Literal["explicit", "ambiguous"] = "ambiguous"

    @model_validator(mode="after")
    def anchored_range(self) -> BusinessTextEvidence:
        if not self.path.startswith("/") or self.start >= self.end:
            raise ValueError("正文证据需合法JSON Pointer及非空字符范围")
        pointer_parts(self.path)
        return self


class BusinessRoleClaim(_Model):
    binding_id: str = Field(min_length=1, max_length=80)
    role: BusinessRole
    entity_id: str = Field(min_length=1, max_length=200)
    source_refs: list[str] = Field(min_length=1, max_length=20)
    text_evidence: list[BusinessTextEvidence] = Field(default_factory=list, max_length=20)


class BusinessRelationClaim(_Model):
    binding_id: str = Field(min_length=1, max_length=80)
    predicate: Literal["supplies_to", "owes", "purchases_from"]
    subject_entity_id: str = Field(min_length=1, max_length=200)
    object_entity_id: str = Field(min_length=1, max_length=200)
    source_refs: list[str] = Field(min_length=1, max_length=20)
    text_evidence: list[BusinessTextEvidence] = Field(default_factory=list, max_length=20)


class BusinessRoleClaims(_Model):
    """Actual model output, never populated by copying the expected author bindings."""

    schema_version: Literal[1] = 1
    assignments: list[BusinessRoleClaim] = Field(default_factory=list, max_length=60)
    relations: list[BusinessRelationClaim] = Field(default_factory=list, max_length=60)

    @model_validator(mode="after")
    def unique_bindings(self) -> BusinessRoleClaims:
        ids = [row.binding_id for row in self.assignments]
        if len(ids) != len(set(ids)):
            raise ValueError("实际业务角色声明包含重复binding_id")
        relation_ids = [row.binding_id for row in self.relations]
        if len(relation_ids) != len(set(relation_ids)):
            raise ValueError("实际业务关系声明包含重复binding_id")
        return self


class MachineRequirements(_Model):
    schema_version: Literal[1] = 1
    stage_count: int | None = Field(default=None, ge=1, le=64, strict=True)
    decision_count: int | None = Field(default=None, ge=0, le=10, strict=True)
    decision_option_count: int | None = Field(default=None, ge=2, le=6, strict=True)
    distinct_outcomes: StrictBool | None = None
    participant_ids: list[str] = Field(default_factory=list, max_length=60)
    game_state_variables: list[str] = Field(default_factory=list, max_length=100)
    business_roles: list[BusinessRoleBinding] = Field(default_factory=list, max_length=60)
    business_relations: list[BusinessRelationBinding] = Field(default_factory=list, max_length=60)

    @model_validator(mode="after")
    def coherent(self) -> MachineRequirements:
        if len(self.participant_ids) != len(set(self.participant_ids)) or any(
            not value.strip() for value in self.participant_ids
        ):
            raise ValueError("显式参与者必须非空且不能重复")
        if len(self.game_state_variables) != len(set(self.game_state_variables)) or any(
            not key.strip() for key in self.game_state_variables
        ):
            raise ValueError("显式游戏状态变量须非空且不能重复")
        ids = [row.id for row in self.business_roles]
        if len(ids) != len(set(ids)):
            raise ValueError("业务角色binding id不能重复")
        relation_ids = [row.id for row in self.business_relations]
        if len(relation_ids) != len(set(relation_ids)):
            raise ValueError("业务关系binding id不能重复")
        if self.decision_count == 0 and (
            self.decision_option_count is not None or self.distinct_outcomes is True
        ):
            raise ValueError("零决策与选项数量／不同后果要求冲突")
        return self


class RequirementContract(_Model):
    schema_version: Literal[1] = 1
    project: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    task_revision: int = Field(ge=1, strict=True)
    base_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    scenario: Scenario
    use_case_ids: list[str] = Field(default_factory=list, max_length=80)
    authority: RequirementAuthority
    confirmed: StrictBool = False
    machine: MachineRequirements = Field(default_factory=MachineRequirements)
    original_brief: str
    original_protected: list[str] = Field(default_factory=list)
    free_text: list[str] = Field(default_factory=list)
    sources: list[RequirementSourceBinding] = Field(min_length=3, max_length=200)
    source_binding_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    contract_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("use_case_ids")
    @classmethod
    def stable_use_cases(cls, values: list[str]) -> list[str]:
        allowed = {f"UC-{index:03d}" for index in range(1, 81)}
        if len(values) != len(set(values)) or set(values) - allowed:
            raise ValueError("须沿用UC-001至UC-080的既有稳定编号，不能自创或重新编号")
        return values

    @model_validator(mode="after")
    def verified(self) -> RequirementContract:
        if self.confirmed and self.authority == "model_proposal":
            raise ValueError("模型解释不能自行成为作者确认的硬要求")
        ids = [source.id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("需求来源binding id不能重复")
        known = set(ids)
        for refs in [
            *(row.source_refs for row in self.machine.business_roles),
            *(row.source_refs for row in self.machine.business_relations),
        ]:
            if set(refs) - known:
                raise ValueError("业务角色引用了未绑定的来源")
        if self.scenario == "reference" and not any(
            source.kind == "reference_excerpt" for source in self.sources
        ):
            raise ValueError("参考场景需要绑定真实参考片段，不能回退成无来源idea")
        if self.scenario == "local_repair" and not any(
            source.kind == "draft_artifact" for source in self.sources
        ):
            raise ValueError("局部修复需要绑定真实基线草稿，不能回退成从零创作")
        if self.source_binding_hash != content_hash(self.sources):
            raise ValueError("需求来源binding hash不匹配")
        if self.contract_hash != content_hash(
            self.model_dump(mode="json", exclude={"contract_hash"})
        ):
            raise ValueError("需求合同hash不匹配")
        return self


class RequirementCheck(_Model):
    key: str
    status: CheckStatus
    blocking: StrictBool
    scope: Literal["structure", "binding", "declared_default_state", "free_text"]
    reason: str
    expected: Any = None
    observed: Any = None
    evidence_paths: list[str] = Field(default_factory=list)


class RequirementsReport(_Model):
    schema_version: Literal[1] = 1
    contract_hash: str
    operation: str
    status: CheckStatus
    machine_status: CheckStatus
    can_continue: StrictBool
    checks: list[RequirementCheck]
    observed_hashes: dict[str, str] = Field(default_factory=dict)
    semantic_status: Literal["unknown"] = "unknown"
    author_satisfaction: Literal["unknown"] = "unknown"
    limitations: list[str] = Field(
        default_factory=lambda: [
            "自由文本完整保留为未知，不进行词法猜测或自动确认",
            "业务角色声明一致仅证明结构身份与来源一致，不证明对白文学含义",
            "后果差异限已声明默认初态，不能外推全路径可达或任意初态",
        ]
    )


_OPERATIONS = {
    "plan_events",
    "draft_quest",
    "draft_logic",
    "logic_contract",
    "draft_dialogue",
    "dialogue_constrained",
    "validate_decisions",
    "bind_dialogue",
    "coverage_audit",
    "single_writer",
}
_PLAN_OPS = {"plan_events", "validate_decisions", "coverage_audit"}
_QUEST_OPS = {
    "draft_quest",
    "single_writer",
    "draft_logic",
    "logic_contract",
    "bind_dialogue",
    "coverage_audit",
}
_LOGIC_OPS = {
    "draft_logic",
    "logic_contract",
    "validate_decisions",
    "bind_dialogue",
    "coverage_audit",
}
_TREE_OPS = {"draft_dialogue", "dialogue_constrained", "bind_dialogue", "coverage_audit"}


def _plain(value: Any) -> Any:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


def _verified(value: RequirementContract | Mapping[str, Any]) -> RequirementContract:
    return RequirementContract.model_validate(_plain(value))


def build_requirements(
    task: TaskRecord,
    machine: MachineRequirements | Mapping[str, Any] | None = None,
    *,
    scenario: Scenario = "idea",
    authority: RequirementAuthority = "author_input",
    confirmed: bool = False,
    sources: Sequence[RequirementSourceBinding | Mapping[str, Any]] = (),
    use_case_ids: Sequence[str] = (),
    free_text: Sequence[str] = (),
) -> RequirementContract:
    """The HTTP host supplies authority; neither LLM output nor reference text grants it."""
    requirements = MachineRequirements.model_validate(
        _plain(machine) if machine is not None else {}
    )
    base = task.base_bundle
    if content_hash(ContentBundle.model_validate(base)) != task.base_hash:
        raise ValueError("需求绑定的任务基线hash不匹配")
    entities = base.get("entities", {})
    needed = (
        set(requirements.participant_ids)
        | {binding.entity_id for binding in requirements.business_roles}
        | {
            entity
            for relation in requirements.business_relations
            for entity in (relation.subject_entity_id, relation.object_entity_id)
        }
    )
    if needed - set(entities):
        raise ValueError(
            "显式参与者／业务角色必须引用已绑定项目实体："
            + ",".join(sorted(needed - set(entities)))
        )
    own = [
        RequirementSourceBinding(
            id="author_brief",
            kind="author_input",
            source_id=task.id,
            locator="/request/brief",
            content_hash=content_hash(task.request.brief),
        ),
        RequirementSourceBinding(
            id="author_protected",
            kind="author_input",
            source_id=task.id,
            locator="/request/protected",
            content_hash=content_hash(task.request.protected),
        ),
        RequirementSourceBinding(
            id="author_structured",
            kind=authority,
            source_id=f"{task.id}:{task.revision}",
            locator="/machine",
            content_hash=content_hash(requirements),
        ),
    ]
    own.extend(
        RequirementSourceBinding(
            id="entity:" + entity_id,
            kind="project_entity",
            source_id=entity_id,
            locator="/entities/" + entity_id.replace("~", "~0").replace("/", "~1"),
            content_hash=content_hash(entities[entity_id]),
        )
        for entity_id in sorted(needed)
    )
    bindings = [*own, *(RequirementSourceBinding.model_validate(_plain(row)) for row in sources)]
    payload = {
        "schema_version": 1,
        "project": task.project,
        "task_id": task.id,
        "task_revision": task.revision,
        "base_hash": task.base_hash,
        "request_hash": content_hash(task.request),
        "scenario": scenario,
        "use_case_ids": list(use_case_ids),
        "authority": authority,
        "confirmed": confirmed,
        "machine": requirements.model_dump(mode="json"),
        "original_brief": task.request.brief,
        "original_protected": task.request.protected,
        "free_text": [task.request.brief, *task.request.protected, *free_text],
        "sources": [row.model_dump(mode="json") for row in bindings],
        "source_binding_hash": content_hash(bindings),
    }
    return RequirementContract.model_validate({**payload, "contract_hash": content_hash(payload)})


def verify_requirement_binding(
    contract: RequirementContract | Mapping[str, Any], task: TaskRecord
) -> RequirementContract:
    value = _verified(contract)
    if (
        value.project != task.project
        or value.task_id != task.id
        or value.task_revision != task.revision
        or value.base_hash != task.base_hash
        or value.request_hash != content_hash(task.request)
        or value.original_brief != task.request.brief
        or value.original_protected != task.request.protected
    ):
        raise ValueError("需求合同不属于当前项目／任务／版本／请求")
    return value


def compile_role_requirements(
    contract: RequirementContract | Mapping[str, Any], operation: str
) -> dict[str, Any]:
    value = _verified(contract)
    if operation not in _OPERATIONS:
        raise ValueError("未支持的需求编译角色：" + operation)
    return {
        "schema_version": 1,
        "contract_hash": value.contract_hash,
        "operation": operation,
        "scenario": value.scenario,
        "use_case_ids": value.use_case_ids,
        "author_confirmed": value.confirmed,
        "authority": value.authority,
        "machine": value.machine.model_dump(mode="json"),
        "machine_enforcement": value.confirmed and value.authority != "model_proposal",
        "original_brief": value.original_brief,
        "original_protected": value.original_protected,
        "free_text": [{"text": text, "status": "unknown"} for text in value.free_text],
        "sources": [source.model_dump(mode="json") for source in value.sources],
        "source_binding_hash": value.source_binding_hash,
        "business_role_claims_schema": BusinessRoleClaims.model_json_schema(),
        "business_role_output": (
            "模型在business_role_claims中独立声明实际角色及来源；Quest也可存入"
            "metadata.requirements_v2.business_role_claims。不能由host复制期望伪造满足。"
        ),
        "unsupported_policy": "fail_closed_without_fallback",
    }


def _report(
    contract: RequirementContract, operation: str, checks: list[RequirementCheck], observed: dict
) -> RequirementsReport:
    hard = [check for check in checks if check.blocking]
    machine_status: CheckStatus = (
        "failed"
        if any(check.status == "failed" for check in hard)
        else "unknown"
        if not contract.confirmed or any(check.status == "unknown" for check in hard)
        else "passed"
    )
    return RequirementsReport(
        contract_hash=contract.contract_hash,
        operation=operation,
        status="failed"
        if machine_status == "failed"
        else "unknown"
        if any(check.status == "unknown" for check in checks)
        else "passed",
        machine_status=machine_status,
        can_continue=all(check.status == "passed" for check in hard),
        checks=checks,
        observed_hashes={
            key: content_hash(_plain(item)) for key, item in observed.items() if item is not None
        },
    )


def _check(
    key: str,
    status: CheckStatus,
    *,
    reason: str,
    expected: Any = None,
    observed: Any = None,
    scope: Literal["structure", "binding", "declared_default_state", "free_text"] = "structure",
    blocking: bool = True,
    paths: Sequence[str] = (),
) -> RequirementCheck:
    return RequirementCheck(
        key=key,
        status=status,
        blocking=blocking,
        scope=scope,
        reason=reason,
        expected=expected,
        observed=observed,
        evidence_paths=list(paths),
    )


def _business_checks(
    contract: RequirementContract, claims: Any, *, required: bool, actual_outputs: Mapping[str, Any]
) -> list[RequirementCheck]:
    expected = {row.id: row for row in contract.machine.business_roles}
    expected_relations = {row.id: row for row in contract.machine.business_relations}
    if not expected and not expected_relations:
        return []
    if claims is None:
        return [
            _check(
                "business_roles",
                "unknown",
                scope="binding",
                blocking=required,
                reason="实际模型输出缺少业务角色声明，不能复制期望值当作已满足",
                expected=[*expected, *expected_relations],
                paths=["/business_role_claims"],
            )
        ]
    try:
        actual = BusinessRoleClaims.model_validate(_plain(claims))
    except ValueError as exc:
        return [_check("business_roles", "failed", scope="binding", reason=str(exc))]
    observed = {row.binding_id: row for row in actual.assignments}
    correct = set(observed) == set(expected) and all(
        observed[key].role == item.role
        and observed[key].entity_id == item.entity_id
        and set(observed[key].source_refs) == set(item.source_refs)
        for key, item in expected.items()
        if key in observed
    )
    checks = [
        _check(
            "business_roles",
            "passed" if correct else "failed",
            scope="binding",
            reason="核对实际声明的身份／业务角色／来源，不推断对白语义",
            expected=[row.model_dump(mode="json") for row in expected.values()],
            observed=actual.model_dump(mode="json"),
            paths=["/business_role_claims/assignments"],
        )
    ]
    relations = {row.binding_id: row for row in actual.relations}
    if expected_relations or relations:
        correct_relations = set(relations) == set(expected_relations) and all(
            relations[key].predicate == item.predicate
            and relations[key].subject_entity_id == item.subject_entity_id
            and relations[key].object_entity_id == item.object_entity_id
            and set(relations[key].source_refs) == set(item.source_refs)
            for key, item in expected_relations.items()
            if key in relations
        )
        checks.append(
            _check(
                "business_relations",
                "passed" if correct_relations else "failed",
                scope="binding",
                reason="按实际生成关系的主语／谓词／宾语和来源查验，欠款或供货方向不能交换",
                expected=[row.model_dump(mode="json") for row in expected_relations.values()],
                observed=[row.model_dump(mode="json") for row in actual.relations],
                paths=["/business_role_claims/relations"],
            )
        )
    all_claims: list[BusinessRoleClaim | BusinessRelationClaim] = [
        *actual.assignments,
        *actual.relations,
    ]
    for claim in all_claims:
        if not claim.text_evidence:
            checks.append(
                _check(
                    "business_text:" + claim.binding_id,
                    "unknown",
                    scope="binding",
                    reason="业务身份声明必须定位实际生成正文，不能仅复述期望元数据",
                )
            )
        for index, evidence in enumerate(claim.text_evidence):
            try:
                target: Any = actual_outputs
                for part in pointer_parts(evidence.path):
                    target = target[int(part)] if isinstance(target, list) else target[part]
                if (
                    not isinstance(target, str)
                    or evidence.end > len(target)
                    or target[evidence.start : evidence.end] != evidence.quote
                ):
                    raise ValueError("正文证据quote／字符范围与实际输出不一致")
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                checks.append(
                    _check(
                        f"business_text:{claim.binding_id}:{index}",
                        "failed",
                        scope="binding",
                        reason=str(exc),
                        paths=[evidence.path],
                    )
                )
            else:
                checks.append(
                    _check(
                        f"business_text:{claim.binding_id}:{index}",
                        "passed",
                        scope="binding",
                        reason="实际正文锚点匹配；该句是否蕴涵业务关系仍是模型解释而非形式证明",
                        observed=evidence.model_dump(mode="json"),
                        paths=[evidence.path],
                    )
                )
                checks.append(
                    _check(
                        f"business_meaning:{claim.binding_id}:{index}",
                        "unknown",
                        scope="free_text",
                        blocking=False,
                        reason="正文到业务角色的语义解释未获独立证明：" + evidence.interpretation,
                        observed=evidence.quote,
                    )
                )
    return checks


def check_requirement_outputs(
    contract: RequirementContract | Mapping[str, Any],
    *,
    plan: EventPlan | Mapping[str, Any] | None = None,
    quest: Quest | Mapping[str, Any] | None = None,
    tree: DialogueTree | Mapping[str, Any] | None = None,
    decisions: Any = None,
    choices: Any = None,
    business_claims: Any = None,
    operation: str = "final",
) -> RequirementsReport:
    """Check actual typed outputs; omitted required evidence stays unknown and blocks."""
    value = _verified(contract)
    if operation != "final" and operation not in _OPERATIONS:
        raise ValueError("未支持的需求检查角色：" + operation)
    raw = {
        "plan": plan,
        "quest": quest,
        "tree": tree,
        "decisions": decisions,
        "choices": choices,
        "business_role_claims": business_claims,
    }
    checks = [
        _check("free_text:" + str(index), "unknown", reason=text, scope="free_text", blocking=False)
        for index, text in enumerate(value.free_text)
    ]
    if not value.confirmed:
        checks.append(
            _check(
                "machine_confirmation",
                "unknown",
                scope="binding",
                blocking=False,
                reason="结构化内容尚未由作者确认，仅作为候选解释，不能宣称需求已满足",
                observed=value.authority,
            )
        )
        return _report(value, operation, checks, raw)
    machine = value.machine
    parsed_plan = EventPlan.model_validate(_plain(plan)) if plan is not None else None
    parsed_quest = Quest.model_validate(_plain(quest)) if quest is not None else None
    parsed_tree = DialogueTree.model_validate(_plain(tree)) if tree is not None else None
    full = operation == "final"
    want_plan = (
        full or operation in _PLAN_OPS or operation in {"logic_contract", "dialogue_constrained"}
    )
    want_quest = full or operation in _QUEST_OPS
    want_logic = full or operation in _LOGIC_OPS
    want_tree = full or operation in _TREE_OPS
    if decisions is not None and machine.game_state_variables:
        selectors = set(_plain(decisions).get("selector_variables", []))
        overlap = selectors & set(machine.game_state_variables)
        checks.append(
            _check(
                "protected_game_state",
                "failed" if overlap else "passed",
                scope="binding",
                reason="作者显式游戏状态不能被模型重新分类为控制selector以规避后果检查",
                expected=machine.game_state_variables,
                observed=sorted(overlap),
                paths=["/decisions/selector_variables"],
            )
        )
    if machine.stage_count is not None and want_quest:
        checks.append(
            _check(
                "stage_count",
                "unknown"
                if parsed_quest is None
                else "passed"
                if len(parsed_quest.stages) == machine.stage_count
                else "failed",
                reason="核对真实Quest.stages数量，不使用模型声明的计数",
                expected=machine.stage_count,
                observed=len(parsed_quest.stages) if parsed_quest is not None else None,
                paths=["/quest/stages"],
            )
        )
    if want_plan:
        if machine.decision_count is not None:
            checks.append(
                _check(
                    "decision_count",
                    "unknown"
                    if parsed_plan is None
                    else "passed"
                    if len(parsed_plan.decisions) == machine.decision_count
                    else "failed",
                    reason="核对实际EventPlan.decisions数量",
                    expected=machine.decision_count,
                    observed=len(parsed_plan.decisions) if parsed_plan is not None else None,
                    paths=["/plan/decisions"],
                )
            )
        if machine.decision_option_count is not None:
            sizes = (
                [len(row.options) for row in parsed_plan.decisions]
                if parsed_plan is not None
                else None
            )
            good = (
                sizes is not None
                and bool(sizes)
                and all(size == machine.decision_option_count for size in sizes)
            )
            checks.append(
                _check(
                    "decision_option_count",
                    "unknown" if sizes is None else "passed" if good else "failed",
                    reason="逐项核对实际决策选项，空决策不能以真空条件通过",
                    expected=machine.decision_option_count,
                    observed=sizes,
                    paths=["/plan/decisions/*/options"],
                )
            )
        if machine.participant_ids:
            actual = sorted(set(parsed_plan.participant_ids)) if parsed_plan is not None else None
            checks.append(
                _check(
                    "participants",
                    "unknown"
                    if actual is None
                    else "passed"
                    if set(actual) == set(machine.participant_ids)
                    else "failed",
                    reason="参与者须使用作者明确的项目实体，不按人物名称词法猜测",
                    expected=sorted(machine.participant_ids),
                    observed=actual,
                    scope="binding",
                    paths=["/plan/participant_ids"],
                )
            )
    if machine.participant_ids and parsed_tree is not None and want_tree:
        actors = set(parsed_tree.participants) | {
            node.speaker_id for node in parsed_tree.nodes.values() if node.speaker_id is not None
        }
        checks.append(
            _check(
                "dialogue_participants",
                "passed" if actors.issubset(machine.participant_ids) else "failed",
                reason="对白显式参与者／说话者不能超出确认名单；不判断正文暗含人物",
                expected=machine.participant_ids,
                observed=sorted(actors),
                scope="binding",
                paths=["/tree/participants", "/tree/nodes/*/speaker_id"],
            )
        )
    if (
        machine.distinct_outcomes is True
        and want_logic
        and not (operation == "bind_dialogue" and parsed_plan is None)
    ):
        if parsed_plan is None or parsed_quest is None or decisions is None:
            checks.append(
                _check(
                    "distinct_outcomes",
                    "unknown",
                    scope="declared_default_state",
                    reason="缺少实际plan／QuestLogic／决策效果映射，不能用不同结局名称替代后果",
                )
            )
        else:
            from .decision_contract import validate_decisions

            try:
                mapping = _plain(decisions)
                if mapping.get("schema_version") != 2 or not mapping.get("selector_variables"):
                    raise ValueError("严格不同后果要求v2映射及显式selector_variables，禁止回退v1")
                if not parsed_plan.decisions:
                    raise ValueError("没有真实决策，不能宣称选项有不同后果")
                result = validate_decisions(
                    parsed_plan, parsed_quest, mapping, require_distinct=True
                )
            except ValueError as exc:
                checks.append(
                    _check(
                        "distinct_outcomes",
                        "failed",
                        scope="declared_default_state",
                        reason=str(exc),
                    )
                )
            else:
                checks.append(
                    _check(
                        "distinct_outcomes",
                        "passed",
                        scope="declared_default_state",
                        reason="正式分支检查排除显式控制selector后核对真实后果",
                        expected=True,
                        observed={"contract_hash": result["contract_hash"]},
                        paths=["/quest/logic/branches/*/effects", "/decisions/selector_variables"],
                    )
                )
    if machine.decision_option_count is not None and (
        full or operation in {"bind_dialogue", "coverage_audit"}
    ):
        if (
            parsed_plan is None
            or parsed_quest is None
            or parsed_tree is None
            or decisions is None
            or choices is None
        ):
            checks.append(
                _check(
                    "playable_option_coverage",
                    "unknown",
                    blocking=full or operation == "coverage_audit",
                    reason="需真实plan／quest／tree及两种映射，不能按选项标签或总数猜测覆盖",
                )
            )
        else:
            from .decision_contract import validate_choice_coverage

            try:
                result = validate_choice_coverage(
                    parsed_plan,
                    parsed_quest,
                    parsed_tree,
                    _plain(decisions),
                    _plain(choices),
                    require_distinct=machine.distinct_outcomes is True,
                )
            except ValueError as exc:
                checks.append(_check("playable_option_coverage", "failed", reason=str(exc)))
            else:
                checks.append(
                    _check(
                        "playable_option_coverage",
                        "passed",
                        reason="实际对白选择完整绑定真实分支",
                        observed=result,
                        paths=["/choices", "/tree/nodes/*/choices"],
                    )
                )
    business_required = full or operation in _QUEST_OPS
    if business_required or business_claims is not None:
        checks.extend(
            _business_checks(
                value,
                business_claims,
                required=business_required,
                actual_outputs={
                    "plan": _plain(plan),
                    "quest": _plain(quest),
                    "logic": _plain(quest),
                    "tree": _plain(tree),
                    "dialogue": _plain(tree),
                    "bound": {"quest": _plain(quest), "tree": _plain(tree)},
                },
            )
        )
    return _report(value, operation, checks, raw)


def check_role_requirements(
    contract: RequirementContract | Mapping[str, Any],
    operation: str,
    outputs: Mapping[str, Any],
    *,
    inputs: Mapping[str, Any] | None = None,
) -> RequirementsReport:
    """Adapt real registered role ports/envelopes; no unspecified role fallback exists."""
    if operation not in _OPERATIONS:
        raise ValueError("未支持的需求检查角色：" + operation)
    incoming = dict(inputs or {})
    contract_input = incoming.get("contract")
    if isinstance(contract_input, Mapping):
        for target, source in (("plan", "plan"), ("quest", "quest"), ("decisions", "mapping")):
            if target not in incoming and source in contract_input:
                incoming[target] = contract_input[source]
    combined = {**incoming, **outputs}
    bound = combined.get("bound")
    if isinstance(bound, Mapping):
        combined = {**combined, **bound}
    actual_bound = outputs.get("bound")
    actual_bound = actual_bound if isinstance(actual_bound, Mapping) else {}
    actual_quest = outputs.get("quest") or outputs.get("logic") or actual_bound.get("quest")
    quest = actual_quest or combined.get("quest") or combined.get("logic")
    tree = (
        outputs.get("tree")
        or outputs.get("dialogue")
        or actual_bound.get("tree")
        or combined.get("tree")
        or combined.get("dialogue")
    )
    actual_claims = outputs.get("business_role_claims") or actual_bound.get("business_role_claims")
    if operation in {"coverage_audit", "validate_decisions"} and actual_claims is None:
        actual_claims = incoming.get("business_role_claims")
    claim_values = [actual_claims] if actual_claims is not None else []
    if isinstance(actual_quest, (Mapping, BaseModel)):
        metadata = _plain(actual_quest).get("metadata", {}).get("requirements_v2", {})
        if isinstance(metadata, Mapping) and metadata.get("business_role_claims") is not None:
            claim_values.append(metadata["business_role_claims"])
    elif operation in {
        "draft_dialogue",
        "dialogue_constrained",
        "coverage_audit",
        "validate_decisions",
    } and isinstance(quest, (Mapping, BaseModel)):
        metadata = _plain(quest).get("metadata", {}).get("requirements_v2", {})
        if isinstance(metadata, Mapping) and metadata.get("business_role_claims") is not None:
            claim_values.append(metadata["business_role_claims"])
    if len({content_hash(_plain(row)) for row in claim_values}) > 1:
        raise ValueError("实际输出信封与Quest metadata中的业务角色声明冲突")
    return check_requirement_outputs(
        contract,
        plan=combined.get("plan"),
        quest=quest,
        tree=tree,
        decisions=combined.get("decisions"),
        choices=combined.get("choices"),
        business_claims=claim_values[0] if claim_values else None,
        operation=operation,
    )
