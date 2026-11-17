"""LangGraph execution with durable budgets, trace evidence and partial-result continuation."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from ..config import DEFAULT_CHEAP_MODEL
from ..content.hash import content_hash
from ..content.models import Quest
from ..llm.execution_policy import SINGLE_DISPATCH_POLICY, LLMExecutionPolicy
from ..llm.gateway import LLMGateway, provider_error_info
from ..llm.resilience import build_real_provider
from ..llm.router import StaticRouter
from ..pipeline.project import ProjectContext
from .applicability import assess_applicability
from .budget_policy import BudgetLedger, dispatch_failure_status
from .catalog import OPERATION_CONTRACT_VERSIONS
from .measurement import bind_measurement_capture, call_measurement, collect_measurements
from .memory import accepted_workflow_context
from .models import Artifact, NodeOutcome, RunRecord, TaskRecord, WorkflowNode, WorkflowSpec
from .operators import OPERATION_KINDS, execute_operator
from .planning import interpret_task, plan_workflow, validate_bindings
from .store import BudgetExhaustedError, RunInterruptedError, WorkflowStore

_DISPATCH: ContextVar[tuple[str, str, int] | None] = ContextVar("studio_dispatch", default=None)
TASKS = (
    "studio_interpret",
    "studio_workflow_plan",
    "studio_plan_events",
    "studio_bind_dialogue",
    "studio_learn_method",
    "quest_draft",
    "dialogue_tree",
    "draft_quest_logic",
)


class TraceGateway(LLMGateway):
    def __init__(
        self,
        store: WorkflowStore,
        run_id: str,
        provider_factory: Callable[[], Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.store = store
        self.run_id = run_id
        self._provider_factory = provider_factory
        self._workflow_execution_policy: LLMExecutionPolicy | None = kwargs.get("execution_policy")
        self._measurement_owner = object()
        bind_measurement_capture(self.telemetry, self._measurement_owner)

    def complete(self, *, task: str, system: str, user: str, tier: str | None = None) -> str:
        dispatch = _DISPATCH.get()
        if dispatch is None:
            raise ValueError("模型派发必须归属于已领取预算的节点")
        budget_id, node_id, attempt = dispatch
        call_id = self.store.begin_model_call(
            self.run_id,
            budget_id,
            node_id,
            attempt,
            {"task": task, "system": system, "user": user},
        )
        started = time.perf_counter()
        with collect_measurements(self._measurement_owner) as records:
            try:
                if not self.providers and self._provider_factory is not None:
                    self.providers["cheap"] = self._provider_factory()
                result = super().complete(task=task, system=system, user=user, tier=tier)
            except Exception as exc:
                self.store.finish_model_call(
                    call_id,
                    error=_error_text(exc),
                    outcome_known=dispatch_failure_status(exc) != "claimed_unknown",
                    measurement=call_measurement(
                        records,
                        provider=self.providers.get(
                            records[0].tier if len(records) == 1 else tier or "cheap"
                        ),
                        policy=self._workflow_execution_policy,
                        duration_ms=(time.perf_counter() - started) * 1000,
                    ),
                )
                raise
            self.store.finish_model_call(
                call_id,
                response=result,
                measurement=call_measurement(
                    records,
                    provider=self.providers.get(
                        records[0].tier if len(records) == 1 else tier or "cheap"
                    ),
                    policy=self._workflow_execution_policy,
                    duration_ms=(time.perf_counter() - started) * 1000,
                ),
            )
        return result


def _error_text(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    if provider_error_info(exc).local_permission:
        text = "执行环境权限拒绝；先检查进程/联网权限，不按临时断网重复尝试。 " + text
    cause = exc.__cause__
    seen = {id(exc)}
    while cause is not None and id(cause) not in seen and len(seen) < 5:
        seen.add(id(cause))
        text += f" <- {type(cause).__name__}: {cause}"
        cause = cause.__cause__
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OWCOPILOT_API_KEY"):
        secret = os.getenv(key)
        if secret:
            text = text.replace(secret, "[redacted]")
    return text[:1600]


def _merge(left: dict, right: dict) -> dict:
    return {**left, **right}


class ExecutionState(TypedDict):
    outcomes: Annotated[dict[str, dict], _merge]


def _invalidated(task: TaskRecord, artifact: Artifact) -> bool:
    operation = next(
        (key for key, kind in OPERATION_KINDS.items() if kind == artifact.kind), artifact.node_id
    )
    for feedback in task.feedback:
        if feedback.get("revision", task.revision) <= artifact.revision:
            continue
        if artifact.node_id in {"_interpret", "_plan"}:
            if feedback.get("scope") == "workflow":
                return True
        elif operation in feedback.get("affected_operations", []):
            return True
    return False


def _run_step(
    store: WorkflowStore,
    run: RunRecord,
    task: TaskRecord,
    node_id: str,
    operation: str,
    input_hash: str,
    call: Callable[[str], tuple[str, dict]],
    emit: Callable[[str, dict], None],
    *,
    kind: str,
    preflight_error: Exception | None = None,
) -> NodeOutcome:
    budget_id = task.preserved_budgets.get(operation) or f"{task.id}:{task.revision}:{operation}"
    if run.budget_overrides and not run.budget_authorization_note:
        raise ValueError("额外预算必须有明确的授权记录")
    if run.budget_overrides.get(operation, budget_id) != budget_id:
        raise ValueError("不能换预算身份绕过旧账；请向原budget_id追加版本化授权")
    current = store.outcomes(run.id).get(node_id)
    if current and current.status == "succeeded" and preflight_error is None:
        return current
    for previous in reversed(store.artifacts(task.id)):
        if preflight_error is not None:
            break
        if previous.kind != kind or previous.revision > task.revision:
            continue
        if _invalidated(task, previous):
            continue
        if previous.input_hash == input_hash:
            outcome = NodeOutcome(
                node_id=node_id,
                operation=operation,
                status="succeeded",
                budget_id=budget_id,
                artifact_id=previous.id,
                attempts=store.attempts(budget_id),
            )
            store.complete_node(run.id, outcome)
            emit("node_reused", outcome.model_dump(mode="json"))
            return store.outcomes(run.id)[node_id]
    last_error = ""
    policy = BudgetLedger(store).snapshot(budget_id)["policy"]
    for _ in range(policy["step_attempt_limit"]):
        fresh = store.get_run(run.id)
        latest_task = store.get_task(task.id)
        if fresh.cancel_requested or latest_task.revision != task.revision:
            outcome = NodeOutcome(
                node_id=node_id,
                operation=operation,
                status="cancelled",
                budget_id=budget_id,
                attempts=store.attempts(budget_id),
                error="任务已取消或输入已改变",
            )
            store.complete_node(run.id, outcome)
            return outcome
        try:
            attempt = store.claim_attempt(budget_id, run.id, node_id, operation=operation)
        except RunInterruptedError as exc:
            outcome = NodeOutcome(
                node_id=node_id,
                operation=operation,
                status="cancelled",
                budget_id=budget_id,
                attempts=store.attempts(budget_id),
                error=str(exc),
            )
            store.complete_node(run.id, outcome)
            return outcome
        except BudgetExhaustedError as exc:
            last_error = str(exc)
            break
        running = NodeOutcome(
            node_id=node_id,
            operation=operation,
            status="running",
            attempts=attempt,
            budget_id=budget_id,
        )
        store.complete_node(run.id, running)
        emit("node_started", running.model_dump(mode="json"))
        token = _DISPATCH.set((budget_id, node_id, attempt))
        try:
            if preflight_error is not None:
                raise preflight_error
            output_kind, payload = call(last_error)
            artifact = Artifact(
                task_id=task.id,
                revision=task.revision,
                run_id=run.id,
                node_id=operation,
                kind=output_kind,
                payload=payload,
                input_hash=input_hash,
                content_hash=content_hash(payload),
            )
            outcome = running.model_copy(update={"status": "succeeded", "artifact_id": artifact.id})
            store.complete_node(run.id, outcome, artifact)
            if not any(
                row["node_id"] == node_id and row["attempt"] == attempt
                for row in store.model_calls(run.id)
            ):
                store.settle_attempt(
                    budget_id,
                    attempt,
                    status="completed",
                    evidence={"host_artifact_id": artifact.id, "provider_dispatched": False},
                )
            outcome = store.outcomes(run.id)[node_id]
            emit("node_outcome", outcome.model_dump(mode="json"))
            return outcome
        except Exception as exc:
            last_error = _error_text(exc)
            category = provider_error_info(exc).category
            dispatched = any(
                row["node_id"] == node_id and row["attempt"] == attempt
                for row in store.model_calls(run.id)
            )
            retry = (isinstance(exc, ValueError) and dispatched) or category in {
                "rate_limit",
                "conflict",
                "server_error",
            }
            if isinstance(exc, BudgetExhaustedError) or category in {
                "auth",
                "permission",
                "config",
                "bad_request",
                "output_budget",
                "deadline",
            }:
                retry = False
            if not dispatched:
                store.settle_attempt(
                    budget_id,
                    attempt,
                    status="completed_failed",
                    evidence={"host_error": last_error, "provider_dispatched": False},
                )
            if dispatched and any(
                item["status"] == "claimed_unknown"
                for item in BudgetLedger(store).dispatches(budget_id)
            ):
                retry = False
                last_error += "；派发结果未知，先对账，不自动重放"
            status = "retrying" if retry else "skipped_nonretryable"
            outcome = running.model_copy(update={"status": status, "error": last_error})
            store.complete_node(run.id, outcome)
            emit("node_failed", outcome.model_dump(mode="json"))
            # A diagnostic continuation is explicit and retains the owner; the
            # default policy never samples a failing node repeatedly in one call.
            if not retry or policy["step_attempt_limit"] == 1:
                return outcome
        finally:
            _DISPATCH.reset(token)
    outcome = NodeOutcome(
        node_id=node_id,
        operation=operation,
        status="skipped_retry_exhausted",
        budget_id=budget_id,
        attempts=store.attempts(budget_id),
        error=last_error or "本次节点执行已达到持久策略边界；后续沿原预算依据诊断继续",
    )
    store.complete_node(run.id, outcome)
    return outcome


def _with_error(task: TaskRecord, error: str) -> TaskRecord:
    if not error:
        return task
    return task.model_copy(
        update={
            "feedback": [
                *task.feedback,
                {
                    "text": "仅修正本次技术输出错误，不改变创作目标：" + error,
                    "scope": "content",
                    "technical": True,
                },
            ]
        }
    )


def _comparison_baseline_input(
    store: WorkflowStore,
    task: TaskRecord,
    current: Artifact,
    node: WorkflowNode,
) -> dict[str, Any]:
    """Resolve a baseline before the actual logic revision, even during a report-only refresh."""
    if current.task_id != task.id or current.kind != "quest_with_logic":
        raise ValueError("比较输入必须来自当前任务的任务逻辑产物")
    after = Quest.model_validate(current.payload["quest"])
    context = {"current_artifact_id": current.id, "current_revision": current.revision}
    explicit_id = node.parameters.get("baseline_quest_id")
    quests = task.base_bundle.get("quests", {})
    if explicit_id is not None and explicit_id != "":
        if not isinstance(explicit_id, str) or explicit_id not in quests:
            raise ValueError("显式比较基线任务不存在于冻结项目，不能自动换用其他基线")
        return {
            **context,
            "source": "project",
            "quest": quests[explicit_id],
            "quest_id": explicit_id,
            "version": task.base_version,
        }
    candidates = [
        (index, artifact)
        for index, artifact in enumerate(store.artifacts(task.id))
        if artifact.kind in {"quest_with_logic", "bound_dialogue"}
        and artifact.revision < current.revision
        and artifact.payload.get("quest", {}).get("id") == after.id
        and artifact.payload.get("quest", {}).get("logic") is not None
    ]
    if candidates:
        _, previous = max(candidates, key=lambda row: (row[1].revision, row[1].created_at, row[0]))
        return {
            **context,
            "source": "previous_candidate",
            "source_kind": previous.kind,
            "artifact_id": previous.id,
            "revision": previous.revision,
            "quest_id": after.id,
            "quest": previous.payload["quest"],
        }
    return {
        **context,
        "source": "project" if after.id in quests else "unavailable",
        "quest_id": after.id,
        "version": task.base_version,
        "quest": quests.get(after.id),
    }


def execute_workflow(
    store: WorkflowStore,
    run_id: str,
    content_root: str,
    emit: Callable[[str, dict], None] | None = None,
) -> dict:
    def notify(event: str, data: dict) -> None:
        if emit is not None:
            emit(event, data)

    run = store.get_run(run_id)
    task = store.get_task(run.task_id)
    if run.revision != task.revision:
        store.update_run(run_id, status="interrupted", error="输入版本已改变")
        return {"run_id": run_id, "status": "interrupted"}
    store.update_run(run_id, status="running")
    project: ProjectContext | None = None
    try:
        project = ProjectContext.open(
            content_root,
            world_id=task.request.world_id,
            version=task.base_version,
        )
        if content_hash(project.bundle) != task.base_hash:
            raise ValueError("项目基线已改变，请新建任务，旧产物仍可查看")
        model = task.request.model or os.getenv("OWCOPILOT_CHEAP_MODEL") or DEFAULT_CHEAP_MODEL
        gateway = TraceGateway(
            store,
            run_id,
            providers={},
            provider_factory=lambda: build_real_provider(
                model=model,
                timeout=120,
                max_output_tokens=6000,
                execution_policy=SINGLE_DISPATCH_POLICY,
            ),
            router=StaticRouter(mapping=dict.fromkeys(TASKS, "cheap")),
            execution_policy=SINGLE_DISPATCH_POLICY,
            namespace=f"studio:{task.project}:{task.base_hash}",
        )
        fingerprint = content_hash(
            {
                "request": task.request.model_dump(
                    mode="json", exclude={"comparison"}, exclude_none=True
                ),
                "base": task.base_hash,
            }
        )
        if run.workflow is None:
            selected = next(
                (
                    item
                    for item in store.workflows()
                    if item["id"] == task.request.workflow_id
                    and not item.get("disabled")
                    and (
                        item.get("scope", "project") != "project"
                        or item.get("project") == task.project
                    )
                ),
                None,
            )
            if task.request.workflow_id and not any(
                row.get("scope") == "workflow" for row in task.feedback
            ):
                if selected is None:
                    reasons = ["所选流程不可用或已停用"]
                else:
                    try:
                        assessment = assess_applicability(
                            WorkflowSpec.model_validate(selected["spec"]),
                            task,
                            reference_sources=project.reference_store.list_sources(),
                        )
                        reasons = assessment["reasons"]
                    except ValueError as exc:
                        reasons = [str(exc)]
                if reasons:
                    rejected = NodeOutcome(
                        node_id="_plan",
                        operation="_plan",
                        status="skipped_nonretryable",
                        budget_id=f"{task.id}:{task.revision}:_plan",
                        error="所选方法不适用于当前资源：" + "；".join(reasons),
                    )
                    store.complete_node(run.id, rejected)
                    store.update_run(run.id, status="completed_with_skips", error=rejected.error)
                    return {
                        "run_id": run.id,
                        "status": "completed_with_skips",
                        "error": rejected.error,
                    }
            interpretation = _run_step(
                store,
                run,
                task,
                "_interpret",
                "_interpret",
                fingerprint,
                lambda error: (
                    "interpretation",
                    interpret_task(
                        gateway,
                        _with_error(task, error),
                    ).model_dump(mode="json"),
                ),
                notify,
                kind="interpretation",
            )
            if interpretation.status != "succeeded":
                store.update_run(run_id, status="completed_with_skips", error=interpretation.error)
                return {"run_id": run_id, "status": "completed_with_skips"}
            interpretation_id = interpretation.artifact_id
            if interpretation_id is None:
                raise ValueError("成功的意图解释缺少产物引用")
            recalled = accepted_workflow_context(
                store,
                project=task.project,
                task=task,
                reference_sources=project.reference_store.list_sources(),
            )

            def make_plan(error: str) -> tuple[str, dict]:
                workflow_feedback = any(item.get("scope") == "workflow" for item in task.feedback)
                if task.request.workflow_id and not workflow_feedback:
                    if selected is None:
                        raise ValueError("所选流程不可用或已停用")
                    spec = validate_bindings(WorkflowSpec.model_validate(selected["spec"]))
                elif task.feedback and task.feedback[-1].get("scope") != "workflow":
                    old = next(
                        (
                            item
                            for item in store.list_runs(task.id)
                            if item.revision < task.revision and item.workflow is not None
                        ),
                        None,
                    )
                    if old is None:
                        spec = plan_workflow(
                            gateway,
                            _with_error(task, error),
                            store.get_artifact(interpretation_id).payload,
                            recalled,
                        )
                    else:
                        assert old.workflow is not None
                        spec = old.workflow
                else:
                    spec = plan_workflow(
                        gateway,
                        _with_error(task, error),
                        store.get_artifact(interpretation_id).payload,
                        recalled,
                    )
                return "workflow_spec", spec.model_dump(mode="json")

            planned = _run_step(
                store,
                run,
                task,
                "_plan",
                "_plan",
                fingerprint,
                make_plan,
                notify,
                kind="workflow_spec",
            )
            if planned.status != "succeeded":
                store.update_run(run_id, status="completed_with_skips", error=planned.error)
                return {"run_id": run_id, "status": "completed_with_skips"}
            if planned.artifact_id is None:
                raise ValueError("成功的工作图规划缺少产物引用")
            spec = validate_bindings(
                WorkflowSpec.model_validate(
                    store.get_artifact(planned.artifact_id).payload,
                )
            )
            run = store.update_run(
                run_id,
                workflow=spec.model_dump(mode="json"),
                workflow_source=(
                    "selected" if selected else "adapted" if spec.method_source_ids else "new"
                ),
            )
        current_spec = run.workflow
        if current_spec is None:
            raise ValueError("工作图未生成")
        spec = validate_bindings(current_spec)
        if task.request.participation == "direction" and not run.direction_confirmed:
            store.update_run(
                run_id,
                status="waiting_user",
                waiting_question="创作方向和步骤已生成，可调整或继续",
            )
            return {"run_id": run_id, "status": "waiting_user"}

        graph = StateGraph(ExecutionState)
        by_id = {node.id: node for node in spec.nodes}

        def wrap(node: WorkflowNode) -> Callable:
            def execute(state: ExecutionState) -> dict:
                required = [state["outcomes"].get(parent, {}) for parent in node.depends_on]
                if any(item.get("status") != "succeeded" for item in required):
                    outcome = NodeOutcome(
                        node_id=node.id,
                        operation=node.operation,
                        status="blocked_dependency",
                        budget_id=f"{task.id}:{task.revision}:{node.operation}",
                        error="必需上游产物未完成；其他独立节点继续",
                    )
                    store.complete_node(run_id, outcome)
                    notify("node_outcome", outcome.model_dump(mode="json"))
                else:
                    inputs: dict[str, Any] = {}
                    preflight_error: Exception | None = None
                    for parent in node.depends_on + node.optional_dependencies:
                        result = state["outcomes"].get(parent, {})
                        if result.get("artifact_id"):
                            inputs[by_id[parent].operation] = store.get_artifact(
                                result["artifact_id"],
                            ).payload
                    if node.operation == "compare":
                        if task.request.comparison is not None:
                            inputs["comparison_conditions"] = task.request.comparison.model_dump(
                                mode="json"
                            )
                        logic_parent = next(
                            parent
                            for parent in node.depends_on
                            if by_id[parent].operation == "draft_logic"
                        )
                        current_logic = store.get_artifact(
                            state["outcomes"][logic_parent]["artifact_id"]
                        )
                        # Keep the full baseline under a normal input key so the existing
                        # signature includes its payload/provenance and cannot reuse a self-diff.
                        try:
                            inputs["comparison_baseline"] = _comparison_baseline_input(
                                store, task, current_logic, node
                            )
                        except Exception as exc:
                            preflight_error = exc
                            inputs["comparison_baseline"] = {
                                "source": "invalid",
                                "error": _error_text(exc),
                            }
                    inputs["_outcomes"] = state["outcomes"]
                    inputs["_method_slots"] = [
                        slot.model_dump(mode="json") for slot in spec.input_slots
                    ]
                    signature_data: dict[str, Any] = {
                        "base": task.base_hash,
                        "operation": node.operation,
                        "parameters": node.parameters,
                        "inputs": {
                            key: value for key, value in inputs.items() if not key.startswith("_")
                        },
                    }
                    if node.guidance or spec.input_slots:
                        signature_data.update(
                            guidance=node.guidance,
                            method_slots=inputs["_method_slots"],
                        )
                    if node.operation in OPERATION_CONTRACT_VERSIONS:
                        signature_data["operation_contract_version"] = OPERATION_CONTRACT_VERSIONS[
                            node.operation
                        ]
                    signature = content_hash(signature_data)
                    outcome = _run_step(
                        store,
                        run,
                        task,
                        node.id,
                        node.operation,
                        signature,
                        lambda error: execute_operator(
                            node,
                            task=_with_error(task, error),
                            project=project,
                            gateway=gateway,
                            inputs=inputs,
                        ),
                        notify,
                        kind=OPERATION_KINDS[node.operation],
                        preflight_error=preflight_error,
                    )
                return {"outcomes": {node.id: outcome.model_dump(mode="json")}}

            return execute

        children = set()
        for node in spec.nodes:
            graph.add_node(node.id, wrap(node))
        for node in spec.nodes:
            parents = node.depends_on + node.optional_dependencies
            if parents:
                graph.add_edge(parents, node.id)
                children.update(parents)
            else:
                graph.add_edge(START, node.id)
        for node in spec.nodes:
            if node.id not in children:
                graph.add_edge(node.id, END)
        checkpoint_path = store.path.with_name("creative_graph_checkpoints.sqlite3")
        config: RunnableConfig = {
            "configurable": {"thread_id": run.id},
            "max_concurrency": 1,
        }
        with SqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
            compiled = graph.compile(checkpointer=checkpointer)
            checkpoint = compiled.get_state(config)
            if checkpoint.created_at is None:
                compiled.invoke({"outcomes": {}}, config, durability="sync")
            elif checkpoint.next:
                compiled.invoke(None, config, durability="sync")
            # A finished checkpoint needs only the business-ledger finalization below.
            # Supplying another input would restart from START instead of resuming.
        outcomes = store.outcomes(run_id)
        fresh = store.get_run(run_id)
        status = (
            "cancelled"
            if fresh.cancel_requested
            else (
                "completed"
                if all(item.status == "succeeded" for item in outcomes.values())
                else "completed_with_skips"
            )
        )
        store.update_run(run_id, status=status)
        return {
            "run_id": run_id,
            "status": status,
            "outcomes": [item.model_dump(mode="json") for item in outcomes.values()],
        }
    except Exception as exc:
        store.update_run(run_id, status="failed_setup", error=_error_text(exc))
        return {"run_id": run_id, "status": "failed_setup", "error": _error_text(exc)}
    finally:
        if project is not None:
            project.close()
