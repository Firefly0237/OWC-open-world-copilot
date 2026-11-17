"""Typed planning, grounded reflection and parameterized method maintenance.

This is a project adaptation: distinct Reflector and Curator calls, explicit
typed graph edits and source-bound entry deltas.
Semantic similarity suggests curation candidates, never establishes truth.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..content.hash import content_hash
from .evolution import GuidanceEntry, SkillConditions, SourceRef
from .graph_delta import GraphDelta, GraphEditIR, graph_edit_schema, validate_narrative_obligations
from .graph_v2 import (
    ControlEdge,
    DataEdge,
    GraphSpecV2,
    NodeSpecV2,
    OutputRef,
    PortSpec,
    RoleContract,
    compile_graph,
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlannedNode(_Model):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    operation: str = Field(min_length=1)
    guidance: str = Field(default="", max_length=8000)


class PlanIR(_Model):
    rationale: str = Field(min_length=1, max_length=4000)
    nodes: list[PlannedNode] = Field(min_length=1, max_length=32)
    data_edges: list[DataEdge] = Field(default_factory=list, max_length=128)
    control_edges: list[ControlEdge] = Field(default_factory=list, max_length=64)
    outputs: dict[str, OutputRef]


PRODUCTS = {
    "plan_events": "plan",
    "draft_quest": "quest",
    "draft_logic": "logic",
    "logic_contract": "logic",
    "draft_dialogue": "dialogue",
    "dialogue_constrained": "dialogue",
    "validate_decisions": "logic",
    "bind_dialogue": "binding",
    "coverage_audit": "audit",
}


def compile_plan_ir(plan: PlanIR, registry: Mapping[str, RoleContract]) -> GraphSpecV2:
    """Assign budgets in the host, never from planner-produced instance names."""
    nodes = []
    for proposed in plan.nodes:
        role = registry.get(proposed.operation)
        product = PRODUCTS.get(proposed.operation)
        if role is None or product is None:
            raise ValueError("规划器选择了未注册生产能力")
        nodes.append(
            NodeSpecV2(
                id=proposed.id,
                operation=proposed.operation,
                role_version=role.version,
                product_key=f"slice/{product}",
                problem_key=f"slice/{product}/produce",
                context_bindings={"task": "task"} if "task" in role.inputs else {},
                guidance=proposed.guidance,
            )
        )
    operations = {node.operation for node in nodes}
    if not {"plan_events", "draft_quest", "bind_dialogue", "coverage_audit"} <= operations:
        raise ValueError("初始图缺少必需规划、草稿、绑定或审计角色")
    if not operations.intersection(
        {"draft_logic", "logic_contract"}
    ) or not operations.intersection({"draft_dialogue", "dialogue_constrained"}):
        raise ValueError("初始图缺少实际机制或对白生产角色")
    graph = GraphSpecV2(
        name="模型规划的制作图",
        nodes=nodes,
        data_edges=plan.data_edges,
        control_edges=plan.control_edges,
        outputs=plan.outputs,
    )
    compiled = compile_graph(graph, registry, {"task": PortSpec(type_name="task_context")})
    validate_narrative_obligations(compiled.spec)
    by_id = {node.id: node for node in nodes}
    if set(graph.outputs) != {"bound", "report"} or (
        by_id[graph.outputs["bound"].node_id].operation != "bind_dialogue"
        or graph.outputs["bound"].port != "bound"
        or by_id[graph.outputs["report"].node_id].operation != "coverage_audit"
        or graph.outputs["report"].port != "report"
    ):
        raise ValueError("初始图必须发布真实绑定产物及独立审计")
    report_id = graph.outputs["report"].node_id
    bound_id = graph.outputs["bound"].node_id
    if not any(
        edge.source_node == bound_id
        and edge.source_port == "bound"
        and edge.target_node == report_id
        and edge.target_port == "bound"
        for edge in graph.data_edges
    ):
        raise ValueError("审计必须实际消费最终绑定产物")
    reachable = {ref.node_id for ref in graph.outputs.values()}
    for _ in nodes:
        reachable.update(
            edge.source_node for edge in graph.data_edges if edge.target_node in reachable
        )
        reachable.update(
            edge.source_node for edge in graph.control_edges if edge.target_node in reachable
        )
    if any(node.id not in reachable for node in nodes):
        raise ValueError("规划图不能包含脱离产物路径的假节点")
    for node in nodes:
        if (
            node.operation not in {"validate_decisions", "coverage_audit"}
            and not any(edge.source_node == node.id for edge in graph.data_edges)
            and node.id not in {ref.node_id for ref in graph.outputs.values()}
        ):
            raise ValueError("规划图的生产节点输出必须被真实消费")
    return compiled.spec


class ParameterSlot(_Model):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    kind: Literal["entity", "requirement"]
    binding_key: str = Field(min_length=1, max_length=120)
    source_value: str = Field(min_length=1, max_length=400)


class ReusableEntry(_Model):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    text: str = Field(min_length=1, max_length=8000)
    operation: str = ""
    slots: list[ParameterSlot] = Field(default_factory=list, max_length=20)
    evidence_kind: Literal["observation", "author_preference", "hypothesis"] = "hypothesis"
    support_refs: list[SourceRef] = Field(default_factory=list, max_length=30)
    counterexample_refs: list[SourceRef] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def placeholders(self) -> Self:
        declared = [slot.name for slot in self.slots]
        actual = set(re.findall(r"\{\{([A-Za-z][A-Za-z0-9_]*)\}\}", self.text))
        if len(declared) != len(set(declared)) or actual != set(declared):
            raise ValueError("经验参数与模板占位符必须完整一一对应")
        if not self.support_refs and not self.counterexample_refs:
            raise ValueError("经验条目必须保留支持或反例来源")
        return self

    def guidance(self) -> GuidanceEntry:
        refs = {content_hash(ref): ref for ref in [*self.support_refs, *self.counterexample_refs]}
        return GuidanceEntry(
            id=self.id,
            text=self.text,
            operation=self.operation,
            evidence_kind=self.evidence_kind,
            source_refs=list(refs.values()),
        )


class ReflectionReport(_Model):
    diagnosis: str = Field(min_length=1, max_length=8000)
    target_problem: str = Field(min_length=1, max_length=4000)
    evidence_refs: list[SourceRef] = Field(min_length=1, max_length=30)
    reusable_entries: list[ReusableEntry] = Field(default_factory=list, max_length=30)
    proposed_change: Literal["structure", "guidance", "mixed", "none"]
    risks: list[str] = Field(default_factory=list, max_length=20)


class EntryMerge(_Model):
    keep_id: str
    remove_id: str
    reason: str = Field(min_length=1, max_length=2000)


class TypedEvolutionProposal(_Model):
    rationale: str = Field(min_length=1, max_length=4000)
    target_problem: str = Field(min_length=1, max_length=4000)
    change_kind: Literal["structure", "guidance", "mixed"]
    graph_delta: GraphDelta | None = None
    upsert_entries: list[ReusableEntry] = Field(default_factory=list, max_length=30)
    remove_entry_ids: list[str] = Field(default_factory=list, max_length=30)
    merges: list[EntryMerge] = Field(default_factory=list, max_length=20)
    conditions: SkillConditions | None = None
    description: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def actual_kind(self) -> Self:
        guidance = bool(self.upsert_entries or self.remove_entry_ids or self.merges)
        structure = self.graph_delta is not None
        expected = "mixed" if structure and guidance else "structure" if structure else "guidance"
        if self.change_kind != expected or not (structure or guidance):
            raise ValueError("必须按真实变化区分结构演化和指导维护")
        ids = [entry.id for entry in self.upsert_entries]
        if len(ids) != len(set(ids)) or len(self.remove_entry_ids) != len(
            set(self.remove_entry_ids)
        ):
            raise ValueError("同一条目不能重复维护")
        return self


class ReflectionReportV2(_Model):
    """Host identity and model diagnosis are intentionally different fields."""

    protocol: Literal["method-reflection-v2"] = "method-reflection-v2"
    problem_id: str = Field(min_length=1, max_length=160)
    target_description: str = Field(min_length=1, max_length=4000)
    diagnosis: str = Field(min_length=1, max_length=8000)
    evidence_refs: list[SourceRef] = Field(min_length=1, max_length=30)
    reusable_entries: list[ReusableEntry] = Field(default_factory=list, max_length=30)
    proposed_change: Literal["structure", "guidance", "mixed", "none"]
    risks: list[str] = Field(default_factory=list, max_length=20)
    evidence_status: Literal["sufficient", "insufficient"] = "sufficient"

    @model_validator(mode="after")
    def unchanged_is_empty(self) -> Self:
        if self.proposed_change == "none" and self.reusable_entries:
            raise ValueError("无需变更的报告不能同时提出经验更新")
        if self.evidence_status == "insufficient" and self.proposed_change != "none":
            raise ValueError("证据不足不能提出方法修改")
        return self


class TypedEvolutionProposalV2(_Model):
    protocol: Literal["method-proposal-v2"] = "method-proposal-v2"
    problem_id: str = Field(min_length=1, max_length=160)
    target_description: str = Field(min_length=1, max_length=4000)
    rationale: str = Field(min_length=1, max_length=4000)
    change_kind: Literal["structure", "guidance", "mixed"]
    graph_edit: GraphEditIR | None = None
    upsert_entries: list[ReusableEntry] = Field(default_factory=list, max_length=30)
    remove_entry_ids: list[str] = Field(default_factory=list, max_length=30)
    merges: list[EntryMerge] = Field(default_factory=list, max_length=20)
    conditions: SkillConditions | None = None
    description: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def actual_kind(self) -> Self:
        guidance = bool(self.upsert_entries or self.remove_entry_ids or self.merges)
        structure = self.graph_edit is not None
        expected = "mixed" if structure and guidance else "structure" if structure else "guidance"
        if self.change_kind != expected or not (structure or guidance):
            raise ValueError("必须按真实变化区分结构演化和指导维护")
        ids = [entry.id for entry in self.upsert_entries]
        if len(ids) != len(set(ids)) or len(self.remove_entry_ids) != len(
            set(self.remove_entry_ids)
        ):
            raise ValueError("同一条目不能重复维护")
        return self


class TemplateText(_Model):
    kind: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=8000)

    @model_validator(mode="after")
    def host_owned_syntax(self) -> Self:
        if "{{" in self.text or "}}" in self.text:
            raise ValueError("文字片段不能手写占位符；参数必须使用binding片段")
        return self


class TemplateBinding(_Model):
    kind: Literal["binding"] = "binding"
    binding_kind: Literal["entity", "requirement"]
    binding_key: str = Field(min_length=1, max_length=120)


TemplatePart = Annotated[TemplateText | TemplateBinding, Field(discriminator="kind")]


class EntryTemplateIR(_Model):
    """Constructive template syntax; slot names/source values are never model fields."""

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    parts: list[TemplatePart] = Field(min_length=1, max_length=100)
    operation: str = ""
    evidence_kind: Literal["observation", "author_preference", "hypothesis"] = "hypothesis"
    support_refs: list[SourceRef] = Field(default_factory=list, max_length=30)
    counterexample_refs: list[SourceRef] = Field(default_factory=list, max_length=30)


class ReflectionDraftV3(_Model):
    protocol: Literal["method-reflection-v3"] = "method-reflection-v3"
    problem_id: str = Field(min_length=1, max_length=160)
    target_description: str = Field(min_length=1, max_length=4000)
    diagnosis: str = Field(min_length=1, max_length=8000)
    evidence_refs: list[SourceRef] = Field(min_length=1, max_length=30)
    reusable_entries: list[EntryTemplateIR] = Field(default_factory=list, max_length=30)
    proposed_change: Literal["structure", "guidance", "mixed", "none"]
    risks: list[str] = Field(default_factory=list, max_length=20)
    evidence_status: Literal["sufficient", "insufficient"] = "sufficient"

    @model_validator(mode="after")
    def unchanged_is_empty(self) -> Self:
        if self.proposed_change == "none" and self.reusable_entries:
            raise ValueError("无需变更的报告不能同时提出经验更新")
        if self.evidence_status == "insufficient" and self.proposed_change != "none":
            raise ValueError("证据不足不能提出方法修改")
        return self


class TypedEvolutionDraftV3(_Model):
    protocol: Literal["method-proposal-v3"] = "method-proposal-v3"
    problem_id: str = Field(min_length=1, max_length=160)
    target_description: str = Field(min_length=1, max_length=4000)
    rationale: str = Field(min_length=1, max_length=4000)
    change_kind: Literal["structure", "guidance", "mixed"]
    graph_edit: GraphEditIR | None = None
    upsert_entries: list[EntryTemplateIR] = Field(default_factory=list, max_length=30)
    remove_entry_ids: list[str] = Field(default_factory=list, max_length=30)
    merges: list[EntryMerge] = Field(default_factory=list, max_length=20)
    conditions: SkillConditions | None = None
    description: str | None = Field(default=None, max_length=4000)


def compile_entry_templates(
    entries: list[EntryTemplateIR], snapshot: dict[str, Any], registry: Mapping[str, RoleContract]
) -> tuple[list[ReusableEntry], dict[str, Any]]:
    """Parameter abstraction with deterministic syntax, not semantic repair."""
    from .reflection_continuation_v2 import available_parameter_bindings

    available = available_parameter_bindings(snapshot)
    bindings = {(row["kind"], row["binding_key"]): row for row in available}
    compiled = []
    derivations = []
    for entry in entries:
        if entry.operation and entry.operation not in registry:
            raise ValueError("经验operation必须是单个已注册角色，或空字符串表示通用")
        slots: dict[str, ParameterSlot] = {}
        text = []
        for index, part in enumerate(entry.parts):
            if isinstance(part, TemplateText):
                text.append(part.text)
                continue
            binding = bindings.get((part.binding_kind, part.binding_key))
            if binding is None:
                raise ValueError("经验参数引用了冻结表之外的绑定")
            candidate = f"{part.binding_kind}_{part.binding_key}".replace(":", "_")
            name = (
                candidate
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", candidate)
                else ("binding_" + content_hash([part.binding_kind, part.binding_key])[:20])
            )
            slots[name] = ParameterSlot(
                name=name,
                kind=binding["kind"],
                binding_key=binding["binding_key"],
                source_value=binding["source_value"],
            )
            text.append("{{" + name + "}}")
            derivations.append(
                {
                    "entry_id": entry.id,
                    "part_index": index,
                    "slot_name": name,
                    "binding_kind": part.binding_kind,
                    "binding_key": part.binding_key,
                    "frozen_source_value": binding["source_value"],
                }
            )
        compiled.append(
            ReusableEntry(
                id=entry.id,
                text="".join(text),
                operation=entry.operation,
                slots=list(slots.values()),
                evidence_kind=entry.evidence_kind,
                support_refs=entry.support_refs,
                counterexample_refs=entry.counterexample_refs,
            )
        )
    receipt = {
        "protocol": "entry-template-compilation-v1",
        "draft_entries": [entry.model_dump(mode="json") for entry in entries],
        "available_bindings_hash": content_hash(available),
        "compiled_entries": [entry.model_dump(mode="json") for entry in compiled],
        "derivations": derivations,
        "semantic_repair": "none",
    }
    return compiled, {**receipt, "receipt_hash": content_hash(receipt)}


def compile_reflection_draft(
    draft: ReflectionDraftV3, snapshot: dict[str, Any], registry: Mapping[str, RoleContract]
) -> tuple[ReflectionReportV2, dict[str, Any]]:
    entries, receipt = compile_entry_templates(draft.reusable_entries, snapshot, registry)
    material = draft.model_dump(mode="json", exclude={"protocol", "reusable_entries"})
    material["reusable_entries"] = [entry.model_dump(mode="json") for entry in entries]
    return ReflectionReportV2.model_validate(material), receipt


def compile_evolution_draft(
    draft: TypedEvolutionDraftV3, snapshot: dict[str, Any], registry: Mapping[str, RoleContract]
) -> tuple[TypedEvolutionProposalV2, dict[str, Any]]:
    entries, receipt = compile_entry_templates(draft.upsert_entries, snapshot, registry)
    material = draft.model_dump(mode="json", exclude={"protocol", "upsert_entries"})
    material["upsert_entries"] = [entry.model_dump(mode="json") for entry in entries]
    return TypedEvolutionProposalV2.model_validate(material), receipt


def bind_method_schema(
    schema: dict[str, Any],
    *,
    problem_id: str,
    graph: GraphSpecV2 | None = None,
    registry: Mapping[str, RoleContract] | None = None,
    graph_hash: str | None = None,
) -> dict[str, Any]:
    """The wire contract constrains identity; runtime must still verify the same value."""
    bound = json.loads(json.dumps(schema))
    bound["properties"]["problem_id"]["const"] = problem_id
    definitions = bound.setdefault("$defs", {})
    if registry is not None and "EntryTemplateIR" in definitions:
        definitions["EntryTemplateIR"]["properties"]["operation"]["enum"] = ["", *sorted(registry)]
    if graph is not None and registry is not None and graph_hash is not None:
        edits = graph_edit_schema(graph, registry, graph_hash)
        definitions = bound.setdefault("$defs", {})
        definitions.update(edits.pop("$defs"))
        definitions["GraphEditIR"] = edits
    return bound


def verify_problem_identity(
    problem_id: str,
    report: ReflectionReportV2,
    proposal: TypedEvolutionProposalV2 | None = None,
) -> None:
    if report.problem_id != problem_id or (
        proposal is not None and proposal.problem_id != problem_id
    ):
        raise ValueError("反思／改图不能改变宿主固定的问题身份")


def verify_grounding(
    report: ReflectionReport | ReflectionReportV2,
    proposal: TypedEvolutionProposal | TypedEvolutionProposalV2 | None,
    allowed_refs: list[SourceRef],
    source_entities: set[str],
    source_requirements: dict[str, Any],
    *,
    author_feedback: bool,
) -> None:
    allowed = {content_hash(ref) for ref in allowed_refs}
    entries = report.reusable_entries + (proposal.upsert_entries if proposal else [])
    refs = list(report.evidence_refs)
    for entry in entries:
        if entry.evidence_kind == "author_preference" and not author_feedback:
            raise ValueError("模型或开发示例不能升级为作者偏好")
        refs.extend([*entry.support_refs, *entry.counterexample_refs])
        for slot in entry.slots:
            if slot.kind == "entity" and slot.source_value not in source_entities:
                raise ValueError("参数化经验引用未知源实体")
            if slot.kind == "requirement" and str(source_requirements.get(slot.binding_key)) != (
                slot.source_value
            ):
                raise ValueError("参数化经验的源机器要求没有依据")
            if len(slot.source_value) > 1 and slot.source_value in entry.text:
                raise ValueError("源实例值必须替换成参数，不能混入通用指导")
    if any(content_hash(ref) not in allowed for ref in refs):
        raise ValueError("反思或整理引用了未授权／变更的证据")
    if proposal is not None and proposal.conditions is not None:
        for requirement in proposal.conditions.requirements:
            if requirement.confirmed and (
                requirement.key not in source_requirements
                or type(requirement.value) is not type(source_requirements[requirement.key])
                or requirement.value != source_requirements[requirement.key]
                or requirement.source_ref is None
                or content_hash(requirement.source_ref) not in allowed
            ):
                raise ValueError("Curator不能把模型推断升级为已确认机器条件")


def install_entry_templates(
    graph: dict[str, Any], entries: list[ReusableEntry], remove_ids: list[str]
) -> dict[str, Any]:
    spec = GraphSpecV2.model_validate(graph)
    nodes = []
    for node in spec.nodes:
        parameters = dict(node.parameters)
        templates = {row["id"]: row for row in parameters.get("skill_entry_templates", [])}
        for entry_id in remove_ids:
            templates.pop(entry_id, None)
        for entry in entries:
            if entry.operation in {"", node.operation}:
                templates[entry.id] = entry.model_dump(mode="json")
            else:
                templates.pop(entry.id, None)
        if templates or "skill_entry_templates" in parameters:
            parameters["skill_entry_templates"] = list(templates.values())
        nodes.append(node.model_copy(update={"parameters": parameters}))
    return spec.model_copy(update={"nodes": nodes}).model_dump(mode="json")


def render_parameterized_guidance(node: NodeSpecV2, task_context: dict[str, Any]) -> str:
    """Bind current author-specified participants/requirements, never old story values."""
    result = node.guidance
    requirements = task_context.get("machine_requirements") or {}
    explicit = task_context.get("slot_bindings") or {}
    entity_ids = {row["id"] for row in task_context.get("entities", [])}
    for raw in node.parameters.get("skill_entry_templates", []):
        entry = ReusableEntry.model_validate(raw)
        for slot in entry.slots:
            value = explicit.get(slot.name)
            if (
                value is None
                and slot.kind == "entity"
                and slot.binding_key.startswith("participant:")
            ):
                index = int(slot.binding_key.split(":", 1)[1])
                participants = requirements.get("participant_ids", [])
                value = participants[index] if 0 <= index < len(participants) else None
            elif value is None and slot.kind == "requirement":
                value = requirements.get(slot.binding_key)
            if value is None or (slot.kind == "entity" and value not in entity_ids):
                raise ValueError(f"经验槽{slot.name}无法按当前明确要求重新绑定")
            result = result.replace("{{" + slot.name + "}}", str(value))
    if re.search(r"\{\{[A-Za-z][A-Za-z0-9_]*\}\}", result):
        raise ValueError("经验指导仍含未绑定参数")
    return result


class SemanticMaintenance:
    """Persist real embeddings; expose similarity for explicit, evidence-preserving curation."""

    def __init__(self, workflow, embedder=None):
        self.workflow = workflow
        self.embedder = embedder
        with workflow.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS evolution_entry_vectors("
                "project TEXT,family_id TEXT,entry_id TEXT,text_hash TEXT,model_id TEXT,"
                "payload TEXT,PRIMARY KEY(project,family_id,entry_id,text_hash,model_id))"
            )

    def candidates(
        self, project: str, family_id: str, entries: list[GuidanceEntry]
    ) -> dict[str, Any]:
        if not entries:
            return {"status": "not_needed", "pairs": [], "model_id": None}
        if self.embedder is None:
            from ..retrieval.embedding import resolve_embedder

            self.embedder = resolve_embedder()
        model_id = str(self.embedder.model_id)
        if not model_id.startswith("st:"):
            raise ValueError("经验语义维护要求真实embedding，拒绝hashing替代")
        vectors = {}
        with self.workflow.connection() as db:
            for entry in entries:
                text_hash = content_hash(entry.text)
                row = db.execute(
                    "SELECT payload FROM evolution_entry_vectors WHERE project=? AND "
                    "family_id=? AND entry_id=? AND text_hash=? AND model_id=?",
                    (project, family_id, entry.id, text_hash, model_id),
                ).fetchone()
                if row:
                    vector = json.loads(row[0])
                else:
                    vector = self.embedder.embed(entry.text)
                    if str(self.embedder.model_id) != model_id or getattr(
                        self.embedder, "degraded", False
                    ):
                        raise ValueError("真实embedding加载或编码失败；不接受hashing回退")
                    if not vector or not all(math.isfinite(value) for value in vector):
                        raise ValueError("embedding返回无效向量")
                    db.execute(
                        "INSERT INTO evolution_entry_vectors VALUES(?,?,?,?,?,?)",
                        (project, family_id, entry.id, text_hash, model_id, json.dumps(vector)),
                    )
                vectors[entry.id] = vector
        pairs = []
        for i, left in enumerate(entries):
            for right in entries[i + 1 :]:
                if left.operation != right.operation:
                    continue
                a, b = vectors[left.id], vectors[right.id]
                if len(a) != len(b):
                    raise ValueError("同embedding fingerprint下向量维度不一致")
                denominator = math.sqrt(sum(v * v for v in a) * sum(v * v for v in b))
                similarity = (
                    sum(x * y for x, y in zip(a, b, strict=True)) / denominator
                    if denominator
                    else 0
                )
                if similarity >= 0.9:
                    pairs.append(
                        {"left_id": left.id, "right_id": right.id, "similarity": similarity}
                    )
        return {
            "status": "encoded",
            "model_id": model_id,
            "pairs": pairs,
            "scope": "similarity_candidate_not_equivalence_or_truth",
        }


def compare_production_runs(
    parent: dict[str, Any],
    child: dict[str, Any],
    parent_requests: list[dict[str, Any]],
    child_requests: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare observed technical outcomes only under the same frozen business inputs."""

    def identity(run, requests):
        contexts = [row.get("context_manifest", {}).get("task") for row in requests]
        contexts = [value for value in contexts if value is not None]
        if not contexts or any(value != contexts[0] for value in contexts):
            return None
        configurations = {}
        for row in requests:
            request = row.get("request", {})
            role = request.get("role_id")
            config = request.get("config")
            if not role or not isinstance(config, dict) or not config:
                return None
            configurations.setdefault(role, {})[content_hash(config)] = config
        providers = {
            role: [configs[key] for key in sorted(configs)]
            for role, configs in sorted(configurations.items())
        }
        return content_hash(
            {
                "task": contexts[0],
                "references": run["references"],
                "requirements": (run.get("requirements") or {}).get("machine"),
                "contracts": run.get("host_contract_identity"),
                "provider": providers,
            }
        )

    left, right = identity(parent, parent_requests), identity(child, child_requests)

    def observation(run, requests):
        result = run.get("result") or {}
        report = result.get("outputs", {}).get("report") or {}
        return {
            "status": result.get("status", "missing"),
            "failed_nodes": [
                key
                for key, outcome in result.get("outcomes", {}).items()
                if outcome["status"] not in {"succeeded", "reused"}
            ],
            "hard_issues": [
                issue for issue in report.get("issues", []) if issue.get("severity") == "error"
            ],
            "report_present": bool(report),
            "dispatches": len(requests),
        }

    comparable = left is not None and left == right
    return {
        "status": "comparable" if comparable else "withheld",
        "same_frozen_input": comparable,
        "parent_run_id": parent["run_id"],
        "child_run_id": child["run_id"],
        "parent": observation(parent, parent_requests),
        "child": observation(child, child_requests),
        "scope": "technical_observation_not_causal_quality_or_generalization",
        "goal_status": "withheld",
        "author_acceptance": "unknown",
    }
