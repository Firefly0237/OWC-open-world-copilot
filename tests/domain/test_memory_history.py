"""Controlled projection and budgeting checks, without model or author evaluations."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.workflow.memory_history import (
    LocalMemoryTokenCounter,
    _token_count_cache,
    build_history_payload,
    counter_for_identity,
    encoded,
    normal_visible_feedback,
    project_visible_messages,
)


class ControlledCounter:
    identity = {"kind": "controlled_test_units", "version": 1}

    def count(self, text):
        return len(text)


def source_with_feedback(*, past=None, response="broken JSON", failure=True):
    context = {
        "inputs": {"task": {"brief": "Keep the ending", "protected": ["ending"]}},
        "guidance": "Use the explicit method",
        "output_schema": {"type": "object"},
        "requirements": {"decision_option_count": 2},
    }
    if past is not None:
        context["past_experience"] = past
    request = {
        "api": "complete",
        "system": "Follow the typed output contract",
        "user": json.dumps(context),
        "config": {"audit_only": "x" * 5000},
    }
    record = {
        "id": "call",
        "run_id": "run",
        "node_id": "plan",
        "request": request,
        "request_hash": content_hash(request),
        "context_hash": "context-hash",
        "context_manifest": {"audit_only": "x" * 5000},
    }
    result = {
        "seq": 2,
        "type": "model/result",
        "run_id": "run",
        "node_id": "plan",
        "payload": {"request_id": "call", "status": "done", "response": {"text": response}},
    }
    feedback = {
        "seq": 3,
        "type": "graph.node.failed" if failure else "graph.node.completed",
        "run_id": "run",
        "node_id": "plan",
        "payload": {
            "status": "failed" if failure else "succeeded",
            "error": "expected object",
            "validation_errors": [{"type": "model_type"}],
        },
    }
    feedback["event_hash"] = content_hash(feedback)
    native = {"run_id": "run", "requests": [record], "results": [result]}
    source = {
        "source_schema_version": 3,
        "source_chain": [
            {
                "run_id": "run",
                "task_id": "task",
                "revision": 1,
                "source_hash": content_hash(native),
                "source": native,
                "request_order": [{"request_id": "call", "seq": 1, "event_hash": "request-event"}],
                "feedback_events": [feedback],
                "predecessor": None,
            }
        ],
    }
    source["visible_messages"] = project_visible_messages(source)
    return source


def test_projection_keeps_native_constraints_and_real_failure_without_audit_packaging():
    source = source_with_feedback(past={"old": "unrelated history"})
    original = deepcopy(source)
    messages = source["visible_messages"]
    user = json.loads(next(message["content"] for message in messages if message["role"] == "user"))
    assert set(user) == {"inputs", "guidance", "output_schema", "requirements"}
    assert user["inputs"]["task"]["protected"] == ["ending"]
    assert user["guidance"] == "Use the explicit method"
    assert messages[-2]["content"] == "broken JSON"
    failure = json.loads(messages[-1]["content"])
    assert failure["execution_feedback"] == "graph.node.failed"
    assert failure["observation"]["validation_errors"] == [{"type": "model_type"}]
    assert "audit_only" not in encoded(messages)
    assert "past_experience" not in encoded(messages)
    assert source == original


def test_token_fifo_drops_oldest_messages_and_never_backfills_older_after_large_latest():
    source = source_with_feedback(response="new native response " * 100)
    counter = ControlledCounter()
    full, _, _ = build_history_payload([{"source": source}], 12000, counter)
    target = {**full, "messages": full["messages"][-2:]}
    budget = counter.count(encoded(target))
    payload, lineage, record = build_history_payload([{"source": source}], budget, counter)
    assert payload["messages"] == full["messages"][-2:]
    assert record["token_count"] <= budget
    assert record["omitted_messages"] == 2
    assert len(lineage["omitted_message_refs"]) == 2
    # A huge latest message consumes the suffix; no earlier small message gets refilled.
    huge = source_with_feedback(response="large", failure=False)
    huge["source_chain"][0]["feedback_events"][0]["payload"]["error"] = "x" * 10000
    huge["visible_messages"] = project_visible_messages(huge)
    empty, _, meta = build_history_payload([{"source": huge}], 400, counter)
    assert empty["messages"] == []
    assert meta["omitted_messages"] == len(huge["visible_messages"])


def test_two_generation_projection_is_compact_and_original_calls_remain_reconstructible():
    first = source_with_feedback(response="first visible result")
    first_view, _, _ = build_history_payload([{"source": first}], 12000, ControlledCounter())
    second = source_with_feedback(past=first_view, response="second visible result")
    before = deepcopy(second["source_chain"])
    payload, lineage, _ = build_history_payload([{"source": second}], 12000, ControlledCounter())
    assert "second visible result" in encoded(payload)
    assert "first visible result" not in encoded(payload)
    pointer = lineage["requests"][0]
    record = second["source_chain"][0]["source"]["requests"][0]
    assert pointer["request_hash"] == content_hash(record["request"])
    assert pointer["removed_advisory_hash"] == content_hash(first_view)
    assert second["source_chain"] == before
    forged = deepcopy(second)
    forged["visible_messages"][0]["content"] = "invented"
    with pytest.raises(ValueError, match="实际来源"):
        build_history_payload([{"source": forged}], 12000, ControlledCounter())


def test_missing_or_changed_tokenizer_never_substitutes_another_counter(tmp_path):
    with pytest.raises(ValueError, match="未配置"):
        LocalMemoryTokenCounter(tmp_path / "missing.json")
    changed = tmp_path / "changed.json"
    changed.write_text("{}")
    with pytest.raises(ValueError, match="hash"):
        LocalMemoryTokenCounter(changed)
    with pytest.raises(ValueError, match="身份"):
        counter_for_identity({"kind": "different"}, counter=ControlledCounter())


def test_normal_feedback_excludes_hidden_reward_events():
    feedback = source_with_feedback()["source_chain"][0]["feedback_events"][0]
    hidden = {"type": "hidden/reward", "payload": {"score": 1}}
    assert normal_visible_feedback([feedback, hidden]) == [feedback]


def test_loaded_counter_caches_only_exact_text_per_instance_with_bounded_capacity():
    class RecordingTokenizer:
        def __init__(self):
            self.calls = []

        def encode(self, text, *, add_special_tokens):
            assert add_special_tokens is False
            self.calls.append(text)
            return SimpleNamespace(ids=list(range(len(text))))

    # Explicit unit-only transport avoids requiring model files in the test fixture.
    first = object.__new__(LocalMemoryTokenCounter)
    first._tokenizer = RecordingTokenizer()
    first._count = _token_count_cache(first._tokenizer)
    second = object.__new__(LocalMemoryTokenCounter)
    second._tokenizer = RecordingTokenizer()
    second._count = _token_count_cache(second._tokenizer)
    try:
        assert first.count("identical") == first.count("identical") == 9
        assert first._tokenizer.calls == ["identical"]
        assert first.count("different") == 9
        assert first._tokenizer.calls == ["identical", "different"]
        assert second.count("identical") == 9
        assert second._tokenizer.calls == ["identical"]
        for index in range(129):
            first.count(f"bounded-{index}")
        assert first._count.cache_info().currsize == 128
        before = len(first._tokenizer.calls)
        first.count("identical")
        assert len(first._tokenizer.calls) == before + 1
    finally:
        first._count.cache_clear()
