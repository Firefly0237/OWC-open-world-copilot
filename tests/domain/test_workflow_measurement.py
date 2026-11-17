"""Observe production dispatch telemetry without generating extra remote calls."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from owcopilot.app.actions.workflow import create_task_action, open_workflow_store
from owcopilot.content.models import ContentBundle
from owcopilot.content.store import ContentStore
from owcopilot.llm.cache import ExactCache
from owcopilot.llm.execution_policy import SINGLE_DISPATCH_POLICY
from owcopilot.llm.telemetry import TelemetryCollector
from owcopilot.workflow.models import TaskInput
from owcopilot.workflow.runtime import TraceGateway, _run_step


def fixture(tmp_path):
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    task_id = create_task_action(root, "test", TaskInput(brief="Measurement input"))["task"]["id"]
    store = open_workflow_store(root)
    task = store.get_task(task_id)
    run, _ = store.create_run(task)
    store.update_run(run.id, status="running")
    return store, task, run


def dispatch(store, task, run, gateway, operation, text):
    return _run_step(
        store,
        run,
        task,
        operation,
        operation,
        operation,
        lambda error: (
            "test_transport",
            {"text": gateway.complete(task=operation, system="input", user=text)},
        ),
        lambda *_: None,
        kind="test_transport",
    )


def test_parallel_dispatch_usage_is_bound_to_its_own_call_and_survives_reopen(
    tmp_path, monkeypatch
):
    store, task, run = fixture(tmp_path)
    barrier = Barrier(2)

    class Provider:
        model = "requested-model"
        provider_kind = "openai_compatible"
        base_url = "https://user:secret@provider.example/api?key=secret"
        api_key = "DO_NOT_EXPORT"
        max_output_tokens = 6000

        def complete(self, *, system, user, model):
            barrier.wait(timeout=5)
            n = int(user)
            return user, n, n + 1, n // 2, "actual-response-model"

    monkeypatch.setenv("OWCOPILOT_PRICE_CHEAP", "1,2,3")
    telemetry = TelemetryCollector()
    gateway = TraceGateway(
        store,
        run.id,
        providers={"cheap": Provider()},
        telemetry=telemetry,
        execution_policy=SINGLE_DISPATCH_POLICY,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(dispatch, store, task, run, gateway, op, text)
            for op, text in (("draft_quest", "10"), ("draft_dialogue", "20"))
        ]
        assert all(f.result(timeout=10).status == "succeeded" for f in futures)
    reopened = type(store)(store.path)
    calls = reopened.model_calls(run.id)
    assert len(calls) == 2 and len(telemetry.records) == 2
    for call in calls:
        n = int(call["request"]["user"])
        m = call["measurement"]
        assert (m["input_tokens"], m["output_tokens"], m["cached_input_tokens"]) == (
            n,
            n + 1,
            n // 2,
        )
        assert m["requested_model"] == "requested-model"
        assert m["response_model"] == "actual-response-model"
        assert m["request_config"]["server_address"] == "provider.example"
        assert m["cost_estimate_usd"] > 0 and m["cost_basis"] == "configured_rates"
        assert m["policy"]["allow_failover"] is False
        assert "secret" not in str(m) and "DO_NOT_EXPORT" not in str(m)
        assert call["status"] == "done"


def test_local_initialization_failure_preserves_attempt_and_unknown_usage(tmp_path):
    store, task, run = fixture(tmp_path)

    def fail():
        raise RuntimeError("provider setup failed")

    gateway = TraceGateway(
        store, run.id, providers={}, provider_factory=fail, execution_policy=SINGLE_DISPATCH_POLICY
    )
    outcome = dispatch(store, task, run, gateway, "draft_quest", "input")
    assert outcome.status == "skipped_nonretryable"
    calls = store.model_calls(run.id)
    assert len(calls) == 1 and store.attempts(outcome.budget_id) == 1
    assert calls[0]["status"] == "failed"
    m = calls[0]["measurement"]
    assert m["capture_status"] == "missing" and m["usage_status"] == "unknown"
    assert m["input_tokens"] is None and m["cost_estimate_usd"] is None


def test_client_cache_is_distinct_from_provider_cache_and_unknown_prices(tmp_path, monkeypatch):
    store, task, run = fixture(tmp_path)

    class Provider:
        model = "model"
        calls = 0

        def complete(self, *, system, user, model):
            self.calls += 1
            return "answer", 20, 4, 12, "response"

    monkeypatch.delenv("OWCOPILOT_PRICE_CHEAP", raising=False)
    provider = Provider()
    gateway = TraceGateway(
        store,
        run.id,
        providers={"cheap": provider},
        cache=ExactCache(),
        execution_policy=SINGLE_DISPATCH_POLICY,
    )
    for operation in ("draft_quest", "draft_dialogue"):
        assert dispatch(store, task, run, gateway, operation, "same input").status == "succeeded"
    calls = store.model_calls(run.id)
    first, second = (call["measurement"] for call in calls)
    assert provider.calls == 1
    assert first["cached_input_tokens"] == 12 and first["client_cache_hit"] is False
    assert first["cost_estimate_usd"] is None and first["cost_basis"] == "unknown"
    assert second["client_cache_hit"] is True and second["input_tokens"] == 0
    assert second["cost_basis"] == "client_cache_no_provider_call"
    assert second["cost_estimate_usd"] == 0
