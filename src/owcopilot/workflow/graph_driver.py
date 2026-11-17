"""Official LangGraph/SqliteSaver driver for the typed workflow V2 contracts.

Each registered graph node is a real StateGraph node. The per-node execution below
adapts graph_v2.execute_graph's validated input/provenance/reuse algorithm; it does
not wrap a whole topological loop in one checkpoint. Checkpoints do not provide
exactly-once remote side effects: the Harness still owns persistent dispatch budgets.
"""

from __future__ import annotations

import copy
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig, RunnableLambda
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..content.hash import content_hash
from .graph_v2 import (
    CompiledGraph,
    Emitter,
    GraphExecutionResult,
    Handler,
    NodeOutcomeV2,
    NodeSpecV2,
    PortArtifact,
    RoleContract,
    compile_graph,
)


def _merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {**left, **right}


class _DriverState(TypedDict):
    outcomes: Annotated[dict[str, Any], _merge]
    artifacts: Annotated[dict[str, Any], _merge]


def _required(role: RoleContract) -> set[str]:
    if role.required_inputs is not None:
        return set(role.required_inputs)
    return {name for name, port in role.inputs.items() if port.required}


def _bind_checkpoint(path: Path, thread_id: str, graph_hash: str, context_hash: str) -> str:
    """A run cannot silently resume with a changed graph or changed frozen context."""
    if not thread_id.strip():
        raise ValueError("持久工作图需要非空运行thread_id")
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=30)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS owc_graph_thread_bindings ("
            "thread_id TEXT PRIMARY KEY, graph_hash TEXT NOT NULL, context_hash TEXT NOT NULL)"
        )
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT graph_hash,context_hash FROM owc_graph_thread_bindings WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO owc_graph_thread_bindings VALUES(?,?,?)",
                (thread_id, graph_hash, context_hash),
            )
        elif row != (graph_hash, context_hash):
            raise ValueError("运行thread已绑定不同图或上下文；新版本必须使用新的运行身份")
    return f"{thread_id}:{graph_hash}"


def _execute_node(
    compiled: CompiledGraph,
    node: NodeSpecV2,
    state: _DriverState,
    context: dict[str, Any],
    handlers: Mapping[str, Handler],
    notify: Emitter,
    is_cancelled: Callable[[], bool],
    previous: GraphExecutionResult | None,
    context_identity: dict[str, Any],
) -> dict[str, Any]:
    """One checkpointed unit; real handler invocation happens at most once here."""
    role = compiled.registry[node.operation]
    base: dict[str, Any] = {
        "node_id": node.id,
        "operation": node.operation,
        "product_key": node.product_key,
        "problem_key": node.problem_key,
    }
    if is_cancelled():
        outcome = NodeOutcomeV2(**base, status="cancelled")
        notify("graph.node.cancelled", outcome.model_dump(mode="json"), node_id=node.id)
        return {"outcomes": {node.id: outcome.model_dump(mode="json")}}
    upstream = {
        key: NodeOutcomeV2.model_validate(value) for key, value in state["outcomes"].items()
    }
    inputs: dict[str, Any] = {}
    refs: dict[str, Any] = {}
    failed_parent = False
    for control in compiled.spec.control_edges:
        if control.target_node == node.id and upstream[control.source_node].status not in {
            "succeeded",
            "reused",
        }:
            failed_parent = True
    for edge in compiled.spec.data_edges:
        if edge.target_node != node.id:
            continue
        parent = upstream[edge.source_node]
        if parent.status not in {"succeeded", "reused"}:
            if edge.target_port in _required(role):
                failed_parent = True
            continue
        artifact = parent.outputs[edge.source_port]
        inputs[edge.target_port] = copy.deepcopy(artifact.value)
        refs[edge.target_port] = {
            "artifact_id": artifact.id,
            "content_hash": artifact.content_hash,
            "source_node": edge.source_node,
            "source_port": edge.source_port,
            "signature": artifact.signature,
        }
    if failed_parent:
        outcome = NodeOutcomeV2(**base, status="blocked_dependency", error="必需上游未成功")
        notify("graph.node.blocked", outcome.model_dump(mode="json"), node_id=node.id)
        return {"outcomes": {node.id: outcome.model_dump(mode="json")}}
    signature = ""
    try:
        for target, source in node.context_bindings.items():
            if source not in context:
                raise ValueError(f"缺少冻结上下文：{source}")
            inputs[target] = copy.deepcopy(context[source])
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
                    key: {field: value for field, value in ref.items() if field != "source_node"}
                    for key, ref in refs.items()
                },
                "context_identity": context_identity,
            }
        )
        prior_node = (
            next(
                (
                    candidate
                    for candidate in previous.outcomes.values()
                    if candidate.signature == signature
                    and candidate.product_key == node.product_key
                    and candidate.problem_key == node.problem_key
                    and candidate.status in {"succeeded", "reused"}
                ),
                None,
            )
            if previous
            else None
        )
        outputs: dict[str, PortArtifact]
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
            for name, output_port in role.outputs.items():
                value = compiled.type_validators[output_port.type_name](raw[name])
                outputs[name] = PortArtifact(
                    producer_node=node.id,
                    output_port=name,
                    type_name=output_port.type_name,
                    schema_version=output_port.schema_version,
                    value=value,
                    content_hash=content_hash(value),
                    signature=signature,
                )
            if is_cancelled():
                outcome = NodeOutcomeV2(
                    **base,
                    status="cancelled",
                    signature=signature,
                    input_refs=refs,
                    error="迟到结果未发布",
                )
                notify("graph.node.cancelled", outcome.model_dump(mode="json"), node_id=node.id)
                return {"outcomes": {node.id: outcome.model_dump(mode="json")}}
            outcome = NodeOutcomeV2(
                **base, status="succeeded", signature=signature, input_refs=refs, outputs=outputs
            )
        notify(
            "graph.node.reused" if outcome.status == "reused" else "graph.node.completed",
            outcome.model_dump(mode="json"),
            node_id=node.id,
        )
        return {
            "outcomes": {node.id: outcome.model_dump(mode="json")},
            "artifacts": {
                f"{node.id}.{name}": artifact.model_dump(mode="json")
                for name, artifact in outputs.items()
            },
        }
    except Exception as exc:
        outcome = NodeOutcomeV2(
            **base,
            status="failed",
            signature=signature,
            input_refs=refs,
            error=f"{type(exc).__name__}: {exc}",
        )
        notify("graph.node.failed", outcome.model_dump(mode="json"), node_id=node.id)
        return {"outcomes": {node.id: outcome.model_dump(mode="json")}}


def execute_persistent_graph(
    compiled: CompiledGraph,
    context: dict[str, Any],
    handlers: Mapping[str, Handler],
    checkpoint_path: str | Path,
    thread_id: str,
    *,
    emit: Emitter | None = None,
    cancelled: Callable[[], bool] | None = None,
    prior: GraphExecutionResult | dict[str, Any] | None = None,
    context_identity: dict[str, Any] | None = None,
    before_node: Callable[[NodeSpecV2, dict], dict | None] | None = None,
    consume_resume: Callable[[dict, dict], None] | None = None,
    resume: dict | None = None,
) -> GraphExecutionResult:
    """Run/resume per-node checkpoints; same run identity cannot change its graph or inputs."""
    compiled = compile_graph(
        compiled.spec,
        compiled.registry,
        compiled.context_ports,
        type_validators=compiled.type_validators,
    )
    frozen_context = copy.deepcopy(context)
    frozen_identity = copy.deepcopy(context_identity or {})
    previous = GraphExecutionResult.model_validate(prior) if prior is not None else None
    notify = emit or (lambda *_args, **_kwargs: None)
    is_cancelled = cancelled or (lambda: False)
    path = Path(checkpoint_path)
    checkpoint_thread = _bind_checkpoint(
        path,
        thread_id,
        compiled.graph_hash,
        content_hash({"context": frozen_context, "identity": frozen_identity}),
    )
    graph = StateGraph(_DriverState)
    by_id = {node.id: node for node in compiled.spec.nodes}
    resumed_task_gates: dict[str, dict] = {}

    def wrap(node: NodeSpecV2) -> Callable[[_DriverState], dict[str, Any]]:
        def invoke(state: _DriverState) -> dict[str, Any]:
            # Native GraphInterrupt must escape the generic node failure catcher.
            # There are no model/tool side effects before this boundary.
            # The interrupted task must encounter the identical interrupt on
            # replay, even if a sibling has consumed their shared author gate.
            gate = resumed_task_gates.get(node.id)
            if gate is None and before_node and not is_cancelled():
                gate = before_node(node, dict(state))
            if gate is not None:
                decision = interrupt(gate)
                if not isinstance(decision, dict) or consume_resume is None:
                    raise ValueError("恢复必须有服务端绑定的作者决定")
                consume_resume(gate, decision)
            return _execute_node(
                compiled,
                node,
                state,
                frozen_context,
                handlers,
                notify,
                is_cancelled,
                previous,
                frozen_identity,
            )

        return invoke

    for node_id in compiled.order:
        graph.add_node(node_id, RunnableLambda(wrap(by_id[node_id])), input_schema=_DriverState)
    parent_sets: dict[str, set[str]] = {node_id: set() for node_id in by_id}
    for edge in compiled.spec.data_edges:
        parent_sets[edge.target_node].add(edge.source_node)
    for control in compiled.spec.control_edges:
        parent_sets[control.target_node].add(control.source_node)
    parent_ids = {parent for parents in parent_sets.values() for parent in parents}
    for node_id, parents in parent_sets.items():
        if parents:
            graph.add_edge(sorted(parents), node_id)
        else:
            graph.add_edge(START, node_id)
        if node_id not in parent_ids:
            graph.add_edge(node_id, END)
    config: RunnableConfig = {
        "configurable": {"thread_id": checkpoint_thread},
        "max_concurrency": 1,
        "recursion_limit": max(32, len(by_id) + 4),
    }
    with SqliteSaver.from_conn_string(str(path)) as checkpointer:
        runnable = graph.compile(checkpointer=checkpointer)
        checkpoint = runnable.get_state(config)
        for pending_task in checkpoint.tasks:
            if pending_task.interrupts:
                value = pending_task.interrupts[0].value
                if isinstance(value, dict):
                    resumed_task_gates[pending_task.name] = value
        if checkpoint.created_at is None:
            notify(
                "graph.started",
                {
                    "graph_id": compiled.spec.id,
                    "graph_hash": compiled.graph_hash,
                    "checkpoint_thread": checkpoint_thread,
                },
            )
            runnable.invoke({"outcomes": {}, "artifacts": {}}, config, durability="sync")
        elif checkpoint.next:
            notify(
                "graph.resuming",
                {
                    "graph_id": compiled.spec.id,
                    "checkpoint_thread": checkpoint_thread,
                    "pending_nodes": list(checkpoint.next),
                },
            )
            if checkpoint.interrupts and resume is None:
                pass  # A read/restart cannot silently approve an interrupted node.
            else:
                resume_map = {
                    item.id: resume
                    for item in checkpoint.interrupts
                    if isinstance(item.value, dict)
                    and resume is not None
                    and item.value.get("gate_hash") == resume.get("gate_hash")
                }
                runnable.invoke(
                    Command(resume=resume_map) if resume is not None else None,
                    config,
                    durability="sync",
                )
        snapshot = runnable.get_state(config)
        waiting = bool(snapshot.interrupts)
        state = snapshot.values
    outcomes = {
        key: NodeOutcomeV2.model_validate(value) for key, value in state.get("outcomes", {}).items()
    }
    artifacts = {
        key: PortArtifact.model_validate(value) for key, value in state.get("artifacts", {}).items()
    }
    outputs = {}
    for name, ref in compiled.spec.outputs.items():
        outcome = outcomes.get(ref.node_id)
        if outcome is not None and outcome.status in {"succeeded", "reused"}:
            outputs[name] = copy.deepcopy(outcome.outputs[ref.port].value)
    status: Literal["completed", "completed_with_skips", "cancelled", "waiting_user"] = (
        "cancelled"
        if is_cancelled() or any(item.status == "cancelled" for item in outcomes.values())
        else "waiting_user"
        if waiting
        else "completed"
        if len(outcomes) == len(by_id)
        and all(item.status in {"succeeded", "reused"} for item in outcomes.values())
        else "completed_with_skips"
    )
    result = GraphExecutionResult(
        graph_id=compiled.spec.id,
        graph_version=compiled.spec.version,
        graph_hash=compiled.graph_hash,
        status=status,
        outcomes=outcomes,
        artifacts=artifacts,
        outputs=outputs,
    )
    notify(
        "graph.waiting_user" if waiting else "graph.completed",
        {
            "graph_id": compiled.spec.id,
            "graph_hash": compiled.graph_hash,
            "checkpoint_thread": checkpoint_thread,
            "status": result.status,
        },
    )
    return result
