"""Opt-in call policy for operations whose retries belong to an outer workflow runner."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LLMExecutionPolicy:
    """Keep transport, JSON repair and refinement under one explicit composition policy.

    Existing entry points accept ``None`` and retain their current defaults. A workflow uses
    ``SINGLE_DISPATCH_POLICY`` at both provider/gateway construction and domain-service calls;
    parse or transport failures then return to its node runner without hidden regeneration.
    """

    sdk_max_retries: int = 2
    gateway_max_retries: int = 0
    max_transport_attempts: int | None = None
    json_repair_retries: int = 1
    allow_refinement: bool = True
    allow_failover: bool = True

    def __post_init__(self) -> None:
        if min(self.sdk_max_retries, self.gateway_max_retries, self.json_repair_retries) < 0:
            raise ValueError("retry counts must be non-negative")
        if self.max_transport_attempts is not None and self.max_transport_attempts < 1:
            raise ValueError("max_transport_attempts must be positive")


SINGLE_DISPATCH_POLICY = LLMExecutionPolicy(
    sdk_max_retries=0,
    gateway_max_retries=0,
    max_transport_attempts=1,
    json_repair_retries=0,
    allow_refinement=False,
    allow_failover=False,
)
