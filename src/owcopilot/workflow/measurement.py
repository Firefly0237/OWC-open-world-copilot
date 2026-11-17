"""Per-dispatch observations, isolated across concurrent workflow nodes.

Aligns request/response model and token meanings with GenAI semantic conventions.
Configured-rate calculations are estimates, never provider invoices or recovered history.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from typing import Any
from urllib.parse import urlsplit

from ..llm.execution_policy import LLMExecutionPolicy
from ..llm.telemetry import CallRecord, TelemetryCollector, _parse_tier_prices

_ACTIVE: ContextVar[tuple[object, list[CallRecord]] | None] = ContextVar(
    "workflow_call_measurements", default=None
)


def bind_measurement_capture(collector: TelemetryCollector, owner: object) -> None:
    """Keep existing sinks, without slicing a shared cross-thread records list."""
    previous_sink = collector.record_sink

    def capture(records: list[CallRecord]) -> None:
        active = _ACTIVE.get()
        if active is not None and active[0] is owner:
            active[1].extend(records)
        if previous_sink is not None:
            previous_sink(records)

    collector.record_sink = capture


@contextmanager
def collect_measurements(owner: object) -> Iterator[list[CallRecord]]:
    records: list[CallRecord] = []
    token = _ACTIVE.set((owner, records))
    try:
        yield records
    finally:
        _ACTIVE.reset(token)


def provider_settings(provider: Any) -> dict[str, Any]:
    """Snapshot a small allowlist; credentials, URL queries and arbitrary repr never enter."""
    seen: set[int] = set()
    wrappers: list[str] = []
    while provider is not None and id(provider) not in seen and len(wrappers) < 5:
        seen.add(id(provider))
        wrappers.append(type(provider).__name__)
        inner = getattr(provider, "inner", None)
        if inner is None:
            break
        provider = inner
    result: dict[str, Any] = {"wrappers": wrappers}
    if provider is None:
        return result
    for name in (
        "model",
        "provider_kind",
        "timeout",
        "max_output_tokens",
        "max_retries",
        "json_mode",
    ):
        value = getattr(provider, name, None)
        if isinstance(value, (str, int, float, bool)):
            result[name] = value
    base_url = getattr(provider, "base_url", None)
    if base_url:
        try:
            result["server_address"] = urlsplit(str(base_url)).hostname
        except ValueError:
            result["server_address"] = None
    # Defaults supplied only by the SDK remain unknown; do not invent sampling settings.
    result["sampling_settings"] = "not_captured_or_provider_default"
    return result


def call_measurement(
    records: list[CallRecord],
    *,
    provider: Any,
    policy: LLMExecutionPolicy | None,
    duration_ms: float,
) -> dict[str, Any]:
    observed = records[0] if len(records) == 1 else None
    usage_available = observed is not None and (observed.success or observed.cache_hit)
    rates = _parse_tier_prices(observed.tier) if observed and usage_available else None
    if rates is not None and any(not math.isfinite(rate) or rate < 0 for rate in rates):
        rates = None
    cost = None
    if observed is not None and observed.cache_hit:
        cost = 0.0
    elif observed is not None and usage_available and rates is not None:
        hit = min(max(observed.cached_input_tokens, 0), observed.input_tokens)
        cost = (
            hit * rates[0]
            + (observed.input_tokens - hit) * rates[1]
            + observed.output_tokens * rates[2]
        ) / 1_000_000
    return {
        "schema_version": 1,
        "capture_status": "captured"
        if observed is not None
        else "missing"
        if not records
        else "ambiguous",
        "request_config": provider_settings(provider),
        "policy": asdict(policy) if policy is not None else None,
        "requested_model": observed.model if observed else None,
        "response_model": observed.response_model or None if observed else None,
        "usage_status": "gateway_reported" if usage_available else "unknown",
        "input_tokens": observed.input_tokens if usage_available and observed else None,
        "output_tokens": observed.output_tokens if usage_available and observed else None,
        "cached_input_tokens": observed.cached_input_tokens
        if usage_available and observed
        else None,
        "client_cache_hit": observed.cache_hit if observed else None,
        "gateway_invocations": observed.gateway_invocations if observed else None,
        "transport_attempt_cap": observed.transport_attempt_cap if observed else None,
        "call_duration_ms": duration_ms,
        "gateway_latency_ms": observed.latency_ms if observed else None,
        "error_category": observed.error_category or None if observed else None,
        "cost_estimate_usd": cost,
        "cost_basis": "client_cache_no_provider_call"
        if observed and observed.cache_hit
        else "configured_rates"
        if cost is not None
        else "unknown",
        "prices_usd_per_million": list(rates) if rates is not None else None,
        "limits": [
            "Gateway-reported usage is not a provider invoice; missing usage stays unknown.",
            "Transport attempt cap is a configured upper bound, not observed HTTP attempts.",
            "Provider-default sampling settings are not reconstructed.",
        ],
    }
