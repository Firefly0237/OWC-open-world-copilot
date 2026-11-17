"""Check executable workflow prerequisites against actual frozen task resources."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from ..content.hash import content_hash
from ..content.models import ContentBundle, EntityType
from ..inspiration.models import USE_DRAFTING, ReferenceSource
from .models import TaskRecord, WorkflowSpec

_SLOT = re.compile(r"\{\{([a-zA-Z][a-zA-Z0-9_]*)\}\}")
_GUIDANCE_CONSUMERS = {
    "plan_events",
    "draft_quest",
    "draft_dialogue",
    "draft_logic",
    "bind_dialogue",
}


def assess_applicability(
    spec: WorkflowSpec,
    task: TaskRecord,
    *,
    reference_sources: Sequence[ReferenceSource] | None = None,
) -> dict[str, Any]:
    """Assess only real operator contracts; NL applicability text is not a hard policy.

    Reference sources must be a caller-supplied snapshot from the real reference store.
    None means availability is unknown, not an empty or implicitly permitted catalog.
    This function does not run tools, call models, change budgets, or accept a method.
    """
    from .planning import validate_bindings

    reasons: list[str] = []
    checks: list[dict[str, Any]] = []
    scope = {
        "project": task.project,
        "task_id": task.id,
        "revision": task.revision,
        "base_version": task.base_version,
        "base_hash": task.base_hash,
        "assessment": "current_operator_resource_contracts_only",
        "natural_language_applicability": "not_evaluated",
        "reference_snapshot_recovery": "not_established",
    }

    def check(code: str, passed: bool, reason: str = "", **evidence: Any) -> None:
        checks.append({"code": code, "passed": passed, **evidence})
        if not passed:
            reasons.append(reason)

    def result() -> dict[str, Any]:
        return {"compatible": not reasons, "reasons": reasons, "checks": checks, "scope": scope}

    check(
        "supported_schema",
        spec.schema_version == 1,
        f"工作图schema版本尚不支持：{spec.schema_version}",
        schema_version=spec.schema_version,
    )
    try:
        compiled = validate_bindings(spec)
    except ValueError as exc:
        check("operation_bindings", False, f"工作图操作或依赖不兼容：{exc}")
        return result()
    check("operation_bindings", True)
    try:
        bundle = ContentBundle.model_validate(task.base_bundle)
    except ValueError as exc:
        check("frozen_bundle", False, f"冻结项目内容无效：{exc}")
        return result()
    check(
        "frozen_bundle_hash",
        content_hash(bundle) == task.base_hash,
        "冻结项目内容与base_hash不一致，不能判断工作图资源兼容",
    )

    needs_dialogue = any(node.operation == "draft_dialogue" for node in compiled.nodes)
    npc_ids = sorted(
        entity.id for entity in bundle.entities.values() if entity.type is EntityType.NPC
    )
    check(
        "dialogue_participants",
        not needs_dialogue or bool(npc_ids),
        "工作图包含对白起草，但冻结项目没有真实NPC",
        required=needs_dialogue,
        available_npc_count=len(npc_ids),
    )

    reference_slots = {slot.name for slot in compiled.input_slots if slot.source == "reference_ids"}
    consumed_reference_slot = any(
        node.operation in _GUIDANCE_CONSUMERS
        and bool(set(_SLOT.findall(node.guidance)) & reference_slots)
        for node in compiled.nodes
    )
    requested = set(task.request.reference_ids)
    needs_selected_sources = bool(requested) and (
        task.request.use_references or consumed_reference_slot
    )
    if not needs_selected_sources:
        check(
            "selected_references",
            True,
            required=False,
            reference_use_enabled=task.request.use_references,
            reference_slot_consumed=consumed_reference_slot,
        )
        return result()
    if reference_sources is None:
        check(
            "reference_catalog_available",
            False,
            "已明确使用指定参考资料，但未提供真实参考库快照，无法确认资源可用",
            requested_source_ids=sorted(requested),
        )
        return result()

    check("reference_catalog_available", True)
    sources = {source.id: source for source in reference_sources}
    for source_id in sorted(requested):
        source = sources.get(source_id)
        check(
            "selected_reference_exists",
            source is not None,
            f"指定参考资料不存在：{source_id}",
            source_id=source_id,
        )
        if source is None:
            continue
        check(
            "selected_reference_drafting_allowed",
            USE_DRAFTING in source.allowed_uses and source.quarantine_status != "quarantined",
            f"参考资料尚未允许用于草稿：{source_id}",
            source_id=source_id,
            text_hash=source.text_hash,
            allowed_uses=list(source.allowed_uses),
            quarantine_status=source.quarantine_status,
        )
    return result()
