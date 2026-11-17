"""Versioned typed data-flow graphs; graph identity never grants a model budget.

The compiler follows the LangGraph graph/data dependency model. This module is a
host-controlled adapter: handlers own actual model/tool work and must use the Harness.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Self
from uuid import uuid4

import networkx as nx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..content.hash import content_hash
from ..content.models import DialogueTree, Quest
from .models import EventPlan


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PortSpec(_Model):
    type_name: str = Field(min_length=1)
    schema_version: int = Field(default=1, ge=1, strict=True)
    required: bool = True
    consumed: bool = True


class RoleContract(_Model):
    operation: str = Field(min_length=1)
    version: str = "1"
    inputs: dict[str, PortSpec]
    outputs: dict[str, PortSpec]
    required_inputs: list[str] | None = None
    consumed_inputs: list[str] | None = None

    @model_validator(mode="after")
    def known_ports(self) -> Self:
        for values in (self.required_inputs, self.consumed_inputs):
            if values is not None and (
                len(set(values)) != len(values) or set(values) - self.inputs.keys()
            ):
                raise ValueError("角色声明了重复或未知输入端口")
        if not self.outputs:
            raise ValueError("角色必须声明输出端口")
        return self


class NodeSpecV2(_Model):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    operation: str = Field(min_length=1)
    role_version: str = "1"
    product_key: str = Field(min_length=1)
    problem_key: str = Field(min_length=1)
    context_bindings: dict[str, str] = Field(default_factory=dict)
    guidance: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    required: bool = True


class DataEdge(_Model):
    source_node: str
    source_port: str
    target_node: str
    target_port: str


class ControlEdge(_Model):
    source_node: str
    target_node: str


class OutputRef(_Model):
    node_id: str
    port: str


class GraphSpecV2(_Model):
    schema_version: Literal[2] = 2
    id: str = Field(default_factory=lambda: uuid4().hex)
    version: int = Field(default=1, ge=1, strict=True)
    parent_graph_id: str | None = None
    name: str = Field(min_length=1)
    nodes: list[NodeSpecV2] = Field(min_length=1, max_length=32)
    data_edges: list[DataEdge] = Field(default_factory=list, max_length=128)
    control_edges: list[ControlEdge] = Field(default_factory=list, max_length=64)
    outputs: dict[str, OutputRef] = Field(default_factory=dict)

    @model_validator(mode="after")
    def instance_ids(self) -> Self:
        if len({node.id for node in self.nodes}) != len(self.nodes):
            raise ValueError("图节点实例ID重复")
        if self.parent_graph_id == self.id:
            raise ValueError("图不能是自己的父版本")
        return self


TypeValidator = Callable[[Any], Any]
Handler = Callable[[NodeSpecV2, dict[str, Any]], dict[str, Any]]
Emitter = Callable[..., Any]


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("端口需要JSON对象")
    json.dumps(value, allow_nan=False)
    return copy.deepcopy(value)


def _quest(value: Any) -> dict[str, Any]:
    return Quest.model_validate(value).model_dump(mode="json")


def _tree(value: Any) -> dict[str, Any]:
    return DialogueTree.model_validate(value).model_dump(mode="json")


def _bound(value: Any) -> dict[str, Any]:
    result = _object(value)
    if set(result) != {"quest", "tree"}:
        raise ValueError("绑定产物只接受quest与tree")
    result["quest"] = _quest(result["quest"])
    result["tree"] = _tree(result["tree"])
    if result["tree"].get("quest_id") != result["quest"]["id"]:
        raise ValueError("绑定对白与任务不匹配")
    return result


def default_type_validators() -> dict[str, TypeValidator]:
    from .decision_contract import ChoiceMap, parse_decision_mapping, validate_contract_snapshot

    return {
        "json": lambda value: json.loads(json.dumps(value, allow_nan=False)),
        "task_context": _object,
        "event_plan": lambda value: EventPlan.model_validate(value).model_dump(mode="json"),
        "quest": _quest,
        "dialogue_tree": _tree,
        "bound_dialogue": _bound,
        "decision_effect_map": lambda value: parse_decision_mapping(value).model_dump(
            mode="json"
        ),
        "choice_map": lambda value: ChoiceMap.model_validate(value).model_dump(mode="json"),
        "validated_decision_contract": validate_contract_snapshot,
        "coverage_report": _object,
    }


@dataclass(frozen=True)
class CompiledGraph:
    spec: GraphSpecV2
    registry: dict[str, RoleContract]
    context_ports: dict[str, PortSpec]
    order: tuple[str, ...]
    graph_hash: str
    type_validators: dict[str, TypeValidator] = field(repr=False)


def compile_graph(
    spec: GraphSpecV2 | dict[str, Any],
    registry: Mapping[str, RoleContract],
    context_ports: Mapping[str, PortSpec],
    *,
    type_validators: Mapping[str, TypeValidator] | None = None,
) -> CompiledGraph:
    spec = GraphSpecV2.model_validate(
        spec.model_dump(mode="json") if isinstance(spec, GraphSpecV2) else spec
    )
    roles = {
        key: RoleContract.model_validate(value.model_dump(mode="json"))
        for key, value in registry.items()
    }
    validators = default_type_validators()
    validators.update(type_validators or {})
    by_id = {node.id: node for node in spec.nodes}
    graph = nx.DiGraph()
    graph.add_nodes_from(by_id)
    wired: set[tuple[str, str]] = set()
    for node in spec.nodes:
        role = roles.get(node.operation)
        if role is None or role.operation != node.operation or role.version != node.role_version:
            raise ValueError(f"未注册的角色或角色版本：{node.operation}@{node.role_version}")
        for port in [*role.inputs.values(), *role.outputs.values()]:
            if port.type_name not in validators or port.schema_version != 1:
                raise ValueError("未注册的端口schema或版本")
        for target, source in node.context_bindings.items():
            target_port = role.inputs.get(target)
            source_port = context_ports.get(source)
            if (
                target_port is None
                or source_port is None
                or not _compatible(source_port, target_port)
            ):
                raise ValueError("上下文输入绑定端口或类型不合法")
            _consume(role, target)
            wired.add((node.id, target))
    for edge in spec.data_edges:
        if edge.source_node not in by_id or edge.target_node not in by_id:
            raise ValueError("数据边引用未知节点")
        source_role = roles[by_id[edge.source_node].operation]
        target_role = roles[by_id[edge.target_node].operation]
        source_spec = source_role.outputs.get(edge.source_port)
        target_spec = target_role.inputs.get(edge.target_port)
        if source_spec is None or target_spec is None or not _compatible(source_spec, target_spec):
            raise ValueError("数据边端口不存在或schema不匹配")
        _consume(target_role, edge.target_port)
        key = (edge.target_node, edge.target_port)
        if key in wired:
            raise ValueError("一个输入端口不能由多个来源覆盖")
        wired.add(key)
        graph.add_edge(edge.source_node, edge.target_node)
    for control_edge in spec.control_edges:
        if control_edge.source_node not in by_id or control_edge.target_node not in by_id:
            raise ValueError("控制边引用未知节点")
        graph.add_edge(control_edge.source_node, control_edge.target_node)
    if not nx.is_directed_acyclic_graph(graph):
        raise ValueError("工作图必须是DAG；有界修订由外层episode管理")
    for node in spec.nodes:
        role = roles[node.operation]
        required = _required(role)
        if any((node.id, name) not in wired for name in required):
            raise ValueError(f"节点缺少必需输入端口：{node.id}")
    for output in spec.outputs.values():
        if (
            output.node_id not in by_id
            or output.port not in roles[by_id[output.node_id].operation].outputs
        ):
            raise ValueError("图输出引用未知节点或端口")
    contract_hash = content_hash(
        {
            key: value.model_dump(mode="json")
            for key, value in roles.items()
            if key in {node.operation for node in spec.nodes}
        }
    )
    return CompiledGraph(
        spec,
        roles,
        dict(context_ports),
        tuple(nx.lexicographical_topological_sort(graph)),
        content_hash({"spec": spec, "contracts": contract_hash}),
        validators,
    )


def _required(role: RoleContract) -> set[str]:
    return (
        set(role.required_inputs)
        if role.required_inputs is not None
        else {name for name, port in role.inputs.items() if port.required}
    )


def _consume(role: RoleContract, name: str) -> None:
    consumed = (
        set(role.consumed_inputs)
        if role.consumed_inputs is not None
        else {key for key, port in role.inputs.items() if port.consumed}
    )
    if name not in consumed:
        raise ValueError("数据边接入未被消费的输入端口")


def _compatible(source: PortSpec, target: PortSpec) -> bool:
    return (source.type_name, source.schema_version) == (target.type_name, target.schema_version)


class PortArtifact(_Model):
    id: str = Field(default_factory=lambda: uuid4().hex)
    producer_node: str
    output_port: str
    type_name: str
    schema_version: int = 1
    value: Any
    content_hash: str
    signature: str


class NodeOutcomeV2(_Model):
    node_id: str
    operation: str
    product_key: str
    problem_key: str
    status: Literal["succeeded", "reused", "failed", "blocked_dependency", "cancelled"]
    signature: str = ""
    input_refs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, PortArtifact] = Field(default_factory=dict)
    error: str | None = None


class GraphExecutionResult(_Model):
    graph_id: str
    graph_version: int
    graph_hash: str
    status: Literal["completed", "completed_with_skips", "cancelled", "waiting_user"]
    outcomes: dict[str, NodeOutcomeV2]
    artifacts: dict[str, PortArtifact]
    outputs: dict[str, Any]


def execute_graph(
    compiled: CompiledGraph,
    context: dict[str, Any],
    handlers: Mapping[str, Handler],
    *,
    emit: Emitter | None = None,
    cancelled: Callable[[], bool] | None = None,
    prior: GraphExecutionResult | dict[str, Any] | None = None,
    context_identity: dict[str, Any] | None = None,
) -> GraphExecutionResult:
    """Execute an immutable graph snapshot; no retries or model dispatches are added here."""
    notify = emit or (lambda *_args, **_kwargs: None)
    is_cancelled = cancelled or (lambda: False)
    by_id = {node.id: node for node in compiled.spec.nodes}
    outcomes: dict[str, NodeOutcomeV2] = {}
    artifacts: dict[str, PortArtifact] = {}
    previous = GraphExecutionResult.model_validate(prior) if prior is not None else None
    frozen_context = copy.deepcopy(context)
    notify("graph.started", {"graph_id": compiled.spec.id, "graph_hash": compiled.graph_hash})
    for node_id in compiled.order:
        node = by_id[node_id]
        role = compiled.registry[node.operation]
        base: dict[str, Any] = {
            "node_id": node.id,
            "operation": node.operation,
            "product_key": node.product_key,
            "problem_key": node.problem_key,
        }
        if is_cancelled():
            outcomes[node_id] = NodeOutcomeV2(**base, status="cancelled")
            continue
        inputs: dict[str, Any] = {}
        refs: dict[str, Any] = {}
        failed_parent = False
        for control_edge in compiled.spec.control_edges:
            if control_edge.target_node == node.id and outcomes[
                control_edge.source_node
            ].status not in {
                "succeeded",
                "reused",
            }:
                failed_parent = True
        for data_edge in compiled.spec.data_edges:
            if data_edge.target_node != node.id:
                continue
            parent = outcomes[data_edge.source_node]
            if parent.status not in {"succeeded", "reused"}:
                if data_edge.target_port in _required(role):
                    failed_parent = True
                continue
            artifact = parent.outputs[data_edge.source_port]
            inputs[data_edge.target_port] = copy.deepcopy(artifact.value)
            refs[data_edge.target_port] = {
                "artifact_id": artifact.id,
                "content_hash": artifact.content_hash,
                "source_node": data_edge.source_node,
                "source_port": data_edge.source_port,
                "signature": artifact.signature,
            }
        if failed_parent:
            outcomes[node_id] = NodeOutcomeV2(
                **base, status="blocked_dependency", error="必需上游未成功"
            )
            notify("graph.node.blocked", outcomes[node_id].model_dump(mode="json"), node_id=node.id)
            continue
        signature = ""
        try:
            for target, source in node.context_bindings.items():
                if source not in frozen_context:
                    raise ValueError(f"缺少冻结上下文：{source}")
                inputs[target] = copy.deepcopy(frozen_context[source])
                refs[target] = {"context_key": source, "content_hash": content_hash(inputs[target])}
            inputs = {
                key: compiled.type_validators[role.inputs[key].type_name](value)
                for key, value in inputs.items()
            }
            signature = content_hash(
                {
                    "product_key": node.product_key,
                    "problem_key": node.problem_key,
                    "operation": node.operation,
                    "contract": role,
                    "guidance": node.guidance,
                    "parameters": node.parameters,
                    "inputs": inputs,
                    "input_provenance": {
                        key: {
                            field: value for field, value in ref.items() if field != "source_node"
                        }
                        for key, ref in refs.items()
                    },
                    "context_identity": context_identity or {},
                }
            )
            prior_node = (
                next(
                    (
                        item
                        for item in previous.outcomes.values()
                        if item.signature == signature
                        and item.product_key == node.product_key
                        and item.problem_key == node.problem_key
                        and item.status in {"succeeded", "reused"}
                    ),
                    None,
                )
                if previous
                else None
            )
            if prior_node is not None:
                if set(prior_node.outputs) != set(role.outputs):
                    raise ValueError("历史产物输出端口不完整")
                for name, artifact in prior_node.outputs.items():
                    port = role.outputs[name]
                    if (
                        artifact.signature != signature
                        or artifact.content_hash != content_hash(artifact.value)
                        or (artifact.type_name, artifact.schema_version)
                        != (port.type_name, port.schema_version)
                    ):
                        raise ValueError("历史产物hash或签名不匹配")
                    compiled.type_validators[port.type_name](artifact.value)
                outputs = dict(prior_node.outputs)
                outcome = NodeOutcomeV2(
                    **base, status="reused", signature=signature, input_refs=refs, outputs=outputs
                )
            else:
                handler = handlers.get(node.operation)
                if handler is None:
                    raise ValueError("宿主没有实现已注册角色")
                notify(
                    "graph.node.started",
                    {
                        **base,
                        "input_refs": refs,
                        "input_hash": content_hash(inputs),
                        "signature": signature,
                    },
                    node_id=node.id,
                )
                raw = handler(
                    NodeSpecV2.model_validate(node.model_dump(mode="json")), copy.deepcopy(inputs)
                )
                if not isinstance(raw, dict) or set(raw) != set(role.outputs):
                    raise ValueError("角色实际输出与声明端口不一致")
                outputs = {}
                for name, port in role.outputs.items():
                    value = compiled.type_validators[port.type_name](raw[name])
                    outputs[name] = PortArtifact(
                        producer_node=node.id,
                        output_port=name,
                        type_name=port.type_name,
                        schema_version=port.schema_version,
                        value=value,
                        content_hash=content_hash(value),
                        signature=signature,
                    )
                if is_cancelled():
                    outcomes[node_id] = NodeOutcomeV2(
                        **base,
                        status="cancelled",
                        signature=signature,
                        input_refs=refs,
                        error="迟到结果未发布",
                    )
                    notify(
                        "graph.node.cancelled",
                        outcomes[node_id].model_dump(mode="json"),
                        node_id=node.id,
                    )
                    continue
                outcome = NodeOutcomeV2(
                    **base,
                    status="succeeded",
                    signature=signature,
                    input_refs=refs,
                    outputs=outputs,
                )
            notify(
                "graph.node.reused" if outcome.status == "reused" else "graph.node.completed",
                outcome.model_dump(mode="json"),
                node_id=node.id,
            )
            outcomes[node_id] = outcome
            for output_name, artifact in outputs.items():
                artifacts[f"{node.id}.{output_name}"] = artifact
        except Exception as exc:
            outcomes[node_id] = NodeOutcomeV2(
                **base,
                status="failed",
                signature=signature,
                input_refs=refs,
                error=f"{type(exc).__name__}: {exc}",
            )
            notify("graph.node.failed", outcomes[node_id].model_dump(mode="json"), node_id=node.id)
    public_outputs = {}
    for name, ref in compiled.spec.outputs.items():
        outcome = outcomes[ref.node_id]
        if outcome.status in {"succeeded", "reused"}:
            public_outputs[name] = copy.deepcopy(outcome.outputs[ref.port].value)
    status: Literal["completed", "completed_with_skips", "cancelled"] = (
        "cancelled"
        if is_cancelled()
        else "completed"
        if all(item.status in {"succeeded", "reused"} for item in outcomes.values())
        else "completed_with_skips"
    )
    result = GraphExecutionResult(
        graph_id=compiled.spec.id,
        graph_version=compiled.spec.version,
        graph_hash=compiled.graph_hash,
        status=status,
        outcomes=outcomes,
        artifacts=artifacts,
        outputs=public_outputs,
    )
    notify("graph.completed", {"graph_id": result.graph_id, "status": result.status})
    return result


def rewrite_graph(
    spec: GraphSpecV2,
    *,
    nodes_upsert: list[NodeSpecV2] | tuple[NodeSpecV2, ...] = (),
    remove_nodes: list[str] | tuple[str, ...] = (),
    data_edges: list[DataEdge] | None = None,
    control_edges: list[ControlEdge] | None = None,
    outputs: dict[str, OutputRef] | None = None,
    name: str | None = None,
) -> GraphSpecV2:
    """Create a candidate version; compiler admission remains mandatory before execution."""
    nodes = {node.id: node for node in spec.nodes}
    for node_id in remove_nodes:
        if node_id not in nodes or nodes[node_id].required:
            raise ValueError("只可删除存在的可选节点")
        del nodes[node_id]
    for node in nodes_upsert:
        old = nodes.get(node.id)
        if old is not None and (old.product_key, old.problem_key) != (
            node.product_key,
            node.problem_key,
        ):
            raise ValueError("改图不能改变同一逻辑产物的问题身份")
        nodes[node.id] = node
    return GraphSpecV2(
        id=uuid4().hex,
        version=spec.version + 1,
        parent_graph_id=spec.id,
        name=name or spec.name,
        nodes=list(nodes.values()),
        data_edges=data_edges if data_edges is not None else list(spec.data_edges),
        control_edges=control_edges if control_edges is not None else list(spec.control_edges),
        outputs=outputs if outputs is not None else dict(spec.outputs),
    )


def narrative_role_registry() -> dict[str, RoleContract]:
    """Only contracts: production handlers are supplied by the host application."""

    def role(
        op: str, inputs: dict[str, str], outputs: dict[str, str], optional: tuple[str, ...] = ()
    ) -> RoleContract:
        return RoleContract(
            operation=op,
            inputs={
                name: PortSpec(type_name=kind, required=name not in optional)
                for name, kind in inputs.items()
            },
            outputs={name: PortSpec(type_name=kind) for name, kind in outputs.items()},
            consumed_inputs=list(inputs),
        )

    return {
        "plan_events": role("plan_events", {"task": "task_context"}, {"plan": "event_plan"}),
        "draft_quest": role(
            "draft_quest", {"task": "task_context", "plan": "event_plan"}, {"quest": "quest"}
        ),
        "draft_logic": role(
            "draft_logic", {"task": "task_context", "quest": "quest"}, {"logic": "quest"}
        ),
        "draft_dialogue": role(
            "draft_dialogue",
            {"task": "task_context", "plan": "event_plan"},
            {"dialogue": "dialogue_tree"},
        ),
        "logic_contract": role(
            "logic_contract",
            {"task": "task_context", "plan": "event_plan", "quest": "quest"},
            {"logic": "quest", "decisions": "decision_effect_map"},
        ),
        "validate_decisions": role(
            "validate_decisions",
            {"plan": "event_plan", "logic": "quest", "decisions": "decision_effect_map"},
            {"contract": "validated_decision_contract"},
        ),
        "dialogue_constrained": role(
            "dialogue_constrained",
            {
                "task": "task_context",
                "plan": "event_plan",
                "contract": "validated_decision_contract",
            },
            {"dialogue": "dialogue_tree", "choices": "choice_map"},
        ),
        "bind_dialogue": role(
            "bind_dialogue",
            {
                "task": "task_context",
                "logic": "quest",
                "dialogue": "dialogue_tree",
                "decisions": "decision_effect_map",
                "choices": "choice_map",
            },
            {"bound": "bound_dialogue"},
            ("decisions", "choices"),
        ),
        "coverage_audit": role(
            "coverage_audit",
            {
                "task": "task_context",
                "plan": "event_plan",
                "logic": "quest",
                "bound": "bound_dialogue",
                "decisions": "decision_effect_map",
                "choices": "choice_map",
            },
            {"report": "coverage_report"},
            ("decisions", "choices"),
        ),
    }


def create_narrative_graph(variant: Literal["baseline", "constrained"] = "baseline") -> GraphSpecV2:
    if variant not in {"baseline", "constrained"}:
        raise ValueError("未知叙事图变体")
    constrained = variant == "constrained"
    descriptions = [
        ("plan", "plan_events", "plan"),
        ("quest", "draft_quest", "quest"),
        ("logic", "logic_contract" if constrained else "draft_logic", "logic"),
        ("dialogue", "dialogue_constrained" if constrained else "draft_dialogue", "dialogue"),
        ("bind", "bind_dialogue", "binding"),
        ("audit", "coverage_audit", "audit"),
    ]
    if constrained:
        descriptions.insert(3, ("decision_gate", "validate_decisions", "decision_contract"))
    roles = narrative_role_registry()
    nodes = [
        NodeSpecV2(
            id=node_id,
            operation=operation,
            product_key=f"slice/{product}",
            problem_key=f"slice/{product}/produce",
            context_bindings={"task": "task"} if "task" in roles[operation].inputs else {},
        )
        for node_id, operation, product in descriptions
    ]

    def edge(source: str, source_port: str, target: str, target_port: str) -> DataEdge:
        return DataEdge(
            source_node=source, source_port=source_port, target_node=target, target_port=target_port
        )

    edges = [
        edge("plan", "plan", "quest", "plan"),
        edge("quest", "quest", "logic", "quest"),
        edge("plan", "plan", "dialogue", "plan"),
        edge("logic", "logic", "bind", "logic"),
        edge("dialogue", "dialogue", "bind", "dialogue"),
        edge("plan", "plan", "audit", "plan"),
        edge("logic", "logic", "audit", "logic"),
        edge("bind", "bound", "audit", "bound"),
    ]
    if constrained:
        edges.extend(
            [
                edge("plan", "plan", "logic", "plan"),
                edge("plan", "plan", "decision_gate", "plan"),
                edge("logic", "logic", "decision_gate", "logic"),
                edge("logic", "decisions", "decision_gate", "decisions"),
                edge("decision_gate", "contract", "dialogue", "contract"),
                edge("logic", "decisions", "bind", "decisions"),
                edge("dialogue", "choices", "bind", "choices"),
                edge("logic", "decisions", "audit", "decisions"),
                edge("dialogue", "choices", "audit", "choices"),
            ]
        )
    return GraphSpecV2(
        name="决策状态先行" if constrained else "制作基线",
        version=2 if constrained else 1,
        nodes=nodes,
        data_edges=edges,
        control_edges=[ControlEdge(source_node="bind", target_node="audit")],
        outputs={
            "bound": OutputRef(node_id="bind", port="bound"),
            "report": OutputRef(node_id="audit", port="report"),
        },
    )
