"""Production composition for the N2.1 triage provider.

The default remains a zero-cost abstention.  A deployment must explicitly opt in
to ``real`` mode; those calls still pass through the project's single LLM gateway
and keep the source text in a separately labelled JSON data envelope.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Protocol

from ..config import DEFAULT_CHEAP_MODEL
from ..llm.gateway import LLMGateway, LLMProvider
from ..llm.resilience import build_real_provider
from ..llm.router import StaticRouter
from .service import AbstainingTriageProvider
from .triage import TriageProvider, TriageRequest

TRIAGE_MODE_ENV = "OWCOPILOT_INTAKE_TRIAGE_MODE"
TRIAGE_MODEL_ENV = "OWCOPILOT_INTAKE_TRIAGE_MODEL"
TRIAGE_TIMEOUT_ENV = "OWCOPILOT_INTAKE_TRIAGE_TIMEOUT_SECONDS"


class _CompletionGateway(Protocol):
    def complete(
        self,
        *,
        task: str,
        system: str,
        user: str,
        tier: str | None = None,
    ) -> str: ...


class _ProviderFactory(Protocol):
    def __call__(
        self,
        model: str,
        *,
        json_mode: bool = True,
        timeout: float | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMProvider: ...


class GatewayTriageProvider:
    """Adapt the versioned intake request to the common LLM gateway."""

    name = "llm-gateway"

    def __init__(
        self,
        gateway: _CompletionGateway,
        *,
        model: str,
        tier: str = "triage",
    ) -> None:
        if type(model) is not str or not model.strip() or model != model.strip():
            raise ValueError("triage model must be a non-empty trimmed identifier")
        if type(tier) is not str or not tier.strip() or tier != tier.strip():
            raise ValueError("triage tier must be a non-empty trimmed identifier")
        self.gateway = gateway
        self.model = model
        self.tier = tier

    def classify(self, request: TriageRequest) -> str:
        if not isinstance(request, TriageRequest) or request.untrusted_input is not True:
            raise ValueError("triage request must carry explicitly untrusted source text")
        user_payload = json.dumps(
            {
                "segment_id": request.segment_id,
                "untrusted_input": True,
                "source_text": request.untrusted_text,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return self.gateway.complete(
            task="intake_triage",
            system=request.system_instruction,
            user=user_payload,
            tier=self.tier,
        )


def build_team_triage_provider_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    provider_factory: _ProviderFactory = build_real_provider,
) -> TriageProvider:
    """Build an honest disabled or real provider from explicit team configuration."""

    values = os.environ if environ is None else environ
    raw_mode = values.get(TRIAGE_MODE_ENV, "disabled")
    if raw_mode != raw_mode.strip():
        raise ValueError(f"{TRIAGE_MODE_ENV} must be 'disabled' or 'real'")
    mode = raw_mode.lower()
    if mode == "disabled":
        return AbstainingTriageProvider()
    if mode != "real":
        raise ValueError(f"{TRIAGE_MODE_ENV} must be 'disabled' or 'real'")

    model = (
        values.get(TRIAGE_MODEL_ENV)
        or values.get("OWCOPILOT_CHEAP_MODEL")
        or DEFAULT_CHEAP_MODEL
    )
    if not model.strip() or model != model.strip():
        raise ValueError(f"{TRIAGE_MODEL_ENV} must be a non-empty trimmed identifier")
    timeout_raw = values.get(TRIAGE_TIMEOUT_ENV, "30")
    try:
        timeout = float(timeout_raw)
    except ValueError as exc:
        raise ValueError(f"{TRIAGE_TIMEOUT_ENV} must be a positive number") from exc
    if timeout <= 0:
        raise ValueError(f"{TRIAGE_TIMEOUT_ENV} must be a positive number")

    provider = provider_factory(
        model=model,
        json_mode=True,
        timeout=timeout,
        max_output_tokens=800,
    )
    gateway = LLMGateway(
        providers={"triage": provider},
        router=StaticRouter(mapping={"intake_triage": "triage"}),
        max_retries=1,
        retry_backoff_seconds=1.0,
        namespace="team-intake",
    )
    return GatewayTriageProvider(gateway, model=model)


__all__ = [
    "TRIAGE_MODE_ENV",
    "TRIAGE_MODEL_ENV",
    "TRIAGE_TIMEOUT_ENV",
    "GatewayTriageProvider",
    "build_team_triage_provider_from_env",
]
