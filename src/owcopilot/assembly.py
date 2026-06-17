"""Shared pipeline assembly helpers.

This module centralises the grounded-pipeline wiring that was previously repeated across demos,
benchmark code and the HTTP service: validator suite, gateway/cache/router assembly, optional
cheap-first cascade generation, repair strategy selection, and optional engine landing.

The goal is deliberately narrow: one focused factory for the *grounded* pipeline. P0's bespoke
mock demo remains separate because it exercises a different, intentionally simplified path.
"""

from __future__ import annotations

from typing import Any, Literal

from .consistency.repair import LLMRepairStrategy, RepairStrategy
from .consistency.validators import (
    FactionConflictValidator,
    PrerequisiteCycleValidator,
    ReferenceValidator,
    TimelineValidator,
)
from .core.orchestrator import build_graph
from .generation.quest import CascadingQuestGenerator, GroundedQuestGenerator
from .llm.cache import CacheBackend, NoOpCache
from .llm.gateway import LLMGateway, LLMProvider
from .llm.router import CascadeRouter, StaticRouter
from .llm.telemetry import TelemetryCollector
from .worldbible.graph import LoreGraph
from .worldbible.models import WorldBible

RouterMode = Literal["static", "cascade"]
PrefixMode = Literal["retrieval", "stable"]


def build_validator_suite(wb: WorldBible) -> list:
    """Return the full P1 consistency suite in the order the loop runs it."""
    lore = LoreGraph(wb)
    return [
        ReferenceValidator(wb),
        PrerequisiteCycleValidator(lore),
        FactionConflictValidator(wb),
        TimelineValidator(wb),
    ]


def build_grounded_pipeline(
    wb: WorldBible,
    *,
    cheap_provider: LLMProvider,
    frontier_provider: LLMProvider,
    use_llm_repair: bool = False,
    router_mode: RouterMode = "static",
    cache: CacheBackend | None = None,
    prefix_mode: PrefixMode = "retrieval",
    llm_max_retries: int = 0,
    llm_retry_backoff_seconds: float = 0.0,
) -> tuple[Any, TelemetryCollector, Any]:
    """Assemble the retrieval-grounded quest pipeline.

    The grounded pipeline is the shared kernel behind:
    - P1 offline / live demo runs
    - P2 benchmark configurations
    - the deployable FastAPI surface

    `router_mode="cascade"` enables cheap-first generation and escalation to the strong tier when
    deterministic validators reject the cheap output.
    """
    telemetry = TelemetryCollector()
    validators = build_validator_suite(wb)
    router = CascadeRouter() if router_mode == "cascade" else StaticRouter()
    gateway = LLMGateway(
        providers={"cheap": cheap_provider, "frontier": frontier_provider},
        router=router,
        cache=cache or NoOpCache(),
        telemetry=telemetry,
        max_retries=llm_max_retries,
        retry_backoff_seconds=llm_retry_backoff_seconds,
    )
    base_generator = GroundedQuestGenerator(gateway, wb, prefix_mode=prefix_mode)
    generator = (
        CascadingQuestGenerator(base_generator, validators, strong_tier="frontier")
        if router_mode == "cascade"
        else base_generator
    )
    repair_strategy = (
        LLMRepairStrategy(gateway, wb, validators=validators, fallback=RepairStrategy(wb))
        if use_llm_repair
        else RepairStrategy(wb)
    )
    graph = build_graph(
        gateway=gateway,
        generator=generator,
        validators=validators,
        repair_strategy=repair_strategy,
    )
    return graph, telemetry, generator
