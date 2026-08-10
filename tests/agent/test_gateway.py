import pytest

from owcopilot.llm.gateway import (
    LLMGateway,
    LLMGatewayError,
    MockProvider,
    provider_error_info,
    should_count_breaker_failure,
    should_failover,
    should_gateway_retry,
)
from owcopilot.llm.router import StaticRouter
from owcopilot.llm.telemetry import PRICES, TelemetryCollector


def test_routing_and_telemetry():
    tel = TelemetryCollector()
    gw = LLMGateway(
        providers={"cheap": MockProvider(), "frontier": MockProvider()},
        router=StaticRouter(),
        telemetry=tel,
    )

    gw.complete(task="plan", system="s", user="u")  # -> cheap
    gw.complete(task="generate", system="s", user="u")  # -> frontier

    assert tel.records[0].tier == "cheap"
    assert tel.records[1].tier == "frontier"
    assert tel.total_input_tokens > 0
    assert tel.cache_hit_rate == 0.0  # NoOpCache never hits in P0
    assert tel.total_cost > 0  # frontier tier has non-zero price


class _CacheReportingProvider:
    """Real-provider stand-in that returns the optional 4th value: the server-side
    prompt_cache_hit_tokens (a subset of the input tokens)."""

    def complete(self, *, system, user, model):
        return "ok", 1000, 50, 400  # 400 of the 1000 input tokens were cache hits


def test_gateway_passes_through_provider_cache_hit_tokens():
    tel = TelemetryCollector()
    gw = LLMGateway(providers={"cheap": _CacheReportingProvider()}, telemetry=tel)
    gw.complete(task="generate", system="s", user="u", tier="cheap")

    rec = tel.records[0]
    assert rec.input_tokens == 1000
    assert rec.cached_input_tokens == 400  # the 4-tuple's hit tokens were recorded
    hit_p, miss_p, out_p = PRICES["cheap"]
    assert rec.cost_usd == (400 * hit_p + 600 * miss_p + 50 * out_p) / 1_000_000


def test_gateway_tolerates_three_tuple_provider():
    # Providers that don't report cache tokens (the offline fakes) still work; cached == 0.
    tel = TelemetryCollector()
    gw = LLMGateway(providers={"cheap": MockProvider()}, telemetry=tel)
    gw.complete(task="plan", system="s", user="u", tier="cheap")
    assert tel.records[0].cached_input_tokens == 0


def test_gateway_does_not_cache_empty_response():
    # A transient empty completion must not be memoized, so a retry is a genuine fresh call.
    from owcopilot.llm.cache import ExactCache

    class _EmptyThenX:
        def __init__(self):
            self.calls = 0

        def complete(self, *, system, user, model):
            self.calls += 1
            return ("" if self.calls == 1 else "x"), 1, 1

    prov = _EmptyThenX()
    gw = LLMGateway(providers={"cheap": prov}, cache=ExactCache())
    a = gw.complete(task="generate", system="s", user="u", tier="cheap")
    b = gw.complete(task="generate", system="s", user="u", tier="cheap")
    assert a == "" and b == "x"  # empty not cached -> second call re-invokes provider
    assert prov.calls == 2


def test_gateway_retries_transient_provider_error():
    class _Flaky:
        def __init__(self):
            self.calls = 0

        def complete(self, *, system, user, model):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("timed out")
            return "ok", 1, 1

    prov = _Flaky()
    gw = LLMGateway(providers={"cheap": prov}, max_retries=1)
    assert gw.complete(task="plan", system="s", user="u", tier="cheap") == "ok"
    assert prov.calls == 2


def test_gateway_retries_server_availability_error():
    class _ServerUnavailable(Exception):
        status_code = 503

    class _Flaky:
        def __init__(self):
            self.calls = 0

        def complete(self, *, system, user, model):
            self.calls += 1
            if self.calls == 1:
                raise _ServerUnavailable("service unavailable")
            return "ok", 1, 1

    prov = _Flaky()
    gw = LLMGateway(providers={"cheap": prov}, max_retries=1)
    assert gw.complete(task="plan", system="s", user="u", tier="cheap") == "ok"
    assert prov.calls == 2
    record = gw.telemetry.records[-1]
    assert record.gateway_invocations == 2
    assert record.transport_attempt_cap == 2


@pytest.mark.parametrize(
    ("status_code", "category"),
    [(401, "auth"), (403, "permission"), (400, "bad_request")],
)
def test_gateway_does_not_retry_deterministic_client_errors(status_code, category):
    class _ClientError(Exception):
        def __init__(self):
            self.status_code = status_code
            super().__init__(f"HTTP {status_code}")

    class _AlwaysFails:
        def __init__(self):
            self.calls = 0

        def complete(self, *, system, user, model):
            self.calls += 1
            raise _ClientError()

    prov = _AlwaysFails()
    gw = LLMGateway(providers={"cheap": prov}, max_retries=4)
    with pytest.raises(LLMGatewayError) as ei:
        gw.complete(task="plan", system="s", user="u", tier="cheap")

    assert ei.value.category == category
    assert ei.value.attempts == 1
    assert prov.calls == 1


def test_gateway_does_not_retry_unknown_application_error():
    class _AlwaysFails:
        def __init__(self):
            self.calls = 0

        def complete(self, *, system, user, model):
            self.calls += 1
            raise ValueError("invalid local payload")

    prov = _AlwaysFails()
    gw = LLMGateway(providers={"cheap": prov}, max_retries=4)
    with pytest.raises(LLMGatewayError) as ei:
        gw.complete(task="plan", system="s", user="u", tier="cheap")

    assert ei.value.category == "provider_error"
    assert ei.value.attempts == 1
    assert prov.calls == 1


def test_gateway_never_stacks_retries_outside_sdk_owned_provider():
    class _SdkOwnedRetries:
        owns_retry_policy = True
        suppress_gateway_replay = True
        handles_transport_retries = True
        max_transport_attempts = 3

        def __init__(self):
            self.calls = 0

        def complete(self, *, system, user, model):
            self.calls += 1
            raise TimeoutError("SDK retry budget exhausted")

    prov = _SdkOwnedRetries()
    gw = LLMGateway(providers={"cheap": prov}, max_retries=4)
    with pytest.raises(LLMGatewayError) as ei:
        gw.complete(task="plan", system="s", user="u", tier="cheap")

    # One gateway invocation; the provider's declared SDK ceiling is three HTTP attempts. Before
    # retry ownership this configuration could multiply into 5 x 3 transport attempts.
    assert ei.value.gateway_invocations == 1
    assert ei.value.attempts == 1  # backward-compatible alias, explicitly Gateway-level
    assert ei.value.transport_attempt_cap == 3
    assert "gateway_invocations=1" in str(ei.value)
    assert "transport_attempt_cap=3" in str(ei.value)
    assert prov.calls == 1
    assert prov.max_transport_attempts == 3


def test_gateway_classifies_provider_error_after_retries_exhausted():
    class _AlwaysTimeout:
        def complete(self, *, system, user, model):
            raise TimeoutError("timed out")

    gw = LLMGateway(providers={"cheap": _AlwaysTimeout()}, max_retries=1)
    try:
        gw.complete(task="plan", system="s", user="u", tier="cheap")
    except LLMGatewayError as e:
        assert e.category == "timeout"
        assert e.gateway_invocations == 2
        assert e.transport_attempt_cap == 2
    else:
        raise AssertionError("expected LLMGatewayError")


def test_gateway_output_budget_fails_fast_without_retry():
    """Retrying a capped reply pays for another capped reply — one attempt, guided category."""
    from owcopilot.llm.gateway import OutputTruncatedError

    class _AlwaysCapped:
        def __init__(self):
            self.calls = 0

        def complete(self, *, system, user, model):
            self.calls += 1
            raise OutputTruncatedError(completion_tokens=3000, cap=3000)

    prov = _AlwaysCapped()
    gw = LLMGateway(providers={"cheap": prov}, max_retries=2)
    try:
        gw.complete(task="plan", system="s", user="u", tier="cheap")
    except LLMGatewayError as e:
        assert e.category == "output_budget"
        assert e.attempts == 1  # fail fast: no retries were burned
        assert "OWCOPILOT_MAX_OUTPUT_TOKENS" in str(e.cause)
    else:
        raise AssertionError("expected LLMGatewayError")
    assert prov.calls == 1


class _Response:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.headers = headers or {}


class _HttpError(Exception):
    def __init__(
        self, status_code: int, message: str, headers: dict[str, str] | None = None
    ) -> None:
        self.status_code = status_code
        self.response = _Response(status_code, headers)
        super().__init__(message)


class BadRequestError(Exception):
    pass


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (
            _HttpError(400, "maximum context 4096, got 4290 tokens; timeout=500"),
            "bad_request",
        ),
        (BadRequestError("timeout parameter must be positive"), "bad_request"),
        (ValueError("maximum context 4096, got 4290 tokens; timeout=500"), "provider_error"),
        (ValueError("batch size 500"), "provider_error"),
    ],
)
def test_classifier_does_not_infer_status_or_timeout_from_payload_numbers(exc, category):
    info = provider_error_info(exc)
    assert info.category == category
    assert not should_gateway_retry(exc)
    assert not should_failover(exc)
    assert not should_count_breaker_failure(exc)


def test_classifier_accepts_explicit_http_code_message_without_matching_larger_numbers() -> None:
    assert provider_error_info(RuntimeError("HTTP 429")).category == "rate_limit"
    assert provider_error_info(RuntimeError("token limit 4290")).category == "provider_error"


def test_explicit_category_precedes_type_and_message_fallbacks() -> None:
    class _CategorizedTimeout(TimeoutError):
        category = "bad_request"

    exc = _CategorizedTimeout("request timed out")
    assert provider_error_info(exc).category == "bad_request"
    assert not should_gateway_retry(exc)


def test_conflict_retries_same_provider_but_does_not_failover_or_trip_breaker() -> None:
    exc = _HttpError(409, "conflict")
    info = provider_error_info(exc)
    assert info.category == "conflict"
    assert should_gateway_retry(exc)
    assert not should_failover(exc)
    assert not should_count_breaker_failure(exc)


def test_rate_limit_does_not_pollute_global_breaker_health() -> None:
    exc = _HttpError(429, "too many requests")
    assert provider_error_info(exc).category == "rate_limit"
    assert should_gateway_retry(exc)
    assert should_failover(exc)
    assert not should_count_breaker_failure(exc)


@pytest.mark.parametrize("status_code", [409, 429, 503])
def test_x_should_retry_false_overrides_transient_status(status_code) -> None:
    exc = _HttpError(status_code, "provider rejected request", {"x-should-retry": "false"})
    assert provider_error_info(exc).retry_hint is False
    assert not should_gateway_retry(exc)
    assert not should_failover(exc)
    assert not should_count_breaker_failure(exc)


def test_negative_retry_hint_wins_across_attribute_and_response_headers() -> None:
    exc = _HttpError(429, "rate limited", {"x-should-retry": "false"})
    exc.should_retry = True

    assert provider_error_info(exc).retry_hint is False
    assert not should_gateway_retry(exc)
    assert not should_failover(exc)


def test_x_should_retry_true_enables_unknown_request_replay_but_not_breaker() -> None:
    exc = _HttpError(200, "opaque provider error", {"X-Should-Retry": "true"})
    assert provider_error_info(exc).category == "provider_error"
    assert should_gateway_retry(exc)
    assert should_failover(exc)
    assert not should_count_breaker_failure(exc)


def test_output_truncation_never_replays_even_with_positive_hint() -> None:
    from owcopilot.llm.gateway import OutputTruncatedError

    exc = OutputTruncatedError(completion_tokens=100, cap=100)
    exc.should_retry = True
    assert provider_error_info(exc).category == "output_budget"
    assert not should_gateway_retry(exc)
    assert not should_failover(exc)
    assert not should_count_breaker_failure(exc)


def test_gateway_retries_default_conflict_but_honors_negative_header() -> None:
    class _ConflictThenOk:
        def __init__(self, should_retry: str | None = None):
            self.calls = 0
            self.should_retry = should_retry

        def complete(self, *, system, user, model):
            self.calls += 1
            if self.calls == 1:
                headers = (
                    {"x-should-retry": self.should_retry}
                    if self.should_retry is not None
                    else None
                )
                raise _HttpError(409, "conflict", headers)
            return "ok", 1, 1

    retryable = _ConflictThenOk()
    gateway = LLMGateway(providers={"cheap": retryable}, max_retries=1)
    assert gateway.complete(task="plan", system="s", user="u", tier="cheap") == "ok"
    assert retryable.calls == 2

    suppressed = _ConflictThenOk("false")
    gateway = LLMGateway(providers={"cheap": suppressed}, max_retries=1)
    with pytest.raises(LLMGatewayError) as excinfo:
        gateway.complete(task="plan", system="s", user="u", tier="cheap")
    assert excinfo.value.category == "conflict"
    assert excinfo.value.gateway_invocations == 1
    assert suppressed.calls == 1


def test_gateway_honors_retry_after_before_bare_legacy_retry() -> None:
    slept: list[float] = []

    class _RetryAfterThenOk:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *, system, user, model):
            self.calls += 1
            if self.calls == 1:
                raise _HttpError(429, "rate limited", {"Retry-After": "1.5"})
            return "ok", 1, 1

    provider = _RetryAfterThenOk()
    gateway = LLMGateway(
        providers={"cheap": provider},
        max_retries=1,
        retry_backoff_seconds=99,
        _sleep=slept.append,
        _uniform=lambda _low, _high: pytest.fail("Retry-After must override jitter"),
    )

    assert gateway.complete(task="plan", system="s", user="u", tier="cheap") == "ok"
    assert slept == [1.5]
    assert provider.calls == 2


def test_gateway_uses_exponential_full_jitter_for_legacy_retry() -> None:
    ranges: list[tuple[float, float]] = []
    slept: list[float] = []

    class _TimeoutThenOk:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *, system, user, model):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("timed out")
            return "ok", 1, 1

    def choose(low: float, high: float) -> float:
        ranges.append((low, high))
        return high / 4

    gateway = LLMGateway(
        providers={"cheap": _TimeoutThenOk()},
        max_retries=1,
        retry_backoff_seconds=4,
        retry_backoff_cap_seconds=10,
        _sleep=slept.append,
        _uniform=choose,
    )

    assert gateway.complete(task="plan", system="s", user="u", tier="cheap") == "ok"
    assert ranges == [(0.0, 4)]
    assert slept == [1.0]


def test_total_deadline_refuses_retry_after_that_cannot_fit() -> None:
    now = {"value": 10.0}
    slept: list[float] = []

    class _AlwaysRateLimited:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *, system, user, model):
            self.calls += 1
            raise _HttpError(429, "rate limited", {"Retry-After": "3"})

    provider = _AlwaysRateLimited()
    gateway = LLMGateway(
        providers={"cheap": provider},
        max_retries=3,
        total_deadline_seconds=2,
        _clock=lambda: now["value"],
        _sleep=slept.append,
    )

    with pytest.raises(LLMGatewayError) as excinfo:
        gateway.complete(task="plan", system="s", user="u", tier="cheap")
    assert excinfo.value.category == "deadline"
    assert excinfo.value.gateway_invocations == 1
    assert provider.calls == 1
    assert slept == []


def test_total_transport_budget_stops_a_new_legacy_invocation() -> None:
    class _AlwaysTimeout:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *, system, user, model):
            self.calls += 1
            raise TimeoutError("timed out")

    provider = _AlwaysTimeout()
    gateway = LLMGateway(
        providers={"cheap": provider},
        max_retries=4,
        max_total_transport_attempts=1,
    )

    with pytest.raises(LLMGatewayError) as excinfo:
        gateway.complete(task="plan", system="s", user="u", tier="cheap")
    assert excinfo.value.category == "retry_budget"
    assert excinfo.value.gateway_invocations == 1
    assert excinfo.value.transport_attempt_cap == 1
    assert provider.calls == 1


def test_sdk_retry_owner_is_not_replayed_or_slept_even_with_retry_after() -> None:
    slept: list[float] = []

    class _SdkOwned:
        owns_retry_policy = True
        max_transport_attempts = 3

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *, system, user, model):
            self.calls += 1
            raise _HttpError(429, "rate limited", {"Retry-After": "1"})

    provider = _SdkOwned()
    gateway = LLMGateway(
        providers={"cheap": provider}, max_retries=5, _sleep=slept.append
    )

    with pytest.raises(LLMGatewayError) as excinfo:
        gateway.complete(task="plan", system="s", user="u", tier="cheap")
    assert excinfo.value.gateway_invocations == 1
    assert excinfo.value.transport_attempt_cap == 3
    assert provider.calls == 1
    assert slept == []


def test_classifier_follows_wrapped_cause_and_is_cycle_safe() -> None:
    outer = RuntimeError("adapter failed")
    inner = TimeoutError("timed out")
    outer.__cause__ = inner
    inner.__context__ = outer  # deliberately malformed cycle: classification must terminate

    assert provider_error_info(outer).category == "timeout"
    assert should_gateway_retry(outer)
    assert should_failover(outer)
    assert should_count_breaker_failure(outer)


def test_explicit_status_in_context_beats_outer_message_guess() -> None:
    outer = RuntimeError("request timed out")
    inner = _HttpError(400, "maximum context 4290")
    outer.__context__ = inner

    assert provider_error_info(outer).category == "bad_request"
    assert not should_gateway_retry(outer)


def test_explicit_cause_supersedes_suppressed_context() -> None:
    outer = RuntimeError("adapter failed")
    outer.__cause__ = TimeoutError("timed out")
    outer.__context__ = _HttpError(400, "bad request")

    assert provider_error_info(outer).category == "timeout"
    assert should_gateway_retry(outer)


# ---------------------------------------------------------------------------
# R3-Team-C ③: unregistered tier → guided LLMGatewayError, not a raw KeyError
# ---------------------------------------------------------------------------


def test_gateway_unregistered_tier_raises_guided_error():
    """Router selects a tier with no provider → friendly LLMGatewayError listing what's available
    (memory red line: 'guided errors, not raw'), never a bare KeyError."""
    # Default StaticRouter maps "generate" -> "frontier", which is not registered here.
    gw = LLMGateway(providers={"cheap": MockProvider()})
    with pytest.raises(LLMGatewayError) as ei:
        gw.complete(task="generate", system="s", user="u")
    err = ei.value
    assert err.category == "config"
    assert err.tier == "frontier"
    msg = str(err)
    # Names the offending tier and lists what IS registered, so the fix is obvious.
    assert "frontier" in msg
    assert "cheap" in msg
    # And it is NOT a raw KeyError surfacing to the caller.
    assert not isinstance(err, KeyError)


def test_gateway_records_real_model_id_on_callrecord():
    """R3-C①: CallRecord.model carries the resolved real model id (from provider.model)."""

    class _ModelProvider:
        model = "deepseek-v4-pro"

        def complete(self, *, system, user, model):
            return "ok", 4, 2

    tel = TelemetryCollector()
    gw = LLMGateway(providers={"frontier": _ModelProvider()}, telemetry=tel)
    gw.complete(task="generate", system="s", user="u")  # generate -> frontier
    assert tel.records[0].model == "deepseek-v4-pro"


def test_gateway_callrecord_model_falls_back_to_tier_for_modelless_provider():
    """A provider with no .model attribute → CallRecord.model falls back to the tier label."""
    tel = TelemetryCollector()
    gw = LLMGateway(providers={"cheap": MockProvider()}, telemetry=tel)
    gw.complete(task="plan", system="s", user="u", tier="cheap")
    assert tel.records[0].model == "cheap"
