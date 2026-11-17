"""Bounded typed graph edits, compiled before execution under existing budgets.

One proposal uses registered capabilities and preserves validation obligations.
The compiled per-node StateGraph and SqliteSaver driver executes the graph;
this module neither runs handlers nor launches tools.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal
from uuid import uuid4

import networkx as nx
from pydantic import BaseModel, ConfigDict, Field

from ..content.hash import content_hash
from .graph_v2 import (
    ControlEdge,
    DataEdge,
    GraphSpecV2,
    NodeSpecV2,
    PortSpec,
    RoleContract,
    TypeValidator,
    compile_graph,
    narrative_role_registry,
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class InsertNode(_Model):
    op: Literal["insert_node"] = "insert_node"
    node: NodeSpecV2
    budget_source_node: str = Field(min_length=1)


class RemoveNode(_Model):
    op: Literal["remove_node"] = "remove_node"
    node_id: str = Field(min_length=1)


class ReplaceRole(_Model):
    op: Literal["replace_role"] = "replace_role"
    node_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    role_version: str = "1"


class UpdateGuidance(_Model):
    """A task-local instruction update cannot change ownership or capability fields."""

    op: Literal["update_guidance"] = "update_guidance"
    node_id: str = Field(min_length=1)
    guidance: str = Field(min_length=1, max_length=8000)


class ConnectData(_Model):
    op: Literal["connect_data"] = "connect_data"
    edge: DataEdge


class DisconnectData(_Model):
    op: Literal["disconnect_data"] = "disconnect_data"
    edge: DataEdge


class ConnectControl(_Model):
    op: Literal["connect_control"] = "connect_control"
    edge: ControlEdge


class DisconnectControl(_Model):
    op: Literal["disconnect_control"] = "disconnect_control"
    edge: ControlEdge


GraphOperation = Annotated[
    InsertNode
    | RemoveNode
    | ReplaceRole
    | UpdateGuidance
    | ConnectData
    | DisconnectData
    | ConnectControl
    | DisconnectControl,
    Field(discriminator="op"),
]


class GraphDelta(_Model):
    expected_graph_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    rationale: str = Field(min_length=1, max_length=4000)
    operations: list[GraphOperation] = Field(min_length=1, max_length=64)


class AppliedGraphDelta(_Model):
    graph: GraphSpecV2
    parent_graph_hash: str
    graph_hash: str
    delta_hash: str
    changes: list[dict[str, Any]]
    affected_nodes: list[str]


class InsertRegisteredRole(_Model):
    """Business attachment only; execution identity belongs to the host."""

    op: Literal["insert_registered_role"] = "insert_registered_role"
    node_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    operation: str = Field(min_length=1)
    source_node: str = Field(min_length=1)
    guidance: str = Field(default="", max_length=8000)


class ReplaceRegisteredRole(_Model):
    op: Literal["replace_registered_role"] = "replace_registered_role"
    node_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)


GraphEditOperation = Annotated[
    InsertRegisteredRole
    | ReplaceRegisteredRole
    | UpdateGuidance
    | RemoveNode
    | ConnectData
    | DisconnectData
    | ConnectControl
    | DisconnectControl,
    Field(discriminator="op"),
]


class GraphEditIR(_Model):
    """The model-facing protocol; never accepts quota, permissions or context bindings."""

    schema_version: Literal[1] = 1
    expected_graph_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    rationale: str = Field(min_length=1, max_length=4000)
    operations: list[GraphEditOperation] = Field(min_length=1, max_length=64)


class CompiledGraphEdit(_Model):
    protocol: Literal["host-compiled-graph-edit-v1"] = "host-compiled-graph-edit-v1"
    edit: GraphEditIR
    edit_hash: str
    host_delta: GraphDelta
    host_delta_hash: str
    derived_fields: list[dict[str, Any]]
    applied: AppliedGraphDelta


# This policy is host configuration, not a field accepted from a model. New
# domain roles require an explicit source policy instead of guessing ownership.
_SOURCE_ROLES: dict[str, frozenset[str]] = {
    "plan_events": frozenset({"plan_events"}),
    "draft_quest": frozenset({"draft_quest"}),
    "draft_logic": frozenset({"draft_logic", "logic_contract"}),
    "logic_contract": frozenset({"draft_logic", "logic_contract"}),
    "validate_decisions": frozenset({"draft_logic", "logic_contract"}),
    "draft_dialogue": frozenset({"draft_dialogue", "dialogue_constrained"}),
    "dialogue_constrained": frozenset({"draft_dialogue", "dialogue_constrained"}),
    "bind_dialogue": frozenset({"bind_dialogue"}),
    "coverage_audit": frozenset({"coverage_audit"}),
}


def graph_edit_source_policy(
    spec: GraphSpecV2, registry: Mapping[str, RoleContract]
) -> dict[str, list[str]]:
    return {
        operation: [node.id for node in spec.nodes if node.operation in sources]
        for operation, sources in _SOURCE_ROLES.items()
        if operation in registry
    }


def graph_edit_context(spec: GraphSpecV2, registry: Mapping[str, RoleContract]) -> dict[str, Any]:
    """Explicit model-facing allowlist, excluding host quota and execution fields."""
    return {
        "nodes": [
            {"id": node.id, "operation": node.operation, "guidance": node.guidance}
            for node in spec.nodes
        ],
        "data_edges": [edge.model_dump(mode="json") for edge in spec.data_edges],
        "control_edges": [edge.model_dump(mode="json") for edge in spec.control_edges],
        "outputs": {key: ref.model_dump(mode="json") for key, ref in spec.outputs.items()},
        "insert_role_sources": graph_edit_source_policy(spec, registry),
        "mandatory_node_ids": [node.id for node in spec.nodes if node.required],
        "protected_node_ids": [
            node.id
            for node in spec.nodes
            if _protected(node, registry, DEFAULT_PROTECTED_OPERATIONS)
        ],
    }


def compile_graph_edit(
    spec: GraphSpecV2 | dict[str, Any],
    edit: GraphEditIR | dict[str, Any],
    registry: Mapping[str, RoleContract],
    context_ports: Mapping[str, PortSpec],
    *,
    source_policy: Mapping[str, list[str]] | None = None,
) -> CompiledGraphEdit:
    """Lower real model edits to the existing strict kernel, with a derivation receipt."""
    parent = compile_graph(spec, registry, context_ports)
    request = GraphEditIR.model_validate(
        edit.model_dump(mode="json") if isinstance(edit, GraphEditIR) else edit
    )
    if request.expected_graph_hash != parent.graph_hash:
        raise ValueError("工作图CAS失败：源图或角色合同已改变")
    sources = (
        graph_edit_source_policy(parent.spec, registry) if source_policy is None else source_policy
    )
    original = {node.id: node for node in parent.spec.nodes}
    operations: list[GraphOperation] = []
    derived: list[dict[str, Any]] = []
    for index, step in enumerate(request.operations):
        if isinstance(step, InsertRegisteredRole):
            role = registry.get(step.operation)
            source = original.get(step.source_node)
            if role is None or source is None or source.id not in sources.get(step.operation, []):
                raise ValueError("新增角色或业务源不在宿主注册的继承合同内")
            # Only same-name, same-type host context ports are authorized here.
            # Other values must travel along explicit, typed data edges.
            bindings = {
                name: name
                for name, port in role.inputs.items()
                if name in context_ports and _signature(port) == _signature(context_ports[name])
            }
            node = NodeSpecV2(
                id=step.node_id,
                operation=step.operation,
                role_version=role.version,
                product_key=source.product_key,
                problem_key=source.problem_key,
                context_bindings=bindings,
                guidance=step.guidance,
            )
            operations.append(InsertNode(node=node, budget_source_node=source.id))
            derived.append(
                {
                    "operation_index": index,
                    "node_id": node.id,
                    "source_node": source.id,
                    "role_version": role.version,
                    "product_key": source.product_key,
                    "problem_key": source.problem_key,
                    "context_bindings": bindings,
                    "required": node.required,
                    "parameters": {},
                }
            )
        elif isinstance(step, ReplaceRegisteredRole):
            role = registry.get(step.operation)
            if role is None:
                raise ValueError("替换引用未注册角色")
            operations.append(
                ReplaceRole(
                    node_id=step.node_id, operation=step.operation, role_version=role.version
                )
            )
            derived.append(
                {
                    "operation_index": index,
                    "node_id": step.node_id,
                    "role_version": role.version,
                    "ownership": "preserve_existing",
                }
            )
        else:
            operations.append(step)
    delta = GraphDelta(
        expected_graph_hash=parent.graph_hash,
        rationale=request.rationale,
        operations=operations,
    )
    applied = apply_graph_delta(parent.spec, delta, registry, context_ports)
    return CompiledGraphEdit(
        edit=request,
        edit_hash=content_hash(request),
        host_delta=delta,
        host_delta_hash=content_hash(delta),
        derived_fields=derived,
        applied=applied,
    )


def graph_edit_schema(
    spec: GraphSpecV2, registry: Mapping[str, RoleContract], expected_graph_hash: str
) -> dict[str, Any]:
    """Bind fixed parent and role/source pairs in the wire schema as well as Python."""
    schema = GraphEditIR.model_json_schema()
    schema["properties"]["expected_graph_hash"]["const"] = expected_graph_hash
    definitions = schema["$defs"]
    insert = definitions["InsertRegisteredRole"]
    pairs = [
        {"properties": {"operation": {"const": role}, "source_node": {"const": source}}}
        for role, sources in graph_edit_source_policy(spec, registry).items()
        for source in sources
    ]
    admission: dict[str, Any] = {"oneOf": pairs} if pairs else {"not": {}}
    insert["allOf"] = [admission]
    definitions["ReplaceRegisteredRole"]["properties"]["operation"]["enum"] = sorted(registry)
    return schema


def project_legacy_graph_delta(raw: dict[str, Any]) -> dict[str, Any]:
    """Independent, explicit migration evidence; this never rejudges an old receipt.

    The original proposal remains byte-for-byte represented by its hash. Only
    model-owned business edits are projected. Unrecognized executable metadata
    is rejected rather than silently dropped. Caller must separately compile
    and persist a NEW validation receipt, never overwrite the old failed one.
    """
    legacy = GraphDelta.model_validate(raw)
    operations: list[dict[str, Any]] = []
    stripped: list[dict[str, Any]] = []
    for index, step in enumerate(legacy.operations):
        if isinstance(step, InsertNode):
            if step.node.parameters:
                raise ValueError("旧新增节点含执行参数，不能只剥离权限字段迁移")
            operations.append(
                InsertRegisteredRole(
                    node_id=step.node.id,
                    operation=step.node.operation,
                    source_node=step.budget_source_node,
                    guidance=step.node.guidance,
                ).model_dump(mode="json")
            )
            stripped.append(
                {
                    "operation_index": index,
                    "fields": {
                        key: getattr(step.node, key)
                        for key in (
                            "product_key",
                            "problem_key",
                            "role_version",
                            "context_bindings",
                            "required",
                        )
                    },
                }
            )
        elif isinstance(step, ReplaceRole):
            operations.append(
                ReplaceRegisteredRole(node_id=step.node_id, operation=step.operation).model_dump(
                    mode="json"
                )
            )
            stripped.append(
                {"operation_index": index, "fields": {"role_version": step.role_version}}
            )
        else:
            operations.append(step.model_dump(mode="json"))
    edit = GraphEditIR.model_validate(
        {
            "expected_graph_hash": legacy.expected_graph_hash,
            "rationale": legacy.rationale,
            "operations": operations,
        }
    )
    payload = {
        "protocol": "legacy-proposal-business-projection-v1",
        "legacy_proposal": raw,
        "legacy_proposal_hash": content_hash(raw),
        "edit": edit.model_dump(mode="json"),
        "edit_hash": content_hash(edit),
        "stripped_host_fields": stripped,
        "old_status": "unchanged",
        "validation_status": "not_performed",
    }
    return {**payload, "projection_hash": content_hash(payload)}


DEFAULT_PROTECTED_OPERATIONS = frozenset({"validate_decisions", "coverage_audit"})


def validate_narrative_obligations(spec: GraphSpecV2) -> None:
    """Constrained dialogue must keep mechanical coverage on the published artifact."""
    by_id = {node.id: node for node in spec.nodes}
    if not any(node.operation == "dialogue_constrained" for node in spec.nodes):
        return
    wires = {(edge.target_node, edge.target_port): edge for edge in spec.data_edges}
    for node in spec.nodes:
        if node.operation not in {"bind_dialogue", "coverage_audit"}:
            continue
        for port, source_operation, source_port in (
            ("choices", "dialogue_constrained", "choices"),
            ("decisions", "logic_contract", "decisions"),
        ):
            edge = wires.get((node.id, port))
            if (
                edge is None
                or by_id[edge.source_node].operation != source_operation
                or (edge.source_port != source_port)
            ):
                raise ValueError("约束对白必须将真实choices/decisions交给绑定和最终审计")
        if node.operation == "coverage_audit":
            bound = wires.get((node.id, "bound"))
            if bound is None or by_id[bound.source_node].operation != "bind_dialogue":
                raise ValueError("覆盖审计必须消费实际绑定产物")
            for port in ("choices", "decisions"):
                final = wires[(node.id, port)]
                original = wires.get((bound.source_node, port))
                if original is None or (original.source_node, original.source_port) != (
                    final.source_node,
                    final.source_port,
                ):
                    raise ValueError("覆盖审计与实际绑定必须消费同一份映射")


def _signature(port: PortSpec) -> tuple[str, int]:
    return port.type_name, port.schema_version


def _required(role: RoleContract) -> set[str]:
    return (
        set(role.required_inputs)
        if role.required_inputs is not None
        else {key for key, value in role.inputs.items() if value.required}
    )


def _consumed(role: RoleContract) -> set[str]:
    return (
        set(role.consumed_inputs)
        if role.consumed_inputs is not None
        else {key for key, value in role.inputs.items() if value.consumed}
    )


def _preserve_contract(old: RoleContract, new: RoleContract) -> None:
    """Registered replacements may strengthen, not drop old typed obligations."""
    for name, port in old.outputs.items():
        if name not in new.outputs or _signature(port) != _signature(new.outputs[name]):
            raise ValueError("角色替换不能削弱既有输出合同")
    for name in _required(old):
        if (
            name not in _required(new)
            or name not in _consumed(new)
            or _signature(old.inputs[name]) != _signature(new.inputs[name])
        ):
            raise ValueError("角色替换不能删除或削弱必需消费端口")


def _dependencies(spec: GraphSpecV2, *, data_only: bool = False) -> nx.DiGraph:
    graph = nx.DiGraph()
    graph.add_nodes_from(node.id for node in spec.nodes)
    graph.add_edges_from((edge.source_node, edge.target_node) for edge in spec.data_edges)
    if not data_only:
        graph.add_edges_from((edge.source_node, edge.target_node) for edge in spec.control_edges)
    return graph


def _material(spec: GraphSpecV2) -> dict[str, Any]:
    return {
        "nodes": sorted(
            (node.model_dump(mode="json") for node in spec.nodes), key=lambda n: n["id"]
        ),
        "data": sorted(
            (edge.source_node, edge.source_port, edge.target_node, edge.target_port)
            for edge in spec.data_edges
        ),
        "control": sorted((edge.source_node, edge.target_node) for edge in spec.control_edges),
    }


def _protected(node: NodeSpecV2, roles: Mapping[str, RoleContract], names: frozenset[str]) -> bool:
    return node.operation in names or any(
        port.type_name.startswith("validated_") for port in roles[node.operation].outputs.values()
    )


def _preserve_gates(
    before: GraphSpecV2,
    after: GraphSpecV2,
    roles: Mapping[str, RoleContract],
    protected_operations: frozenset[str],
) -> None:
    protected = {node.id for node in before.nodes if _protected(node, roles, protected_operations)}
    data_graph = _dependencies(after, data_only=True)
    all_graph = _dependencies(after)
    final_wires = {(edge.target_node, edge.target_port): edge for edge in after.data_edges}
    for edge in before.data_edges:
        if edge.source_node not in protected and edge.target_node not in protected:
            continue
        replacement = final_wires.get((edge.target_node, edge.target_port))
        if replacement is None or not (
            replacement.source_node == edge.source_node
            or nx.has_path(data_graph, edge.source_node, replacement.source_node)
        ):
            raise ValueError("不能绕过强制门禁或删除门禁已消费的证据端口")
    for control_edge in before.control_edges:
        if (
            control_edge.source_node in protected or control_edge.target_node in protected
        ) and not nx.has_path(all_graph, control_edge.source_node, control_edge.target_node):
            raise ValueError("不能绕过既有强制控制门禁")


def apply_graph_delta(
    spec: GraphSpecV2 | dict[str, Any],
    delta: GraphDelta | dict[str, Any],
    registry: Mapping[str, RoleContract],
    context_ports: Mapping[str, PortSpec],
    *,
    type_validators: Mapping[str, TypeValidator] | None = None,
    protected_operations: frozenset[str] = DEFAULT_PROTECTED_OPERATIONS,
) -> AppliedGraphDelta:
    """Atomically derive a new graph after compiled-hash CAS and full validation.

    All edits are evaluated on a private snapshot and compiled together. This
    permits a disconnect/connect pair without exposing an invalid intermediate
    graph. Inserted nodes inherit an original node's budget identity, including
    optional new model roles; a new instance never manufactures another quota.
    Host code persists this receipt and uses a new runtime/checkpoint thread.
    """
    parent = compile_graph(spec, registry, context_ports, type_validators=type_validators)
    patch = GraphDelta.model_validate(
        delta.model_dump(mode="json") if isinstance(delta, GraphDelta) else delta
    )
    if patch.expected_graph_hash != parent.graph_hash:
        raise ValueError("工作图CAS失败：源图或角色合同已改变")
    original = {node.id: node for node in parent.spec.nodes}
    nodes = dict(original)
    used_ids = set(original)
    data_edges = list(parent.spec.data_edges)
    control_edges = list(parent.spec.control_edges)
    changed_nodes: set[str] = set()
    inserted: set[str] = set()
    roles = parent.registry
    for step in patch.operations:
        if isinstance(step, InsertNode):
            if step.node.id in used_ids:
                raise ValueError("不能覆盖或重复使用节点实例ID")
            inserted_role = roles.get(step.node.operation)
            if inserted_role is None or inserted_role.version != step.node.role_version:
                raise ValueError("新增节点使用未注册角色或版本")
            owner = original.get(step.budget_source_node)
            if owner is None or (step.node.product_key, step.node.problem_key) != (
                owner.product_key,
                owner.problem_key,
            ):
                raise ValueError("新增节点必须继承源节点的稳定预算身份")
            nodes[step.node.id] = step.node.model_copy(deep=True)
            used_ids.add(step.node.id)
            inserted.add(step.node.id)
            changed_nodes.add(step.node.id)
        elif isinstance(step, RemoveNode):
            existing = nodes.get(step.node_id)
            if existing is None:
                raise ValueError("删除操作引用未知节点")
            if existing.required or _protected(existing, roles, protected_operations):
                raise ValueError("不能删除必需产物或强制检查节点")
            if any(ref.node_id == step.node_id for ref in parent.spec.outputs.values()):
                raise ValueError("不能删除图出口生产节点")
            del nodes[step.node_id]
            changed_nodes.add(step.node_id)
        elif isinstance(step, UpdateGuidance):
            existing = nodes.get(step.node_id)
            if existing is None:
                raise ValueError("指导修改引用未知节点")
            if _protected(existing, roles, protected_operations):
                raise ValueError("不能改写强制检查角色的指导")
            if existing.guidance == step.guidance:
                raise ValueError("指导修改没有实际变化")
            nodes[step.node_id] = NodeSpecV2.model_validate(
                {**existing.model_dump(mode="json"), "guidance": step.guidance}
            )
            changed_nodes.add(step.node_id)
        elif isinstance(step, ReplaceRole):
            existing = nodes.get(step.node_id)
            replacement = roles.get(step.operation)
            if existing is None or replacement is None or replacement.version != step.role_version:
                raise ValueError("替换操作引用未知节点、角色或版本")
            if _protected(existing, roles, protected_operations):
                raise ValueError("不能替换强制检查角色")
            if (existing.operation, existing.role_version) == (step.operation, step.role_version):
                raise ValueError("角色替换没有实际变化")
            _preserve_contract(roles[existing.operation], replacement)
            nodes[step.node_id] = existing.model_copy(
                update={"operation": step.operation, "role_version": step.role_version}
            )
            changed_nodes.add(step.node_id)
        elif isinstance(step, ConnectData):
            if step.edge in data_edges:
                raise ValueError("数据边已经存在")
            data_edges.append(step.edge)
            changed_nodes.add(step.edge.target_node)
        elif isinstance(step, DisconnectData):
            if step.edge not in data_edges:
                raise ValueError("待断开的数据边不存在")
            data_edges.remove(step.edge)
            changed_nodes.add(step.edge.target_node)
        elif isinstance(step, ConnectControl):
            if step.edge in control_edges:
                raise ValueError("控制边已经存在")
            control_edges.append(step.edge)
            changed_nodes.add(step.edge.target_node)
        elif isinstance(step, DisconnectControl):
            if step.edge not in control_edges:
                raise ValueError("待断开的控制边不存在")
            control_edges.remove(step.edge)
            changed_nodes.add(step.edge.target_node)

    child = GraphSpecV2(
        id=uuid4().hex,
        version=parent.spec.version + 1,
        parent_graph_id=parent.spec.id,
        name=parent.spec.name,
        nodes=list(nodes.values()),
        data_edges=data_edges,
        control_edges=control_edges,
        outputs=parent.spec.outputs,
    )
    if content_hash(_material(parent.spec)) == content_hash(_material(child)):
        raise ValueError("delta没有产生实际结构变化")
    compiled = compile_graph(child, roles, context_ports, type_validators=type_validators)
    validate_narrative_obligations(compiled.spec)
    _preserve_gates(parent.spec, child, roles, protected_operations)
    dependencies = _dependencies(child)
    destinations = {ref.node_id for ref in child.outputs.values()}
    for node in child.nodes:
        if (node.required or node.id in inserted) and not any(
            node.id == destination or nx.has_path(dependencies, node.id, destination)
            for destination in destinations
        ):
            raise ValueError("必需或新增节点不能脱离已发布产物的依赖路径")
        if node.id in inserted and not _protected(node, roles, protected_operations):
            if not any(edge.source_node == node.id for edge in child.data_edges):
                raise ValueError("新增非门禁节点的输出必须被实际数据端口消费")
    old_dependencies = _dependencies(parent.spec)
    affected = set(changed_nodes)
    for node_id in changed_nodes:
        if node_id in dependencies:
            affected.update(nx.descendants(dependencies, node_id))
        if node_id in old_dependencies:
            affected.update(nx.descendants(old_dependencies, node_id))
    affected.intersection_update(nodes)
    return AppliedGraphDelta(
        graph=compiled.spec,
        parent_graph_hash=parent.graph_hash,
        graph_hash=compiled.graph_hash,
        delta_hash=content_hash(patch),
        changes=[step.model_dump(mode="json") for step in patch.operations],
        affected_nodes=sorted(affected),
    )


def baseline_to_constrained_delta(spec: GraphSpecV2) -> GraphDelta:
    """An inspectable initial proposal, not an opaque replacement by a template."""
    roles = narrative_role_registry()
    compiled = compile_graph(spec, roles, {"task": PortSpec(type_name="task_context")})
    by_role = {node.operation: node for node in compiled.spec.nodes}
    needed = {
        "plan_events",
        "draft_quest",
        "draft_logic",
        "draft_dialogue",
        "bind_dialogue",
        "coverage_audit",
    }
    if not needed.issubset(by_role) or "validate_decisions" in by_role:
        raise ValueError("初始升级需要未加决策门禁的制作基线")
    plan = by_role["plan_events"]
    logic = by_role["draft_logic"]
    dialogue = by_role["draft_dialogue"]
    bind = by_role["bind_dialogue"]
    audit = by_role["coverage_audit"]
    gate_id = "decision_gate"
    if any(node.id == gate_id for node in spec.nodes):
        raise ValueError("初始门禁节点ID已被占用，请使用显式delta")
    operations: list[GraphOperation] = [
        ReplaceRole(node_id=logic.id, operation="logic_contract"),
        ReplaceRole(node_id=dialogue.id, operation="dialogue_constrained"),
        InsertNode(
            node=NodeSpecV2(
                id=gate_id,
                operation="validate_decisions",
                product_key=logic.product_key,
                problem_key=logic.problem_key,
            ),
            budget_source_node=logic.id,
        ),
    ]
    for source, source_port, target, target_port in [
        (plan.id, "plan", logic.id, "plan"),
        (plan.id, "plan", gate_id, "plan"),
        (logic.id, "logic", gate_id, "logic"),
        (logic.id, "decisions", gate_id, "decisions"),
        (gate_id, "contract", dialogue.id, "contract"),
        (logic.id, "decisions", bind.id, "decisions"),
        (dialogue.id, "choices", bind.id, "choices"),
        (logic.id, "decisions", audit.id, "decisions"),
        (dialogue.id, "choices", audit.id, "choices"),
    ]:
        operations.append(
            ConnectData(
                edge=DataEdge(
                    source_node=source,
                    source_port=source_port,
                    target_node=target,
                    target_port=target_port,
                )
            )
        )
    return GraphDelta(
        expected_graph_hash=compiled.graph_hash,
        rationale="先生成并确定性检查决策状态合同，再让对白实际消费合同并审计绑定产物",
        operations=operations,
    )
