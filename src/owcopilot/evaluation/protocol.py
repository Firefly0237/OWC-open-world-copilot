"""Source of truth for the formal-evaluation artifact protocol.

The protocol is intentionally independent from suite registries, scorers, manifests on disk, and
runners.  It defines the common envelopes and the honesty invariants those later layers must obey.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_SCHEMA_VERSION = "formal-eval/v1"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
Sha256 = Annotated[str, Field(pattern=SHA256_PATTERN)]


class EvalStatus(str, Enum):
    """The only statuses allowed in formal evaluation artifacts."""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    INVALID = "INVALID"
    BLOCKED_EXTERNAL_DATA = "BLOCKED_EXTERNAL_DATA"


class EvalProvider(str, Enum):
    FIXTURE = "fixture"
    SCRIPTED = "scripted"
    REAL_MODEL = "real_model"


class EvalSplit(str, Enum):
    CANARY = "canary"
    DEV = "dev"
    SEALED = "sealed"


class MetricScope(str, Enum):
    HARNESS = "harness"
    QUALITY = "quality"


class _ProtocolModel(BaseModel):
    # Formal artifacts are immutable value objects. This prevents constructing a valid report and
    # then mutating status/count fields before serialization. Pydantic's default ``model_copy``
    # update deliberately skips validation, so updates are rebuilt through ``model_validate``.
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if not update:
            return super().model_copy(deep=deep)
        payload = self.model_dump(mode="python", round_trip=True)
        payload.update(update)
        return self.__class__.model_validate(payload)


class ScorerProvenance(_ProtocolModel):
    scorer_id: str = Field(min_length=1)
    scorer_version: str = Field(min_length=1)
    sha256: Sha256


class ModelProvenance(_ProtocolModel):
    provider: str = Field(min_length=1)
    full_model_id: str = Field(min_length=1)
    configuration_sha256: Sha256


class BudgetCaps(_ProtocolModel):
    """Frozen aggregate limits; all model-using components count against these caps."""

    model_call_cap: int = Field(ge=0)
    token_cap: int = Field(ge=0, description="Hard cap over input + output tokens.")
    cost_usd_cap: float = Field(ge=0)


class ResourceUsage(_ProtocolModel):
    model_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    latency_ms: float = Field(default=0.0, ge=0)


class TraceEvent(_ProtocolModel):
    sequence: int = Field(ge=0)
    kind: str = Field(min_length=1)
    timestamp: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ArtifactReference(_ProtocolModel):
    path: str = Field(min_length=1)
    sha256: Sha256
    byte_count: int = Field(ge=0)
    split: EvalSplit | None = None


class MetricDefinition(_ProtocolModel):
    name: str = Field(min_length=1)
    scope: MetricScope
    description: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    primary: bool = False


class FormalEvalManifest(_ProtocolModel):
    """Common manifest envelope; suite-specific frozen data is added by N0.3-b."""

    schema_version: Literal["formal-eval/v1"]
    suite_id: str = Field(min_length=1)
    suite_version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    status: EvalStatus
    status_reasons: tuple[str, ...] = ()
    trace_schema_version: Literal["formal-eval/v1"]
    report_schema_version: Literal["formal-eval/v1"]
    scorer: ScorerProvenance | None = None
    allowed_providers: tuple[EvalProvider, ...]
    artifacts: tuple[ArtifactReference, ...]
    metrics: tuple[MetricDefinition, ...]


def _merge_reasons(existing: tuple[str, ...], added: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys([*existing, *added]))


class FormalEvalTrace(_ProtocolModel):
    """One case attempt, including enough provenance to audit a real-model result."""

    schema_version: Literal["formal-eval/v1"]
    suite_id: str = Field(min_length=1)
    suite_version: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    split: EvalSplit
    provider: EvalProvider
    status: EvalStatus
    status_reasons: tuple[str, ...] = ()
    started_at: datetime | None = None
    finished_at: datetime | None = None
    model: ModelProvenance | None = None
    prompt_sha256: Sha256 | None = None
    tool_schema_sha256: Sha256 | None = None
    scorer: ScorerProvenance | None = None
    budget: BudgetCaps | None = None
    usage: ResourceUsage | None = None
    trajectory: tuple[TraceEvent, ...] = ()
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def mark_invalid_real_model_provenance(self) -> FormalEvalTrace:
        missing: list[str] = []
        sequences = [event.sequence for event in self.trajectory]
        if sequences != list(range(len(sequences))):
            missing.append("INVALID_TRACE_SEQUENCE")

        if self.provider is EvalProvider.REAL_MODEL:
            required = {
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "model": self.model,
                "prompt_sha256": self.prompt_sha256,
                "tool_schema_sha256": self.tool_schema_sha256,
                "scorer": self.scorer,
                "budget": self.budget,
                "usage": self.usage,
                "trajectory": self.trajectory,
            }
            missing.extend(
                f"MISSING_REAL_MODEL_{name.upper()}"
                for name, value in required.items()
                if not value
            )
            if self.budget is not None and self.usage is not None:
                if self.usage.model_calls > self.budget.model_call_cap:
                    missing.append("MODEL_CALL_BUDGET_EXCEEDED")
                if self.usage.input_tokens + self.usage.output_tokens > self.budget.token_cap:
                    missing.append("TOKEN_BUDGET_EXCEEDED")
                if self.usage.cost_usd > self.budget.cost_usd_cap:
                    missing.append("COST_BUDGET_EXCEEDED")
                if (
                    self.status in {EvalStatus.PASS, EvalStatus.FAIL}
                    and self.usage.model_calls == 0
                ):
                    missing.append("ZERO_REAL_MODEL_CALLS")
            if (
                self.started_at is not None
                and self.finished_at is not None
                and self.finished_at < self.started_at
            ):
                missing.append("FINISHED_BEFORE_STARTED")
        if missing:
            object.__setattr__(self, "status", EvalStatus.INVALID)
            object.__setattr__(
                self,
                "status_reasons",
                _merge_reasons(self.status_reasons, missing),
            )
        return self


class MetricResult(_ProtocolModel):
    name: str = Field(min_length=1)
    scope: MetricScope
    sample_size: int = Field(ge=0)
    value: float | None = None
    success_count: int | None = Field(default=None, ge=0)
    baseline_value: float | None = None
    effect_size: float | None = None
    unit: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_metric_semantics(self) -> MetricResult:
        if self.success_count is not None and self.success_count > self.sample_size:
            raise ValueError("success_count cannot exceed sample_size")
        result_values = (
            self.value,
            self.success_count,
            self.baseline_value,
            self.effect_size,
        )
        if self.sample_size == 0 and any(value is not None for value in result_values):
            raise ValueError("zero-sample metric cannot carry a result")
        if self.sample_size > 0 and self.value is None:
            raise ValueError("nonzero-sample metric must carry a value")
        if self.scope is MetricScope.HARNESS and (
            self.baseline_value is not None or self.effect_size is not None
        ):
            raise ValueError("harness metrics cannot carry baseline/effect-size quality claims")
        return self


class EvaluationCounts(_ProtocolModel):
    total_cases: int = Field(ge=0)
    executed_cases: int = Field(ge=0)
    passed_cases: int = Field(default=0, ge=0)
    failed_cases: int = Field(default=0, ge=0)
    errored_cases: int = Field(default=0, ge=0)
    skipped_cases: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> EvaluationCounts:
        if self.executed_cases > self.total_cases:
            raise ValueError("executed_cases cannot exceed total_cases")
        if self.passed_cases + self.failed_cases + self.errored_cases != self.executed_cases:
            raise ValueError("passed + failed + errored must equal executed_cases")
        if self.executed_cases + self.skipped_cases != self.total_cases:
            raise ValueError("executed_cases + skipped_cases must equal total_cases")
        return self


class FormalEvalReport(_ProtocolModel):
    """Aggregate report with separate harness validity and quality-evaluation status."""

    schema_version: Literal["formal-eval/v1"]
    suite_id: str = Field(min_length=1)
    suite_version: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    split: EvalSplit
    provider: EvalProvider
    generated_at: datetime
    harness_status: EvalStatus
    evaluation_status: EvalStatus
    skipped: bool = False
    status_reasons: tuple[str, ...] = ()
    counts: EvaluationCounts
    primary_metric: str | None = None
    metrics: tuple[MetricResult, ...] = ()
    trace_sha256s: tuple[Sha256, ...] = ()

    @model_validator(mode="after")
    def enforce_honest_statuses(self) -> FormalEvalReport:
        metric_by_name = {metric.name: metric for metric in self.metrics}
        if len(metric_by_name) != len(self.metrics):
            raise ValueError("metric names must be unique")
        if self.primary_metric is not None and self.primary_metric not in metric_by_name:
            raise ValueError("primary_metric must name one of metrics")

        quality_metrics = [metric for metric in self.metrics if metric.scope is MetricScope.QUALITY]
        if self.provider is not EvalProvider.REAL_MODEL and quality_metrics:
            raise ValueError("fixture/scripted providers cannot emit quality-effect metrics")

        all_error = (
            self.counts.executed_cases > 0
            and self.counts.errored_cases == self.counts.executed_cases
        )
        no_run = (
            self.skipped
            or self.counts.total_cases == 0
            or self.counts.executed_cases == 0
            or self.counts.skipped_cases > 0
        )
        primary = metric_by_name.get(self.primary_metric or "")
        zero_denominator = primary is None or primary.sample_size == 0

        if all_error:
            object.__setattr__(self, "harness_status", EvalStatus.INVALID)
            object.__setattr__(self, "evaluation_status", EvalStatus.INVALID)
            object.__setattr__(
                self,
                "status_reasons",
                _merge_reasons(self.status_reasons, ["ALL_TRIALS_ERRORED"]),
            )
            return self

        # A quality verdict is not valid when the harness that produced it did not pass.  Preserve
        # both fields for diagnosis, but propagate the harness boundary to the evaluation status so
        # INVALID/NOT_RUN/NOT_AVAILABLE/BLOCKED reports can never smuggle a quality PASS/FAIL.
        if self.harness_status is not EvalStatus.PASS and self.evaluation_status in {
            EvalStatus.PASS,
            EvalStatus.FAIL,
        }:
            propagated = {
                EvalStatus.INVALID: EvalStatus.INVALID,
                EvalStatus.FAIL: EvalStatus.INVALID,
                EvalStatus.NOT_RUN: EvalStatus.NOT_RUN,
                EvalStatus.NOT_AVAILABLE: EvalStatus.NOT_AVAILABLE,
                EvalStatus.BLOCKED_EXTERNAL_DATA: EvalStatus.BLOCKED_EXTERNAL_DATA,
            }
            object.__setattr__(
                self,
                "evaluation_status",
                propagated[self.harness_status],
            )
            object.__setattr__(
                self,
                "status_reasons",
                _merge_reasons(
                    self.status_reasons,
                    [f"HARNESS_STATUS_{self.harness_status.value}"],
                ),
            )

        if no_run or zero_denominator:
            if self.harness_status in {EvalStatus.PASS, EvalStatus.FAIL} and no_run:
                object.__setattr__(self, "harness_status", EvalStatus.NOT_RUN)
            if self.evaluation_status in {EvalStatus.PASS, EvalStatus.FAIL}:
                object.__setattr__(self, "evaluation_status", EvalStatus.NOT_RUN)
            reason = "NO_EXECUTED_CASES" if no_run else "ZERO_PRIMARY_METRIC_DENOMINATOR"
            object.__setattr__(
                self,
                "status_reasons",
                _merge_reasons(self.status_reasons, [reason]),
            )

        if self.provider is not EvalProvider.REAL_MODEL and self.evaluation_status in {
            EvalStatus.PASS,
            EvalStatus.FAIL,
        }:
            object.__setattr__(self, "evaluation_status", EvalStatus.NOT_RUN)
            object.__setattr__(
                self,
                "status_reasons",
                _merge_reasons(
                    self.status_reasons,
                    ["NON_REAL_PROVIDER_HAS_NO_QUALITY_EFFECT"],
                ),
            )

        if self.evaluation_status in {EvalStatus.PASS, EvalStatus.FAIL}:
            invalid_reasons: list[str] = []
            if primary is None or primary.scope is not MetricScope.QUALITY:
                invalid_reasons.append("NO_PRIMARY_QUALITY_METRIC")
            else:
                if primary.value is None:
                    invalid_reasons.append("MISSING_PRIMARY_METRIC_VALUE")
                if primary.sample_size != self.counts.executed_cases:
                    invalid_reasons.append("PRIMARY_CASE_DENOMINATOR_MISMATCH")
            if not self.trace_sha256s:
                invalid_reasons.append("MISSING_TRACE_HASHES")
            elif len(set(self.trace_sha256s)) != len(self.trace_sha256s):
                invalid_reasons.append("DUPLICATE_TRACE_HASHES")
            if len(self.trace_sha256s) < self.counts.executed_cases:
                invalid_reasons.append("INSUFFICIENT_TRACE_HASHES")
            if invalid_reasons:
                object.__setattr__(self, "evaluation_status", EvalStatus.INVALID)
                object.__setattr__(
                    self,
                    "status_reasons",
                    _merge_reasons(self.status_reasons, invalid_reasons),
                )
        return self


_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "manifest.schema.json": FormalEvalManifest,
    "trace.schema.json": FormalEvalTrace,
    "report.schema.json": FormalEvalReport,
}


class FrozenSchemaMismatchError(ValueError):
    pass


def render_protocol_schemas() -> dict[str, bytes]:
    """Render deterministic UTF-8 schema bytes from the Python source of truth."""

    return {
        filename: (
            json.dumps(
                model.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True
            ).encode("utf-8")
            + b"\n"
        )
        for filename, model in _SCHEMA_MODELS.items()
    }


def verify_frozen_schemas(directory: str | Path) -> None:
    """Raise when any committed schema is missing or differs by one byte."""

    root = Path(directory)
    mismatches: list[str] = []
    for filename, expected in render_protocol_schemas().items():
        path = root / filename
        if not path.exists():
            mismatches.append(f"{filename}: missing")
        elif path.read_bytes() != expected:
            mismatches.append(f"{filename}: byte mismatch")
    if mismatches:
        raise FrozenSchemaMismatchError("; ".join(mismatches))


__all__ = [
    "PROTOCOL_SCHEMA_VERSION",
    "ArtifactReference",
    "BudgetCaps",
    "EvalProvider",
    "EvalSplit",
    "EvalStatus",
    "EvaluationCounts",
    "FormalEvalManifest",
    "FormalEvalReport",
    "FormalEvalTrace",
    "FrozenSchemaMismatchError",
    "MetricDefinition",
    "MetricResult",
    "MetricScope",
    "ModelProvenance",
    "ResourceUsage",
    "ScorerProvenance",
    "TraceEvent",
    "render_protocol_schemas",
    "verify_frozen_schemas",
]
