"""OpenTelemetry GenAI semantic-convention span tree for OWCopilot agents.

Architecture overview
---------------------
Activation is **opt-in via environment variable** — when ``OWCOPILOT_OTEL_ENABLED`` is not
set to ``"1"`` the module is a no-op: no OTEL packages are imported, no objects are created,
existing behaviour is byte-for-byte identical.

When enabled, the module provides:

* A **tracer factory** (``get_tracer()``) that creates an OTEL TracerProvider backed by the
  configured exporters.
* A **SQLite span exporter** (``SqliteSpanExporter``) that persists every finished span to a
  local SQLite file (``owcopilot_traces.db`` by default, overridable via
  ``OWCOPILOT_OTEL_SQLITE_PATH``).  Spans survive process restart and can be queried by
  ``run_id``/``trace_id``.
* An **OTLP gRPC exporter** that forwards to any OTEL-compatible backend (Jaeger, Grafana
  Tempo, Uptrace …) when ``OWCOPILOT_OTEL_ENDPOINT`` is set.
* Context managers for the canonical **span tree**::

      invoke_agent          (root span, one per agent.run())
      └── gen_ai.chat       (one per LLM call, child of invoke_agent)
          └── execute_tool  (one per tool execution, child of gen_ai.chat)

Span attributes follow the *OpenTelemetry Semantic Conventions for Generative AI* (v1.39+):
  ``gen_ai.system``, ``gen_ai.operation.name``, ``gen_ai.request.model``,
  ``gen_ai.usage.input_tokens``, ``gen_ai.usage.output_tokens``, ``gen_ai.tool.name``,
  ``gen_ai.agent.name``.

Persistence and queryability (P4B)
-----------------------------------
Every exported span row contains ``trace_id`` (hex-32), ``span_id`` (hex-16),
``parent_span_id`` (hex-16 or empty = root), ``run_id`` (= ``trace_id``), ``step_idx``,
and all standard GenAI attributes.  The ``query_by_run_id()`` function returns the full
span tree for a given run, enabling replay and debugging without a running backend.

In-process TelemetryCollector is preserved
-------------------------------------------
This module complements, not replaces, the existing TelemetryCollector
(:class:`~owcopilot.llm.telemetry.TelemetryCollector`).
The TelemetryCollector handles per-call cost aggregation and ``cost_is_estimate`` flags —
concerns OTEL does not natively address.  Both run in parallel when OTEL is enabled.

Environment variables
---------------------
``OWCOPILOT_OTEL_ENABLED``      "1" to activate (default off)
``OWCOPILOT_OTEL_ENDPOINT``     OTLP gRPC endpoint, e.g. "http://localhost:4317" (optional)
``OWCOPILOT_OTEL_SQLITE_PATH``  Path to SQLite trace DB (default: owcopilot_traces.db)
``OWCOPILOT_OTEL_SERVICE_NAME`` Service name in spans (default: "owcopilot")
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Generator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass  # only used for type annotations that are already strings

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

_OTEL_ENABLED_ENV = "OWCOPILOT_OTEL_ENABLED"
_OTEL_ENDPOINT_ENV = "OWCOPILOT_OTEL_ENDPOINT"
_OTEL_SQLITE_PATH_ENV = "OWCOPILOT_OTEL_SQLITE_PATH"
_OTEL_SERVICE_NAME_ENV = "OWCOPILOT_OTEL_SERVICE_NAME"


def _default_sqlite_path() -> str:
    """Return an absolute path to the default SQLite trace DB.

    Resolution order:
    1. ``OWCOPILOT_OTEL_SQLITE_PATH`` env var (if set, used as-is by callers)
    2. ``~/.owcopilot/traces/owcopilot_traces.db`` — user-home-based absolute path,
       never CWD-relative so traces are not scattered across working directories.
    """
    base = os.path.expanduser("~/.owcopilot/traces")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "owcopilot_traces.db")


_DEFAULT_SERVICE_NAME = "owcopilot"


def otel_enabled() -> bool:
    """True only when ``OWCOPILOT_OTEL_ENABLED=1`` (case-insensitive)."""
    return os.getenv(_OTEL_ENABLED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# SQLite span exporter — persists spans to a local DB, queryable by run_id
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS otel_spans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id        TEXT NOT NULL,
    span_id         TEXT NOT NULL,
    parent_span_id  TEXT NOT NULL DEFAULT '',
    run_id          TEXT NOT NULL,
    name            TEXT NOT NULL,
    start_time_ns   INTEGER NOT NULL,
    end_time_ns     INTEGER NOT NULL,
    status_code     TEXT NOT NULL DEFAULT 'OK',
    attributes      TEXT NOT NULL DEFAULT '{}',
    step_idx        INTEGER NOT NULL DEFAULT -1
)
"""

_INSERT_SPAN_SQL = """
INSERT INTO otel_spans
    (trace_id, span_id, parent_span_id, run_id, name,
     start_time_ns, end_time_ns, status_code, attributes, step_idx)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_CREATE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_otel_spans_run_id ON otel_spans (run_id)"


def _ensure_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute(_CREATE_INDEX_SQL)
    conn.commit()
    return conn


class SqliteSpanExporter:
    """OTEL SpanExporter that persists finished spans to a SQLite file.

    This class implements the ``opentelemetry.sdk.trace.export.SpanExporter`` interface
    without importing OTEL at class-definition time so the module stays importable even
    when ``opentelemetry-sdk`` is not installed (only used when enabled).
    """

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path if db_path is not None else _default_sqlite_path()
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _ensure_db(self.db_path)
        return self._conn

    def export(self, spans: Any) -> Any:
        """Persist a batch of OTEL spans to SQLite.  Returns SUCCESS."""
        from opentelemetry.sdk.trace.export import SpanExportResult  # noqa: PLC0415

        conn = self._get_conn()
        rows = []
        for span in spans:
            ctx = span.get_span_context()
            trace_id_hex = format(ctx.trace_id, "032x") if ctx else ""
            span_id_hex = format(ctx.span_id, "016x") if ctx else ""
            parent_ctx = span.parent
            parent_span_id_hex = (
                format(parent_ctx.span_id, "016x") if parent_ctx else ""
            )
            run_id = trace_id_hex  # run_id == trace_id for single-process runs
            attrs = dict(span.attributes or {})
            step_idx = int(attrs.get("agent.step_idx", -1))
            status_code = span.status.status_code.name if span.status else "UNSET"
            rows.append((
                trace_id_hex,
                span_id_hex,
                parent_span_id_hex,
                run_id,
                span.name,
                span.start_time or 0,
                span.end_time or 0,
                status_code,
                json.dumps(attrs, ensure_ascii=False),
                step_idx,
            ))
        try:
            conn.executemany(_INSERT_SPAN_SQL, rows)
            conn.commit()
        except Exception as exc:
            _log.warning("SqliteSpanExporter: DB write failed (%r).", exc)
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002
        return True


# ---------------------------------------------------------------------------
# Tracer factory
# ---------------------------------------------------------------------------

_TRACER_PROVIDER: Any = None  # opentelemetry.sdk.trace.TracerProvider | None
_TRACER_PROVIDER_LOCK: threading.Lock = threading.Lock()


def get_tracer(service_name: str | None = None) -> Any:
    """Return the module-level TracerProvider's tracer (lazy init, thread-safe).

    On first call this builds a TracerProvider with:
    - SqliteSpanExporter (always, when OTEL enabled)
    - OTLPSpanExporter (when OWCOPILOT_OTEL_ENDPOINT is set)

    Subsequent calls return the same tracer.  A module-level ``threading.Lock``
    prevents double-initialisation when two threads call ``get_tracer()``
    concurrently before the first provider is set (classic check-lock-check pattern).
    """
    global _TRACER_PROVIDER

    # Fast path: provider already initialised — no lock needed.
    if _TRACER_PROVIDER is not None:
        return _TRACER_PROVIDER.get_tracer(
            service_name or os.getenv(_OTEL_SERVICE_NAME_ENV, _DEFAULT_SERVICE_NAME)
        )

    with _TRACER_PROVIDER_LOCK:
        # Re-check inside the lock: another thread may have initialised while we waited.
        if _TRACER_PROVIDER is not None:
            return _TRACER_PROVIDER.get_tracer(
                service_name or os.getenv(_OTEL_SERVICE_NAME_ENV, _DEFAULT_SERVICE_NAME)
            )

        from opentelemetry import trace as otel_trace  # noqa: PLC0415
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

        svc: str = (
            service_name
            or os.getenv(_OTEL_SERVICE_NAME_ENV, _DEFAULT_SERVICE_NAME)
            or _DEFAULT_SERVICE_NAME
        )
        resource = Resource.create({"service.name": svc})
        provider = TracerProvider(resource=resource)

        # SQLite exporter — always enabled when OTEL is on.
        # Use env-override if set, otherwise fall back to absolute default path.
        sqlite_path = os.getenv(_OTEL_SQLITE_PATH_ENV) or _default_sqlite_path()
        sqlite_exporter = SqliteSpanExporter(db_path=sqlite_path)
        provider.add_span_processor(BatchSpanProcessor(sqlite_exporter))  # type: ignore[arg-type]

        # Optional OTLP gRPC exporter
        endpoint = os.getenv(_OTEL_ENDPOINT_ENV, "")
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
                    OTLPSpanExporter,
                )

                otlp_exporter = OTLPSpanExporter(endpoint=endpoint)
                provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
                _log.info("OTEL: OTLP gRPC exporter configured → %s", endpoint)
            except ImportError:
                _log.warning(
                    "OTEL: OTLP exporter requested but opentelemetry-exporter-otlp-proto-grpc "
                    "is not installed.  Install it with: "
                    "pip install opentelemetry-exporter-otlp-proto-grpc"
                )

        otel_trace.set_tracer_provider(provider)
        _TRACER_PROVIDER = provider
        return provider.get_tracer(svc)


# ---------------------------------------------------------------------------
# In-memory exporter (for unit tests — no SQLite, no network)
# ---------------------------------------------------------------------------

class InMemoryExporter:
    """Minimal OTEL SpanExporter that collects finished spans in a list (test helper).

    Usage::

        exporter = InMemoryExporter()
        tracer = build_test_tracer(exporter)
        # ... run code under test ...
        spans = exporter.get_finished_spans()
    """

    def __init__(self) -> None:
        self._spans: list[Any] = []

    def export(self, spans: Any) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult  # noqa: PLC0415

        self._spans.extend(spans)
        return SpanExportResult.SUCCESS

    def get_finished_spans(self) -> list[Any]:
        return list(self._spans)

    def clear(self) -> None:
        self._spans.clear()

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002
        return True


def build_test_tracer(exporter: InMemoryExporter | None = None) -> tuple[Any, InMemoryExporter]:
    """Build an isolated test TracerProvider with an in-memory exporter.

    Returns ``(tracer, exporter)`` — the exporter can be inspected after the test to
    assert span structure without hitting SQLite or any network backend.

    Example::

        tracer, exporter = build_test_tracer()
        with tracer.start_as_current_span("invoke_agent") as root:
            root.set_attribute("gen_ai.agent.name", "test-agent")
        spans = exporter.get_finished_spans()
        assert spans[0].name == "invoke_agent"
    """
    from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: PLC0415

    if exporter is None:
        exporter = InMemoryExporter()

    resource = Resource.create({"service.name": "owcopilot-test"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(exporter))  # type: ignore[arg-type]
    tracer = provider.get_tracer("owcopilot-test")
    return tracer, exporter


# ---------------------------------------------------------------------------
# Span-tree context managers for react.py instrumentation
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def invoke_agent_span(
    tracer: Any,
    *,
    agent_name: str,
    goal: str,
    run_id: str = "",
) -> Generator[Any, None, None]:
    """Root span for one ``agent.run()`` call.

    Attributes (GenAI semantic conventions):
    - ``gen_ai.operation.name`` = "invoke_agent"
    - ``gen_ai.agent.name``     = agent_name
    - ``agent.goal``            = goal (truncated to 500 chars)
    - ``agent.run_id``          = run_id (set after span start)
    """
    with tracer.start_as_current_span(
        "invoke_agent",
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": agent_name,
            "gen_ai.system": "owcopilot",
            "agent.goal": goal[:500],
        },
    ) as span:
        if run_id:
            span.set_attribute("agent.run_id", run_id)
        yield span


@contextlib.contextmanager
def gen_ai_chat_span(
    tracer: Any,
    *,
    model: str,
    step_idx: int,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> Generator[Any, None, None]:
    """Child span for one LLM call.

    Attributes:
    - ``gen_ai.operation.name``      = "chat"
    - ``gen_ai.request.model``       = model
    - ``gen_ai.usage.input_tokens``  = input_tokens
    - ``gen_ai.usage.output_tokens`` = output_tokens
    - ``agent.step_idx``             = step_idx
    """
    with tracer.start_as_current_span(
        "gen_ai.chat",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": model,
            "gen_ai.usage.input_tokens": input_tokens,
            "gen_ai.usage.output_tokens": output_tokens,
            "agent.step_idx": step_idx,
        },
    ) as span:
        yield span


@contextlib.contextmanager
def execute_tool_span(
    tracer: Any,
    *,
    tool_name: str,
    step_idx: int,
) -> Generator[Any, None, None]:
    """Child span for one tool execution.

    Attributes:
    - ``gen_ai.operation.name`` = "execute_tool"
    - ``gen_ai.tool.name``      = tool_name
    - ``agent.step_idx``        = step_idx
    """
    with tracer.start_as_current_span(
        "execute_tool",
        attributes={
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": tool_name,
            "agent.step_idx": step_idx,
        },
    ) as span:
        yield span


# ---------------------------------------------------------------------------
# Query interface — replay / debugging (P4B-3)
# ---------------------------------------------------------------------------

def query_by_run_id(run_id: str, db_path: str | None = None) -> list[dict[str, Any]]:
    """Return all spans for *run_id* from the SQLite trace store, ordered by start time.

    Each row is a dict with keys: ``trace_id``, ``span_id``, ``parent_span_id``,
    ``run_id``, ``name``, ``start_time_ns``, ``end_time_ns``, ``status_code``,
    ``attributes`` (parsed dict), ``step_idx``.

    Example::

        spans = query_by_run_id("abc123def456...")
        for s in spans:
            print(s["name"], s["attributes"].get("gen_ai.tool.name"))
    """
    path_str: str = (
        db_path
        or os.getenv(_OTEL_SQLITE_PATH_ENV)
        or _default_sqlite_path()
    )
    if not os.path.exists(path_str):
        return []

    conn = sqlite3.connect(path_str, check_same_thread=False)
    try:
        cur = conn.execute(
            "SELECT trace_id, span_id, parent_span_id, run_id, name, "
            "start_time_ns, end_time_ns, status_code, attributes, step_idx "
            "FROM otel_spans WHERE run_id = ? ORDER BY start_time_ns ASC",
            (run_id,),
        )
        rows = []
        for row in cur.fetchall():
            try:
                attrs = json.loads(row[8])
            except (ValueError, TypeError):
                attrs = {}
            rows.append({
                "trace_id": row[0],
                "span_id": row[1],
                "parent_span_id": row[2],
                "run_id": row[3],
                "name": row[4],
                "start_time_ns": row[5],
                "end_time_ns": row[6],
                "status_code": row[7],
                "attributes": attrs,
                "step_idx": row[9],
            })
        return rows
    finally:
        conn.close()


def trace_id_of_span(span: Any) -> str:
    """Return the 32-char hex trace_id of *span*, or "" for a no-op / invalid span.

    Used by react.py to back-fill ``agent.run_id`` on the root span (run_id == trace_id in the
    SQLite exporter).  Safe against the no-op span (whose ``get_span_context()`` returns None).
    """
    try:
        ctx = span.get_span_context()
        if ctx is not None and getattr(ctx, "is_valid", False):
            return format(ctx.trace_id, "032x")
    except Exception:
        pass
    return ""


def _extract_trace_id_from_tracer(tracer: Any) -> str:
    """Extract the current trace_id from the active span (helper for react.py)."""
    try:
        from opentelemetry import trace as otel_trace  # noqa: PLC0415

        span = otel_trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x")
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# No-op shims — returned when OTEL is disabled so call sites need no if-guards
# ---------------------------------------------------------------------------

class _NoOpSpan:
    """A span that does nothing, used when OTEL is disabled."""

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: ARG002
        pass

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        pass

    def record_exception(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get_span_context(self) -> None:
        return None


@contextlib.contextmanager
def _noop_span() -> Generator[_NoOpSpan, None, None]:
    yield _NoOpSpan()


class _NoOpTracer:
    """A tracer that returns no-op spans, used when OTEL is disabled."""

    @contextlib.contextmanager
    def start_as_current_span(
        self, name: str, **kwargs: Any
    ) -> Generator[_NoOpSpan, None, None]:  # noqa: ARG002
        yield _NoOpSpan()


_NOOP_TRACER = _NoOpTracer()


def get_tracer_or_noop() -> Any:
    """Return a real tracer when OTEL is enabled, or a no-op tracer otherwise.

    This is the recommended call site in react.py: the rest of the agent code
    uses the same span context managers regardless of whether OTEL is on.
    """
    if not otel_enabled():
        return _NOOP_TRACER
    return get_tracer()
