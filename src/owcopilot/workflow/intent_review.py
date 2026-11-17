"""Pure candidate gate: source binding, actual-diff permissions, audit and supported hard rules."""

from __future__ import annotations

import re
from typing import Any

from ..audit.baseline import issue_fingerprint
from ..audit.context import AuditContext
from ..audit.runner import AuditRunner
from ..content.hash import content_hash
from ..content.models import ContentBundle, DialogueTree, Quest
from ..logic.expr import parse_expr, refs_in
from ..logic.semantics import evaluate, type_errors
from ..logic.temporal import SpecStatus, check_temporal_spec
from ..patches.models import PatchCandidate, PatchOp
from ..patches.shadow import apply_patch_shadow
from .intent_models import IntentContract, IntentFormalRule, IntentPatchReview, intent_pointer_parts
from .models import Artifact, TaskRecord

_INDEX = re.compile(r"0|[1-9][0-9]*")


def validate_intent_rule(bundle: ContentBundle, rule: IntentFormalRule) -> None:
    """Only declared local symbols are supported; unknown flags must never imply a proof."""
    quest = bundle.quests.get(rule.quest_id)
    if quest is None or quest.logic is None or not quest.stages:
        raise ValueError("硬规约需要存在且有阶段和logic的任务")
    spec = rule.spec
    if spec.pattern.value == "AG_EF_ENDING":
        if spec.expression.strip() or spec.antecedent.strip() or spec.int_ranges:
            raise ValueError("无软锁模板不消费表达式、触发条件或整数范围，不得静默忽略")
        return
    if spec.pattern.value != "AG" and spec.antecedent.strip():
        raise ValueError("触发条件只用于AG规约的空真检查")
    symbols = {variable.id: variable.type.value for variable in quest.logic.variables}
    defaults = {variable.id: variable.default for variable in quest.logic.variables}
    for text in (spec.expression, *([spec.antecedent] if spec.antecedent.strip() else [])):
        expression = parse_expr(text)
        unknown = refs_in(expression) - set(symbols)
        if unknown:
            raise ValueError("硬规约引用未声明的本任务变量：" + "、".join(sorted(unknown)))
        if type_errors(expression, symbols) or not isinstance(evaluate(expression, defaults), bool):
            raise ValueError("硬规约表达式必须与声明类型一致并返回布尔值")
    for variable, bounds in spec.int_ranges.items():
        if symbols.get(variable) != "int" or bounds[0] > bounds[1]:
            raise ValueError("整数探索范围必须引用本任务int变量且上下界有序")


def _lookup(document: Any, tokens: tuple[str, ...]) -> Any:
    value = document
    for token in tokens:
        if isinstance(value, dict):
            value = value[token]
        elif isinstance(value, list) and _INDEX.fullmatch(token):
            value = value[int(token)]
        else:
            raise ValueError("范围包含不存在的字段或非规范数组索引")
    return value


def resolve_intent_path(bundle: ContentBundle | dict[str, Any], path: str) -> Any:
    """Saving APIs can validate a confirmed range against the exact reconstructed document."""
    document = bundle.model_dump(mode="json") if isinstance(bundle, ContentBundle) else bundle
    return _lookup(document, intent_pointer_parts(path))


def _prefix(parent: tuple[str, ...], child: tuple[str, ...]) -> bool:
    return child[: len(parent)] == parent


def _pointer(tokens: tuple[str, ...]) -> str:
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in tokens)


def _diff(before: Any, after: Any, path: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    if type(before) is not type(after):
        return {path}
    if isinstance(before, dict):
        changed = set()
        for key in before.keys() | after.keys():
            child = (*path, key)
            if key not in before or key not in after:
                changed.add(child)
            else:
                changed.update(_diff(before[key], after[key], child))
        return changed
    if isinstance(before, list):
        # Length changes affect the list's identity/positions, not merely its last leaf.
        changed = {path} if len(before) != len(after) else set()
        for index, (old, new) in enumerate(zip(before, after, strict=False)):
            changed.update(_diff(old, new, (*path, str(index))))
        return changed
    return set() if before == after else {path}


def _validate_operation(document: dict[str, Any], path: str, op: PatchOp) -> None:
    tokens = intent_pointer_parts(path)
    parent = _lookup(document, tokens[:-1])
    key = tokens[-1]
    if isinstance(parent, list):
        if op is PatchOp.ADD:
            # Production shadow uses assignment for numeric ADD; do not claim RFC insertion.
            if key != "-":
                raise ValueError("数组新增只支持明确的末尾append；已有元素请用replace")
        elif not _INDEX.fullmatch(key) or int(key) >= len(parent):
            raise ValueError("数组索引无效、越界或不是规范非负索引")
    elif isinstance(parent, dict):
        if op is not PatchOp.ADD and key not in parent:
            raise ValueError("replace/remove目标字段不存在")
    else:
        raise ValueError("补丁不能写入标量内部")


def intent_baseline_bundle(task: TaskRecord, baseline: Artifact) -> ContentBundle:
    """Reconstruct the candidate over frozen project facts, never a caller-supplied world."""
    if baseline.task_id != task.id or baseline.revision > task.revision:
        raise ValueError("基线产物不属于当前任务或来自未来版本")
    if content_hash(baseline.payload) != baseline.content_hash:
        raise ValueError("基线产物payload与hash不一致")
    bundle = ContentBundle.model_validate(task.base_bundle)
    if content_hash(bundle) != task.base_hash:
        raise ValueError("冻结项目内容与base_hash不一致")
    if baseline.kind not in {"quest", "quest_with_logic", "bound_dialogue"}:
        raise ValueError("意图修复需要实际任务候选产物")
    quest = Quest.model_validate(baseline.payload["quest"])
    bundle.quests[quest.id] = quest
    if "tree" in baseline.payload:
        tree = DialogueTree.model_validate(baseline.payload["tree"])
        bundle.dialogue_trees[tree.id] = tree
    return bundle


def review_intent_patch(
    contract: IntentContract,
    *,
    task: TaskRecord,
    baseline: Artifact,
    candidate: PatchCandidate,
    runner: AuditRunner,
    max_states: int = 2000,
    max_depth: int = 100,
) -> IntentPatchReview:
    """Review once; no model, mutation, author confirmation, revision or canonical write."""
    report = IntentPatchReview(
        contract_id=contract.id,
        contract_version=contract.version,
        baseline_artifact_id=baseline.id,
        modification_goal=contract.modification_goal,
        soft_results=[{"text": text, "status": "unknown"} for text in contract.soft_text],
    )
    if not contract.confirmed:
        report.status = "needs_confirmation"
        report.reasons.append("意图合同尚未由作者确认")
        return report
    if (
        contract.project != task.project
        or contract.task_id != task.id
        or contract.task_revision != task.revision
        or contract.baseline_artifact_id != baseline.id
        or contract.baseline_artifact_hash != baseline.content_hash
    ):
        report.reasons.append("合同与任务版本或基线产物hash绑定不一致")
        return report
    try:
        if max_states < 1 or max_depth < 1:
            raise ValueError("正式检查范围必须为正数")
        before = intent_baseline_bundle(task, baseline)
        old_document = before.model_dump(mode="json")
        allowed = [intent_pointer_parts(path) for path in contract.allowed_paths]
        for path in contract.allowed_paths:
            resolve_intent_path(old_document, path)
        for lock in contract.hard_locks:
            resolve_intent_path(old_document, lock.path)
        working = before
        for op in candidate.ops:
            parts = intent_pointer_parts(op.path)
            if not any(_prefix(scope, parts) or _prefix(parts, scope) for scope in allowed):
                raise ValueError("补丁操作指向未经允许的范围：" + op.path)
            _validate_operation(working.model_dump(mode="json"), op.path, op.op)
            working = apply_patch_shadow(working, [op])
        after = working
        new_document = after.model_dump(mode="json")
        changes = _diff(old_document, new_document)
        report.before_hash = content_hash(before)
        report.after_hash = content_hash(after)
        report.changed_paths = sorted(_pointer(path) for path in changes)
        if not changes:
            report.reasons.append("候选没有实际内容差异")
        outside = [path for path in changes if not any(_prefix(scope, path) for scope in allowed)]
        if outside:
            report.reasons.append(
                "实际差异越过允许范围：" + "、".join(sorted(map(_pointer, outside)))
            )
        for lock in contract.hard_locks:
            try:
                original = resolve_intent_path(old_document, lock.path)
                changed = resolve_intent_path(new_document, lock.path)
                if _diff(original, changed):
                    report.reasons.append("已确认硬字段锁被改变：" + lock.path)
            except (ValueError, KeyError, IndexError):
                report.reasons.append("已确认硬字段锁被删除：" + lock.path)
        if report.reasons:
            return report

        # Never collapse null/empty fingerprints: the production helper hashes rule/target/evidence.
        before_errors = {
            issue_fingerprint(issue)
            for issue in runner.run(AuditContext.from_bundle(before)).open_errors
        }
        after_errors = {
            issue_fingerprint(issue)
            for issue in runner.run(AuditContext.from_bundle(after)).open_errors
        }
        targets = set(contract.target_error_fingerprints)
        missing = targets - before_errors
        report.resolved_errors = sorted(before_errors - after_errors)
        report.remaining_targets = sorted(targets & after_errors)
        report.introduced_errors = sorted(after_errors - before_errors)
        if missing:
            report.reasons.append(
                "目标错误不在基线真实open errors中：" + "、".join(sorted(missing))
            )
        if report.remaining_targets:
            report.reasons.append("目标错误尚未消失")
        if report.introduced_errors:
            report.reasons.append("候选引入了新的错误身份，不能用错误总数抵消")
        for rule in contract.formal_rules:
            old_quest = before.quests.get(rule.quest_id)
            new_quest = after.quests.get(rule.quest_id)
            results: dict[str, Any] = {
                "quest_id": rule.quest_id,
                "spec": rule.spec.model_dump(mode="json"),
            }
            for side, quest in (("before", old_quest), ("after", new_quest)):
                if task.request.comparison and task.request.comparison.initial_state:
                    results[side] = {
                        "status": "withheld",
                        "strong": False,
                        "reason": "现有时序检查器不支持本任务的显式比较初态，未用默认初态证明替代",
                    }
                elif quest is None or quest.logic is None or not quest.stages:
                    results[side] = {"status": "unsupported", "strong": False}
                else:
                    try:
                        validate_intent_rule(before if side == "before" else after, rule)
                    except ValueError as exc:
                        results[side] = {
                            "status": "unsupported",
                            "strong": False,
                            "reason": str(exc),
                        }
                    else:
                        results[side] = check_temporal_spec(
                            quest, rule.spec, max_states=max_states, max_depth=max_depth
                        ).model_dump(mode="json")
            report.formal_results.append(results)
            if results["after"]["status"] != SpecStatus.PROVEN.value or not results["after"].get(
                "strong"
            ):
                report.reasons.append("硬正式规约未获proven：" + (rule.spec.id or rule.quest_id))
        if not report.reasons:
            report.status = "ready_for_author"
            # Match the canonical ContentBundle hash, including omission of null model fields.
            report.candidate_bundle = after.model_dump(mode="json", exclude_none=True)
        return report
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        report.reasons.append(f"候选或合同无法检查：{type(exc).__name__}: {exc}")
        return report
