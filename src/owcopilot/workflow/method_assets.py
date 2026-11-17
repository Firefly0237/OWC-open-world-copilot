"""Typed executable method fragments, composed into the registered host contract."""

from __future__ import annotations

from collections import Counter
from typing import Literal

import networkx as nx
from pydantic import BaseModel, ConfigDict, Field

from ..content.hash import content_hash
from .graph_v2 import (
    ControlEdge,
    DataEdge,
    GraphSpecV2,
    PortSpec,
    compile_graph,
    create_narrative_graph,
    narrative_role_registry,
)

_MACHINE_OPERATIONS = {"validate_decisions", "bind_dialogue", "coverage_audit"}


class MethodAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1] = 1
    id: str
    kind: Literal["graph", "subgraph", "skill"]
    source_version_id: str
    source_version_hash: str
    source_graph_hash: str
    node_ids: list[str] = Field(min_length=1, max_length=32)
    imports: list[dict] = Field(default_factory=list)
    exports: list[dict] = Field(default_factory=list)
    role_contract_hash: str


def _compiled(spec):
    return compile_graph(
        spec, narrative_role_registry(), {"task": PortSpec(type_name="task_context")}
    )


def extract_asset(version: dict, kind: str, node_ids: list[str] | None = None) -> MethodAsset:
    spec = GraphSpecV2.model_validate(version["graph"])
    _compiled(spec)
    ids = [node.id for node in spec.nodes] if node_ids is None else node_ids
    selected = set(ids)
    nodes = {node.id: node for node in spec.nodes}
    if len(ids) != len(selected) or not selected <= nodes.keys():
        raise ValueError("片段含重复或不存在的节点")
    if kind == "graph" and selected != nodes.keys():
        raise ValueError("整图资产必须保留完整节点")
    if kind == "skill" and len(ids) != 1:
        raise ValueError("技能资产必须是一个真实操作")
    if kind == "subgraph":
        dependency = nx.Graph()
        dependency.add_nodes_from(ids)
        dependency.add_edges_from(
            (e.source_node, e.target_node)
            for e in spec.data_edges
            if e.source_node in selected and e.target_node in selected
        )
        dependency.add_edges_from(
            (e.source_node, e.target_node)
            for e in spec.control_edges
            if e.source_node in selected and e.target_node in selected
        )
        if len(ids) < 2 or not nx.is_connected(dependency):
            raise ValueError("子图必须有真实连接的多个操作")
    if kind != "graph" and any(nodes[key].operation in _MACHINE_OPERATIONS for key in ids):
        raise ValueError("技术门禁和绑定由宿主保留，不能以局部资产替换")
    imports = [
        e.model_dump()
        for e in spec.data_edges
        if e.target_node in selected and e.source_node not in selected
    ]
    exports = [
        e.model_dump()
        for e in spec.data_edges
        if e.source_node in selected and e.target_node not in selected
    ]
    registry = narrative_role_registry()
    material = {
        "kind": kind,
        "source_version_id": version["id"],
        "source_version_hash": version["version_hash"],
        "source_graph_hash": content_hash(version["graph"]),
        "node_ids": ids,
        "imports": imports,
        "exports": exports,
        "role_contract_hash": content_hash(
            {key: value.model_dump() for key, value in registry.items()}
        ),
    }
    return MethodAsset(id=content_hash(material), **material)


def compose_asset(version: dict, raw: dict | MethodAsset) -> tuple[GraphSpecV2, dict]:
    asset = MethodAsset.model_validate(raw)
    expected = extract_asset(version, asset.kind, asset.node_ids)
    if asset != expected:
        raise ValueError("资产内容、来源版本或角色合同已变化")
    source = GraphSpecV2.model_validate(version["graph"])
    if asset.kind == "graph":
        return source, {
            "asset": asset.model_dump(),
            "execution_graph_hash": _compiled(source).graph_hash,
            "composition": "whole_registered_graph",
        }
    selected = {node.id for node in source.nodes if node.id in asset.node_ids}
    operations = {node.operation for node in source.nodes}
    host = create_narrative_graph("constrained" if "logic_contract" in operations else "baseline")
    source_by_id = {node.id: node for node in source.nodes}
    counts = Counter(node.operation for node in source.nodes)
    host_by_operation = {node.operation: node for node in host.nodes}
    # Every imported dependency must have one unambiguous, typed host producer.
    relevant_ids = selected | {
        e.source_node for e in source.data_edges if e.target_node in selected
    }
    relevant_ids |= {e.source_node for e in source.control_edges if e.target_node in selected}
    relevant_ids |= {e.target_node for e in source.control_edges if e.source_node in selected}
    if any(
        counts[source_by_id[key].operation] != 1
        or source_by_id[key].operation not in host_by_operation
        for key in relevant_ids
    ):
        raise ValueError("片段边界无法无歧义绑定宿主操作")
    mapping = {key: host_by_operation[source_by_id[key].operation].id for key in relevant_ids}
    targets = {mapping[key] for key in selected}
    replacements = {}
    for key in selected:
        node = source_by_id[key]
        target = host_by_operation[node.operation]
        replacements[target.id] = node.model_copy(
            update={
                "id": target.id,
                "product_key": target.product_key,
                "problem_key": target.problem_key,
                "parameters": {
                    **node.parameters,
                    "asset_binding": {
                        "asset_id": asset.id,
                        "source_version_id": version["id"],
                        "source_version_hash": version["version_hash"],
                        "source_node_id": key,
                    },
                },
            }
        )
    data = [edge for edge in host.data_edges if edge.target_node not in targets]
    data.extend(
        DataEdge(
            source_node=mapping[e.source_node],
            source_port=e.source_port,
            target_node=mapping[e.target_node],
            target_port=e.target_port,
        )
        for e in source.data_edges
        if e.target_node in selected
    )
    control = list(host.control_edges)
    control.extend(
        ControlEdge(source_node=mapping[e.source_node], target_node=mapping[e.target_node])
        for e in source.control_edges
        if e.source_node in selected or e.target_node in selected
    )
    control = list({(e.source_node, e.target_node): e for e in control}.values())
    # Stable asset identity also makes frozen selection observations reproducible.
    result = host.model_copy(
        update={
            "id": "asset-" + asset.id[:32],
            "parent_graph_id": source.id,
            "name": "组合工作方法",
            "nodes": [replacements.get(node.id, node) for node in host.nodes],
            "data_edges": data,
            "control_edges": control,
        }
    )
    compiled = _compiled(result)
    return result, {
        "asset": asset.model_dump(),
        "execution_graph_hash": compiled.graph_hash,
        "composition": "typed_fragment_in_registered_host",
        "bound_nodes": sorted(targets),
        "mandatory_host_operations": sorted(_MACHINE_OPERATIONS),
    }


def derive_assets(version: dict) -> list[MethodAsset]:
    """Three explicit grains; unsupported boundaries are rejected, never approximated."""
    spec = GraphSpecV2.model_validate(version["graph"])
    result = [extract_asset(version, "graph")]
    production = [node for node in spec.nodes if node.operation not in _MACHINE_OPERATIONS]
    for edge in spec.data_edges:
        ids = [edge.source_node, edge.target_node]
        if all(any(node.id == key for node in production) for key in ids):
            candidate = extract_asset(version, "subgraph", ids)
            try:
                compose_asset(version, candidate)
            except ValueError:
                continue
            result.append(candidate)
            break
    ordered = sorted(
        production,
        key=lambda node: (
            node.operation not in {"dialogue_constrained", "draft_dialogue"},
            node.id,
        ),
    )
    for node in ordered:
        candidate = extract_asset(version, "skill", [node.id])
        try:
            compose_asset(version, candidate)
        except ValueError:
            continue
        result.append(candidate)
        break
    return result


def bind_asset(version: dict, raw: dict | MethodAsset, adaptation: str | None = None):
    """Only a diagnosed, host-defined capability adaptation may change topology here."""
    graph, binding = compose_asset(version, raw)
    if adaptation is None:
        return graph, binding
    if adaptation != "decision_contract":
        raise ValueError("未注册的任务内适配")
    from .graph_delta import apply_graph_delta, baseline_to_constrained_delta

    delta = baseline_to_constrained_delta(graph)
    applied = apply_graph_delta(
        graph, delta, narrative_role_registry(), {"task": PortSpec(type_name="task_context")}
    )
    graph = applied.graph.model_copy(
        update={"id": "adapt-" + content_hash([binding["asset"]["id"], adaptation])[:32]}
    )
    binding = {
        **binding,
        "execution_graph_hash": _compiled(graph).graph_hash,
        "adaptation": {
            "kind": adaptation,
            "delta": delta.model_dump(mode="json"),
            "affected_nodes": applied.affected_nodes,
            "parent_graph_hash": applied.parent_graph_hash,
        },
    }
    return graph, binding
