"""Transparent, bounded model/tool execution over the project workflow database.

Adapts the typed log and guarded tool pipeline described by DeepSeek Harness. Logs
reconstruct the local provider request, not undisclosed SDK defaults or model thought.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..content.hash import content_hash
from ..llm.execution_policy import SINGLE_DISPATCH_POLICY
from ..llm.gateway import ToolCallResponse
from .budget_policy import budget_snapshot, claim_dispatch, dispatch_failure_status, settle_dispatch
from .measurement import provider_settings
from .models import new_id, now
from .store import BudgetExhaustedError, RunInterruptedError, WorkflowStore


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _copy(value: Any) -> Any:
    return json.loads(_json(value))


class BudgetOwner(BaseModel):
    """Host-owned identity; node, graph, prompt and role versions are intentionally absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    project: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    product_key: str = Field(min_length=1)
    problem_key: str = Field(min_length=1)
    legacy_budget_id: str | None = None

    @property
    def identity(self) -> str:
        return content_hash(self.model_dump(exclude={"legacy_budget_id"}))


class HarnessStore:
    """Append-only trace and immutable problem binding, sharing the authoritative budget table."""

    def __init__(self, workflow: WorkflowStore):
        self.workflow = workflow
        with workflow.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS creative_harness_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS creative_harness_events_run
                    ON creative_harness_events(run_id,seq);
                CREATE TABLE IF NOT EXISTS creative_harness_owners(
                    owner_key TEXT PRIMARY KEY, budget_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS creative_harness_dispatches(
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, budget_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL, request TEXT NOT NULL,
                    UNIQUE(budget_id,attempt));
            """)

    @staticmethod
    def _event(
        db: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        parent_seq: int | None = None,
        node_id: str | None = None,
    ) -> dict[str, Any]:
        if not event_type or not isinstance(payload, dict):
            raise ValueError("事件类型与对象payload必需")
        if parent_seq is not None:
            parent = db.execute(
                "SELECT run_id FROM creative_harness_events WHERE seq=?", (parent_seq,)
            ).fetchone()
            if parent is None or parent["run_id"] != run_id:
                raise ValueError("父事件不属于本次运行")
        record = {
            "schema_version": 1,
            "run_id": run_id,
            "type": event_type,
            "parent_seq": parent_seq,
            "node_id": node_id,
            "created_at": now(),
            "payload": _copy(payload),
        }
        record["event_hash"] = content_hash(record)
        cursor = db.execute(
            "INSERT INTO creative_harness_events(run_id,event_type,payload) VALUES(?,?,?)",
            (run_id, event_type, _json(record)),
        )
        return {"seq": cursor.lastrowid, **record}

    def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        parent_seq: int | None = None,
        node_id: str | None = None,
    ) -> dict[str, Any]:
        self.workflow.get_run(run_id)
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._event(
                db, run_id, event_type, payload, parent_seq=parent_seq, node_id=node_id
            )

    def events(self, run_id: str, *, after_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        if after_seq < 0 or not 1 <= limit <= 1000:
            raise ValueError("事件游标或分页大小无效")
        self.workflow.get_run(run_id)
        with self.workflow.connection() as db:
            rows = db.execute(
                "SELECT seq,payload FROM creative_harness_events "
                "WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (run_id, after_seq, limit),
            ).fetchall()
        result = []
        for row in rows:
            record = json.loads(row["payload"])
            expected = record.pop("event_hash")
            if content_hash(record) != expected:
                raise ValueError("Harness事件hash不匹配")
            result.append({"seq": row["seq"], **record, "event_hash": expected})
        return result

    @staticmethod
    def _validate_run(db: sqlite3.Connection, owner: BudgetOwner, run_id: str) -> dict[str, Any]:
        run_row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        task_row = db.execute("SELECT payload FROM tasks WHERE id=?", (owner.task_id,)).fetchone()
        if run_row is None or task_row is None:
            raise KeyError("Harness任务或运行不存在")
        run, task = json.loads(run_row[0]), json.loads(task_row[0])
        if run["task_id"] != owner.task_id or task["project"] != owner.project:
            raise ValueError("Harness预算与项目／任务不一致")
        if (
            run.get("cancel_requested")
            or run.get("status") in {"cancelled", "interrupted"}
            or task["revision"] != run["revision"]
        ):
            raise RunInterruptedError("运行已取消或任务版本改变")
        return task

    def claim(
        self,
        owner: BudgetOwner,
        run_id: str,
        node_id: str,
        request: dict[str, Any],
        context_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        request = _copy(request)
        context_manifest = _copy(context_manifest)
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._validate_run(db, owner, run_id)
            legacy = owner.legacy_budget_id or task.get("preserved_budgets", {}).get(
                owner.product_key
            )
            existing = db.execute(
                "SELECT budget_id,payload FROM creative_harness_owners WHERE owner_key=?",
                (owner.identity,),
            ).fetchone()
            if existing is not None:
                bound = json.loads(existing["payload"])
                if legacy is not None and legacy != existing["budget_id"]:
                    raise ValueError("同一问题不能更换已绑定的预算")
                if bound["owner"] != owner.model_dump(exclude={"legacy_budget_id"}):
                    raise ValueError("预算身份内容不一致")
                budget_id = existing["budget_id"]
            else:
                budget_id = legacy or "harness:" + owner.identity
                if (
                    legacy is not None
                    and db.execute("SELECT id FROM node_budgets WHERE id=?", (legacy,)).fetchone()
                    is None
                ):
                    raise ValueError("声明的历史预算不存在，不得创建替代历史")
                if legacy is not None and not legacy.startswith(owner.task_id + ":"):
                    source = db.execute(
                        "SELECT 1 FROM model_calls c JOIN runs r ON r.id=c.run_id "
                        "WHERE c.budget_id=? AND r.task_id=? LIMIT 1",
                        (legacy, owner.task_id),
                    ).fetchone()
                    local = db.execute(
                        "SELECT payload FROM creative_harness_owners WHERE budget_id=?",
                        (legacy,),
                    ).fetchall()
                    if source is None and not any(
                        json.loads(row[0])["owner"]["task_id"] == owner.task_id for row in local
                    ):
                        raise ValueError("历史预算没有当前任务的归属证据")
                binding = {"owner": owner.model_dump(exclude={"legacy_budget_id"})}
                db.execute(
                    "INSERT INTO creative_harness_owners VALUES(?,?,?)",
                    (owner.identity, budget_id, _json(binding)),
                )
            receipt = claim_dispatch(
                db,
                budget_id,
                run_id,
                node_id,
                {"request": request, "context_manifest": context_manifest},
                owner_key=owner.identity,
            )
            record = {
                "id": new_id(),
                "run_id": run_id,
                "budget_id": budget_id,
                "attempt": int(receipt["attempt"]),
                "budget_receipt_id": receipt["id"],
                "budget_policy": receipt["policy"],
                "node_id": node_id,
                "owner": owner.model_dump(),
                "request": request,
                "request_hash": content_hash(request),
                "context_manifest": context_manifest,
                "context_hash": content_hash(context_manifest),
                "reconstruction_scope": "exact_local_provider_arguments_before_sdk",
            }
            db.execute(
                "INSERT INTO creative_harness_dispatches VALUES(?,?,?,?,?)",
                (record["id"], run_id, budget_id, record["attempt"], _json(record)),
            )
            event = self._event(db, run_id, "model/request", record, node_id=node_id)
            return {**record, "event_seq": event["seq"]}

    def budget_snapshot(self, owner: BudgetOwner) -> dict[str, Any]:
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            binding = db.execute(
                "SELECT budget_id FROM creative_harness_owners WHERE owner_key=?", (owner.identity,)
            ).fetchone()
            task_row = db.execute(
                "SELECT payload FROM tasks WHERE id=?", (owner.task_id,)
            ).fetchone()
            if task_row is None:
                raise KeyError("Harness任务不存在")
            task = json.loads(task_row[0])
            budget_id = (
                binding[0]
                if binding
                else (
                    owner.legacy_budget_id
                    or task.get("preserved_budgets", {}).get(owner.product_key)
                    or "harness:" + owner.identity
                )
            )
            return budget_snapshot(db, budget_id)

    def policy(self, owner: BudgetOwner) -> dict[str, Any]:
        return self.budget_snapshot(owner)["policy"]

    def finish(
        self,
        record: dict[str, Any],
        payload: dict[str, Any],
        node_id: str,
        *,
        settlement: str,
    ) -> None:
        from typing import cast

        from .budget_policy import AttemptStatus

        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._event(
                db,
                record["run_id"],
                "model/result",
                payload,
                parent_seq=record["event_seq"],
                node_id=node_id,
            )
            settle_dispatch(
                db,
                record["budget_id"],
                record["attempt"],
                cast(AttemptStatus, settlement),
                {
                    "request_id": record["id"],
                    "status": payload["status"],
                    "error": payload.get("error", ""),
                    "response_received": "response" in payload,
                },
            )

    def reconstruct_request(self, request_id: str) -> dict[str, Any]:
        with self.workflow.connection() as db:
            row = db.execute(
                "SELECT request FROM creative_harness_dispatches WHERE id=?", (request_id,)
            ).fetchone()
        if row is None:
            raise KeyError("Harness请求不存在")
        record = json.loads(row[0])
        if (
            content_hash(record["request"]) != record["request_hash"]
            or content_hash(record["context_manifest"]) != record["context_hash"]
        ):
            raise ValueError("Harness请求／上下文hash不匹配")
        return record

    def requests(self, run_id: str) -> list[dict[str, Any]]:
        self.workflow.get_run(run_id)
        with self.workflow.connection() as db:
            rows = db.execute(
                "SELECT id FROM creative_harness_dispatches WHERE run_id=? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        return [self.reconstruct_request(row[0]) for row in rows]


@dataclass(frozen=True)
class _Tool:
    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    execute: Callable[[dict[str, Any]], Any]
    roles: frozenset[str]
    description: str
    version: str


class ToolRegistry:
    """Trusted tool registration; denied calls never enter the tool body."""

    def __init__(self) -> None:
        self._tools: dict[str, _Tool] = {}

    def register(
        self,
        name: str,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        execute: Callable[[dict[str, Any]], Any],
        *,
        roles: set[str] | frozenset[str],
        description: str = "",
        version: str = "1",
    ) -> None:
        if not name or name in self._tools or not roles or not version:
            raise ValueError("工具注册需要唯一名称、版本及允许角色")
        self._tools[name] = _Tool(
            name, input_model, output_model, execute, frozenset(roles), description, version
        )

    def schemas(self, role_id: str) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": {
                        **tool.input_model.model_json_schema(),
                        "additionalProperties": False,
                    },
                },
            }
            for tool in self._tools.values()
            if role_id in tool.roles
        ]

    def versions(self, role_id: str) -> dict[str, str]:
        return {tool.name: tool.version for tool in self._tools.values() if role_id in tool.roles}

    def invoke(self, role_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None or role_id not in tool.roles:
            return {"is_error": True, "code": "tool_not_allowed", "message": "工具不在角色许可中"}
        try:
            unknown = set(arguments) - set(
                tool.input_model.model_json_schema().get("properties", {})
            )
            if unknown:
                raise ValueError("未声明的工具参数：" + ",".join(sorted(unknown)))
            validated = tool.input_model.model_validate(arguments, strict=True)
        except (ValueError, TypeError) as exc:
            return {"is_error": True, "code": "invalid_input", "message": str(exc)}
        try:
            value = tool.execute(validated.model_dump(mode="json"))
            if isinstance(value, BaseModel):
                value = value.model_dump(mode="json")
            if isinstance(value, dict) and set(value) - set(
                tool.output_model.model_json_schema().get("properties", {})
            ):
                raise ValueError("工具返回了未声明的输出字段")
            result = tool.output_model.model_validate(value, strict=True)
            return {"is_error": False, "value": _copy(result.model_dump(mode="json"))}
        except Exception as exc:
            return {"is_error": True, "code": "tool_failed", "message": str(exc)}


class Harness:
    """Provider protocol adapter with one durable, shared three-dispatch allowance."""

    def __init__(
        self,
        store: HarnessStore,
        run_id: str,
        provider: Any,
        *,
        cancelled: Callable[[], bool] | None = None,
    ):
        self.store, self.run_id, self.provider = store, run_id, provider
        self.cancelled = cancelled or (lambda: False)
        settings = provider_settings(provider)
        if settings.get("max_retries", 0) != 0:
            raise ValueError("Harness provider必须关闭SDK内层重试")

    def _check_current(self, owner: BudgetOwner) -> None:
        if self.cancelled():
            raise RunInterruptedError("调用已取消")
        with self.store.workflow.connection() as db:
            self.store._validate_run(db, owner, self.run_id)

    def _dispatch(
        self,
        owner: BudgetOwner,
        node_id: str,
        role_id: str,
        request: dict[str, Any],
        context_manifest: dict[str, Any],
        call: Callable[[], Any],
    ) -> Any:
        self._check_current(owner)
        request = {**request, "role_id": role_id, "config": provider_settings(self.provider)}
        record = self.store.claim(owner, self.run_id, node_id, request, context_manifest)
        started = time.perf_counter()
        response_received = False
        try:
            result = call()
            response_received = True
            self._check_current(owner)
            if isinstance(result, ToolCallResponse):
                response = {
                    "text": result.text,
                    "reasoning_content": result.reasoning_content,
                    "tool_calls": [
                        {"id": item.call_id, "name": item.name, "arguments": item.arguments}
                        for item in result.tool_calls
                    ],
                }
                usage = {
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "cached_input_tokens": result.cached_input_tokens,
                    "response_model": result.response_model or None,
                }
            else:
                text, input_tokens, output_tokens, cached_input_tokens, response_model = result
                response = {"text": text}
                usage = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "response_model": response_model or None,
                }
            reported = any(
                isinstance(value, int) and value > 0
                for value in (
                    usage["input_tokens"],
                    usage["output_tokens"],
                    usage["cached_input_tokens"],
                )
            )
            if not reported:
                for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
                    usage[key] = None
            self.store.finish(
                record,
                {
                    "request_id": record["id"],
                    "status": "done",
                    "response": response,
                    "measurement": {
                        **usage,
                        "usage_status": "provider_contract_reported" if reported else "unknown",
                        "cost_usd": None,
                        "cost_status": "unknown",
                        "duration_ms": (time.perf_counter() - started) * 1000,
                        "limits": "SDK defaults and absent provider usage are not reconstructed",
                    },
                },
                node_id,
                settlement="completed",
            )
            return result
        except Exception as exc:
            settlement = dispatch_failure_status(exc, response_received=response_received)
            self.store.finish(
                record,
                {
                    "request_id": record["id"],
                    "status": "discarded" if isinstance(exc, RunInterruptedError) else "failed",
                    "error": str(exc),
                    "outcome_known": settlement != "claimed_unknown",
                    "measurement": {
                        "input_tokens": None,
                        "output_tokens": None,
                        "response_model": None,
                        "usage_status": "unknown",
                        "cost_usd": None,
                        "duration_ms": (time.perf_counter() - started) * 1000,
                    },
                },
                node_id,
                settlement=settlement,
            )
            raise

    def complete(
        self,
        *,
        owner: BudgetOwner,
        node_id: str,
        role_id: str,
        system: str,
        user: str,
        context_manifest: dict[str, Any] | None = None,
    ) -> str:
        model = getattr(self.provider, "model", "cheap")
        request = {"api": "complete", "system": system, "user": user, "model": model}
        result = self._dispatch(
            owner,
            node_id,
            role_id,
            request,
            context_manifest or {},
            lambda: self.provider.complete(system=system, user=user, model=model),
        )
        return str(result[0])

    def run_tools(
        self,
        *,
        owner: BudgetOwner,
        node_id: str,
        role_id: str,
        messages: list[dict[str, Any]],
        registry: ToolRegistry,
        context_manifest: dict[str, Any] | None = None,
        max_dispatches: int | None = None,
    ) -> dict[str, Any]:
        """A tool continuation is another dispatch, never a nested fresh retry allowance."""
        if not getattr(self.provider, "supports_tools", False):
            raise ValueError("当前provider未声明原生工具支持，不使用隐式替代路径")
        history = _copy(messages)
        policy_limit = self.store.policy(owner)["tool_round_limit"]
        if max_dispatches is not None and (
            type(max_dispatches) is not int or not 1 <= max_dispatches <= policy_limit
        ):
            raise ValueError("原生工具本轮派发数必须在持久策略授权范围内")
        dispatch_limit = max_dispatches if max_dispatches is not None else policy_limit
        schemas = registry.schemas(role_id)
        if not schemas:
            raise ValueError("当前角色没有可调用工具")
        model = getattr(self.provider, "model", "cheap")
        for _ in range(dispatch_limit):
            request = {
                "api": "complete_with_tools",
                "messages": history,
                "tools": schemas,
                "model": model,
                "tool_versions": registry.versions(role_id),
            }
            try:
                result = self._dispatch(
                    owner,
                    node_id,
                    role_id,
                    request,
                    context_manifest or {},
                    lambda: self.provider.complete_with_tools(
                        messages=history, tools=schemas, model=model
                    ),
                )
            except BudgetExhaustedError:
                return {"status": "budget_exhausted", "text": "", "messages": history}
            if not isinstance(result, ToolCallResponse):
                raise ValueError("原生工具provider返回了错误合同")
            if not result.tool_calls:
                final_message = {"role": "assistant", "content": result.text or None}
                if result.reasoning_content is not None:
                    final_message["reasoning_content"] = result.reasoning_content
                history.append(final_message)
                return {"status": "completed", "text": result.text, "messages": history}
            ids = [item.call_id for item in result.tool_calls]
            if not all(ids) or len(set(ids)) != len(ids) or len(ids) > 8:
                self.store.append(
                    self.run_id,
                    "tool/protocol_error",
                    {"reason": "工具调用ID缺失／重复或单步超过8个调用"},
                    node_id=node_id,
                )
                return {"status": "protocol_error", "text": "", "messages": history}
            history.append(
                {
                    "role": "assistant",
                    "content": result.text or None,
                    **(
                        {"reasoning_content": result.reasoning_content}
                        if result.reasoning_content is not None
                        else {}
                    ),
                    "tool_calls": [
                        {
                            "id": item.call_id,
                            "type": "function",
                            "function": {
                                "name": item.name,
                                "arguments": _json(item.arguments),
                            },
                        }
                        for item in result.tool_calls
                    ],
                }
            )
            for tool_call in result.tool_calls:
                self._check_current(owner)
                event = self.store.append(
                    self.run_id,
                    "tool/call",
                    {
                        "call_id": tool_call.call_id,
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                        "role_id": role_id,
                        "call_kind": "model_native",
                    },
                    node_id=node_id,
                )
                outcome = registry.invoke(role_id, tool_call.name, tool_call.arguments)
                try:
                    self._check_current(owner)
                except RunInterruptedError:
                    self.store.append(
                        self.run_id,
                        "tool/result",
                        {
                            "call_id": tool_call.call_id,
                            "name": tool_call.name,
                            "is_error": True,
                            "code": "cancelled_after_execution",
                            "status": "discarded",
                            "observed_result": outcome,
                        },
                        parent_seq=event["seq"],
                        node_id=node_id,
                    )
                    raise
                self.store.append(
                    self.run_id,
                    "tool/result",
                    {"call_id": tool_call.call_id, "name": tool_call.name, **outcome},
                    parent_seq=event["seq"],
                    node_id=node_id,
                )
                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.call_id,
                        "content": _json(outcome),
                    }
                )
        return {
            "status": "budget_exhausted"
            if self.store.budget_snapshot(owner)["remaining"] == 0
            else "continuation_limit",
            "text": "",
            "messages": history,
            "policy_round_limit": policy_limit,
            "dispatch_limit": dispatch_limit,
        }


__all__ = ["BudgetOwner", "Harness", "HarnessStore", "ToolRegistry", "SINGLE_DISPATCH_POLICY"]
