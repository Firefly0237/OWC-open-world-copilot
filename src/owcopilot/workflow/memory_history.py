"""Deterministic, source-bound visible history and local token budgeting."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

from ..content.hash import content_hash

HISTORY_PROTOCOL = "actor-visible-history-v1"
TOKENIZER_SHA256 = "89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614"
DEFAULT_TOKENIZER_PATH = (
    Path(__file__).resolve().parents[3] / "var/cache/models/deepseek-v4-tokenizer/tokenizer.json"
)
_FEEDBACK_TYPES = {
    "graph.node.completed",
    "graph.node.failed",
    "graph.node.blocked",
    "graph.node.cancelled",
    "graph.node.reused",
    "requirements/checked",
    "tool/call",
    "tool/result",
    "tool/protocol_error",
    "v2_execution_failed",
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _token_count_cache(tokenizer):
    @lru_cache(maxsize=128)
    def count(text):
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    return count


class LocalMemoryTokenCounter:
    def __init__(self, path):
        path = Path(path)
        if not path.is_file():
            raise ValueError("经历tokenizer未配置；请先准备本地官方tokenizer资源")
        digest = sha256(path.read_bytes()).hexdigest()
        if digest != TOKENIZER_SHA256:
            raise ValueError("经历tokenizer资源hash与固定版本不符")
        try:
            from tokenizers import Tokenizer

            self._tokenizer = Tokenizer.from_file(str(path))
        except (ImportError, OSError, ValueError) as exc:
            raise ValueError("本地经历tokenizer无法加载，不使用字符或其他词表替代") from exc
        self._count = _token_count_cache(self._tokenizer)
        self.identity = {
            "kind": "local_tokenizer",
            "name": "deepseek-v4",
            "sha256": digest,
            "accounting_scope": "offline_estimate_not_provider_usage",
        }

    def count(self, text):
        # Only pure encoding is cached; source and authorization checks always run.
        return self._count(text)


@lru_cache(maxsize=4)
def _counter_at(path):
    return LocalMemoryTokenCounter(path)


def get_memory_token_counter():
    return _counter_at(
        str(
            Path(
                os.environ.get("OWCOPILOT_MEMORY_TOKENIZER_PATH", DEFAULT_TOKENIZER_PATH)
            ).resolve()
        )
    )


def counter_for_identity(identity, *, counter=None):
    counter = counter or get_memory_token_counter()
    if counter.identity != identity:
        raise ValueError("冻结经历的tokenizer身份已改变")
    return counter


def native_user_content(value):
    """Remove only an explicitly structured prior advisory, never native task fields."""
    if not isinstance(value, str):
        return value, None
    try:
        parsed = json.loads(value)
    except ValueError:
        return value, None
    if not isinstance(parsed, dict) or "past_experience" not in parsed:
        return value, None
    prior = parsed.pop("past_experience")
    return encoded(parsed), content_hash(prior)


def native_request_projection(request):
    projected = json.loads(encoded(request))
    removed = []
    if "user" in projected:
        projected["user"], digest = native_user_content(projected["user"])
        if digest is not None:
            removed.append({"field": "request.user.past_experience", "content_hash": digest})
    for index, message in enumerate(projected.get("messages", [])):
        if message.get("role") == "user":
            message["content"], digest = native_user_content(message.get("content"))
            if digest is not None:
                removed.append(
                    {
                        "field": f"request.messages[{index}].content.past_experience",
                        "content_hash": digest,
                    }
                )
    return projected, removed


def _message(role, content, run_id, node_id, **refs):
    if not isinstance(content, str):
        content = encoded(content)
    return {"role": role, "content": content, "source_run_id": run_id, "node_id": node_id, **refs}


def _event_feedback(event):
    payload = event["payload"]
    if event["type"].startswith("graph.node."):
        # Artifact bodies already appear in the actual response. Keep the real disposition.
        payload = {
            key: value
            for key, value in payload.items()
            if key not in {"outputs", "input_refs", "signature"}
        }
    return {"execution_feedback": event["type"], "observation": payload}


def normal_visible_feedback(events):
    return [event for event in events if event["type"] in _FEEDBACK_TYPES]


def project_visible_messages(source):
    """Rebuild visible messages from frozen native calls and normal execution feedback."""
    messages = []
    for episode in source.get("source_chain", []):
        native = episode["source"]
        run_id = episode["run_id"]
        positions = {row["request_id"]: row["seq"] for row in episode["request_order"]}
        timeline = [(positions[row["id"]], "request", row) for row in native["requests"]]
        timeline += [(row["seq"], "result", row) for row in native["results"]]
        timeline += [(row["seq"], "feedback", row) for row in episode["feedback_events"]]
        request_by_id = {row["id"]: row for row in native["requests"]}
        for _, kind, record in sorted(timeline, key=lambda item: item[0]):
            if kind == "request":
                request = record["request"]
                refs = {"request_id": record["id"]}
                node = record["node_id"]
                if request.get("api") == "complete":
                    if request.get("system"):
                        messages.append(_message("system", request["system"], run_id, node, **refs))
                    content, _ = native_user_content(request.get("user", ""))
                    messages.append(_message("user", content, run_id, node, **refs))
                elif request.get("api") == "complete_with_tools":
                    if request.get("tools"):
                        messages.append(
                            _message(
                                "system",
                                {"available_tools": request["tools"]},
                                run_id,
                                node,
                                **refs,
                            )
                        )
                    for message in request["messages"]:
                        content = message.get("content")
                        if message.get("role") == "user":
                            content, _ = native_user_content(content)
                        other = {
                            key: value
                            for key, value in message.items()
                            if key not in {"role", "content", "reasoning_content"}
                        }
                        content = {"content": content, **other} if other else content
                        messages.append(_message(message["role"], content, run_id, node, **refs))
                else:
                    raise ValueError("经历调用接口没有已实现的可见消息投影")
            elif kind == "result":
                result = record["payload"]
                request = request_by_id.get(result.get("request_id"))
                if request is None:
                    raise ValueError("经历回应缺少对应的真实请求")
                if "response" in result:
                    response = result["response"]
                    content = response.get("text", "")
                    if response.get("tool_calls"):
                        content = {"text": content, "tool_calls": response["tool_calls"]}
                    messages.append(
                        _message(
                            "assistant",
                            content,
                            run_id,
                            request["node_id"],
                            request_id=request["id"],
                        )
                    )
                else:
                    visible = {
                        key: value
                        for key, value in result.items()
                        if key not in {"measurement", "request_id"}
                    }
                    messages.append(
                        _message(
                            "user",
                            {"provider_observation": visible},
                            run_id,
                            request["node_id"],
                            request_id=request["id"],
                        )
                    )
            else:
                if record["type"] not in _FEEDBACK_TYPES:
                    raise ValueError("经历反馈不属于正常可见执行事件")
                messages.append(
                    _message(
                        "user",
                        _event_feedback(record),
                        run_id,
                        record.get("node_id"),
                        feedback_ref={
                            "seq": record["seq"],
                            "event_hash": record["event_hash"],
                            "type": record["type"],
                        },
                    )
                )
    return messages


def _member_source(value):
    return value[2] if isinstance(value, (tuple, list)) else value["source"]


def history_lineage(sources, messages, omitted):
    chains, requests, seen_runs = [], [], set()
    for source in sources:
        for episode in source["source_chain"]:
            if episode["run_id"] in seen_runs:
                continue
            seen_runs.add(episode["run_id"])
            chains.append(
                {
                    key: episode[key]
                    for key in ("run_id", "task_id", "revision", "source_hash", "predecessor")
                }
            )
            for record in episode["source"]["requests"]:
                native, removed = native_request_projection(record["request"])
                requests.append(
                    {
                        "source_run_id": record["run_id"],
                        "request_id": record["id"],
                        "request_hash": record["request_hash"],
                        "context_hash": record["context_hash"],
                        "prior_memory_route_hash": (record.get("context_manifest") or {}).get(
                            "memory_route_hash"
                        ),
                        "prior_memory_snapshot_hash": (record.get("context_manifest") or {}).get(
                            "memory_snapshot_hash"
                        ),
                        "native_request_hash": content_hash(native),
                        "removed_advisory_hash": next(
                            (
                                item["content_hash"]
                                for item in removed
                                if item["field"] == "request.user.past_experience"
                            ),
                            None,
                        ),
                        "removed_advisories": removed,
                    }
                )
    refs = [
        {
            key: message[key]
            for key in ("source_run_id", "node_id", "request_id", "feedback_ref")
            if key in message
        }
        | {"message_hash": content_hash(message)}
        for message in messages
    ]
    return {
        "projection_protocol": HISTORY_PROTOCOL,
        "source_chain": chains,
        "requests": requests,
        "available_message_refs": refs,
        "omitted_message_refs": refs[:omitted],
        "provided_message_refs": refs[omitted:],
    }


def build_history_payload(members, max_tokens=12000, counter=None):
    if type(max_tokens) is not int or not 1 <= max_tokens <= 128000:
        raise ValueError("经历token预算应为1至128000")
    counter = counter or get_memory_token_counter()
    sources = [_member_source(member) for member in members]
    messages, seen_runs = [], set()
    for source in sources:
        if source.get("source_schema_version") != 3:
            raise ValueError("新版经历不能混读未迁移的来源协议")
        if source.get("visible_messages") != project_visible_messages(source):
            raise ValueError("经历可见消息与实际来源不一致")
        # A repaired run can share predecessors with an already retained member.
        for episode in source["source_chain"]:
            if episode["run_id"] not in seen_runs:
                messages.extend(project_visible_messages({"source_chain": [episode]}))
                seen_runs.add(episode["run_id"])
    selected = list(messages)
    payload = {"backend": "trajectory", "view_protocol": HISTORY_PROTOCOL, "messages": selected}
    empty_tokens = counter.count(encoded({**payload, "messages": []}))
    if empty_tokens > max_tokens:
        raise ValueError("经历token预算不足以容纳固定消息封装")
    omitted = 0
    while counter.count(encoded(payload)) > max_tokens:
        selected.pop(0)
        omitted += 1
    budget = {
        "max_tokens": max_tokens,
        "token_count": counter.count(encoded(payload)),
        "counter_identity": counter.identity,
        "omitted_messages": omitted,
        "selection_rule": "oldest_complete_message_first",
        "accounting_scope": "serialized_advisory_not_total_provider_context",
    }
    return payload, history_lineage(sources, messages, omitted), budget
