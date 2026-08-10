# 熔断器状态机：CLOSED/OPEN/half-open 的连续失败计数与冷却探测，供 provider 与 chat model 两层复用。
# 单独成模块以打破 resilience(文本路径) 与 chat_model(agent 路径) 的循环依赖——两者都只依赖这里。
"""The circuit-breaker state machine, shared by both resilience wrappers.

One implementation of the CLOSED / OPEN / half-open bookkeeping, used by BOTH the text path's
:class:`~owcopilot.llm.resilience.CircuitBreakerProvider` (wraps an ``LLMProvider``) and the agent
path's :class:`~owcopilot.llm.chat_model.CircuitBreakerChatModel` (wraps a LangChain ``Runnable``).
It lives in its own module so neither of those two modules has to import the other —
``resilience`` already imports ``chat_model`` (for ``ChatModelProvider``), so a breaker in
``chat_model`` reaching back for the state machine would close an import cycle. Here both sides
depend only on this leaf module.

State machine (in-memory, single-process — a multi-instance deploy would share the state):

* **CLOSED** — calls pass through; ``failure_threshold`` *consecutive* failures open the circuit.
* **OPEN** — calls fail fast with :class:`CircuitOpenError` for ``reset_timeout_seconds``.
* **half-open** — the first call after the cooldown is a trial: success closes the circuit, a
  failure re-opens it (restarting the cooldown without counting a new trip).

``trips`` counts how many times the circuit has opened (a re-open from half-open is not a new trip).
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..errors import OWCopilotError

#: The circuit-breaker env contract — ONE set of names both the text path
#: (:func:`~owcopilot.llm.resilience.build_real_provider`) and the agent path
#: (:func:`~owcopilot.llm.chat_model.build_chat_model_with_resilience`) read, so a studio
#: configures the breaker once for both.
CIRCUIT_BREAKER_ENV = "OWCOPILOT_CIRCUIT_BREAKER"
CIRCUIT_FAILURE_THRESHOLD_ENV = "OWCOPILOT_CIRCUIT_FAILURE_THRESHOLD"
CIRCUIT_RESET_SEC_ENV = "OWCOPILOT_CIRCUIT_RESET_SEC"


def circuit_enabled_from_env() -> bool:
    """Whether ``OWCOPILOT_CIRCUIT_BREAKER`` opts the circuit breaker in (unset ⇒ off)."""
    return os.getenv(CIRCUIT_BREAKER_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def circuit_state_from_env() -> _CircuitState:
    """A fresh :class:`_CircuitState` configured from the env contract (threshold 5 / reset 30s)."""
    return _CircuitState(
        failure_threshold=int(os.getenv(CIRCUIT_FAILURE_THRESHOLD_ENV, "5")),
        reset_timeout_seconds=float(os.getenv(CIRCUIT_RESET_SEC_ENV, "30")),
    )


class CircuitOpenError(OWCopilotError, RuntimeError):
    """Raised by a circuit breaker when the circuit is open — fail fast, no call made."""


@dataclass(frozen=True, slots=True)
class _CallToken:
    """The circuit generation in which a call was admitted."""

    generation: int
    is_half_open_probe: bool


class _CircuitState:
    """The consecutive-failure / open-cooldown / half-open-trial bookkeeping for one circuit.

    Shared by a breaker and every wrapper derived from it (e.g. a ``bind_tools`` result), so the
    circuit's health outlives any single wrapper instance: the agent host re-binds tools on the
    breaker on every model call, and those bound wrappers must all read and mutate the SAME state.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.reset_timeout_seconds = max(0.0, reset_timeout_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None  # None = closed
        self._generation = 0
        self._half_open_probe_generation: int | None = None
        self.trips = 0

    @property
    def is_open(self) -> bool:
        """True while the circuit is fast-failing (open and still within the cooldown window)."""
        with self._lock:
            return self._opened_at is not None and (
                self._clock() - self._opened_at < self.reset_timeout_seconds
            )

    def _guard(self) -> _CallToken:
        """Admit a closed call or atomically reserve the sole half-open probe."""
        with self._lock:
            if self._opened_at is None:
                return _CallToken(self._generation, is_half_open_probe=False)

            if self._clock() - self._opened_at < self.reset_timeout_seconds:
                raise CircuitOpenError("provider circuit is open; failing fast")

            # Cooldown has elapsed. Reserve the half-open probe while still holding the same lock
            # used by every other admission, so sync and async callers cannot both slip through.
            if self._half_open_probe_generation == self._generation:
                raise CircuitOpenError("provider circuit is half-open; probe already in progress")
            self._half_open_probe_generation = self._generation
            return _CallToken(self._generation, is_half_open_probe=True)

    def _on_failure(self, token: _CallToken) -> None:
        with self._lock:
            if token.generation != self._generation:
                return

            if token.is_half_open_probe:
                if self._half_open_probe_generation != token.generation:
                    return
                # A failed probe re-opens the circuit and starts a fresh cooldown. It is a new
                # generation, but not a new trip: the circuit never closed between the two.
                self._half_open_probe_generation = None
                self._failures = self.failure_threshold
                self._opened_at = self._clock()
                self._generation += 1
                return

            if self._opened_at is not None:
                return
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self.trips += 1
                self._opened_at = self._clock()
                self._half_open_probe_generation = None
                self._generation += 1

    def _on_success(self, token: _CallToken) -> None:
        with self._lock:
            if token.generation != self._generation:
                return

            if token.is_half_open_probe:
                if self._half_open_probe_generation != token.generation:
                    return
                self._half_open_probe_generation = None
                self._failures = 0
                self._opened_at = None
                self._generation += 1
                return

            if self._opened_at is not None:
                return
            self._failures = 0

    def _on_ignored_failure(self, token: _CallToken) -> None:
        """Release an eligible probe when the failure is outside breaker-health policy."""
        if not token.is_half_open_probe:
            return
        with self._lock:
            if (
                token.generation == self._generation
                and self._half_open_probe_generation == token.generation
            ):
                self._half_open_probe_generation = None

    def run(
        self,
        fn: Callable[[], Any],
        *,
        should_count_failure: Callable[[Exception], bool] | None = None,
    ) -> Any:
        """Run *fn* under the circuit and update the streak for eligible failures only.

        ``None`` preserves the state machine's original count-all behaviour for direct callers.
        Provider wrappers pass an availability-only predicate so deterministic request/configuration
        failures neither increment nor reset provider-health state.
        """
        token = self._guard()
        try:
            result = fn()
        except Exception as exc:
            if should_count_failure is None or should_count_failure(exc):
                self._on_failure(token)
            else:
                self._on_ignored_failure(token)
            raise
        except BaseException:
            # Cancellation/interrupts are not provider-health failures, but a half-open slot must
            # not remain permanently occupied when one escapes the probe.
            self._on_ignored_failure(token)
            raise
        self._on_success(token)
        return result

    async def arun(
        self,
        afn: Callable[[], Awaitable[Any]],
        *,
        should_count_failure: Callable[[Exception], bool] | None = None,
    ) -> Any:
        """Async sibling of :meth:`run` — identical bookkeeping around an awaited call."""
        token = self._guard()
        try:
            result = await afn()
        except Exception as exc:
            if should_count_failure is None or should_count_failure(exc):
                self._on_failure(token)
            else:
                self._on_ignored_failure(token)
            raise
        except BaseException:
            # ``asyncio.CancelledError`` is a BaseException on supported Python versions.
            self._on_ignored_failure(token)
            raise
        self._on_success(token)
        return result
