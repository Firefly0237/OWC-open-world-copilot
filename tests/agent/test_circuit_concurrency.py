from __future__ import annotations

import asyncio
import threading
from typing import NoReturn

import pytest

from owcopilot.llm.circuit import CircuitOpenError, _CircuitState


class _ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _outage() -> NoReturn:
    raise RuntimeError("provider unavailable")


def _open(state: _CircuitState) -> None:
    with pytest.raises(RuntimeError, match="provider unavailable"):
        state.run(_outage)


def test_half_open_allows_exactly_one_sync_probe() -> None:
    clock = _ManualClock()
    state = _CircuitState(failure_threshold=1, reset_timeout_seconds=10, clock=clock)
    _open(state)
    clock.now = 11.0

    probe_started = threading.Event()
    release_probe = threading.Event()
    results: list[str] = []
    errors: list[BaseException] = []
    blocked_call_ran = False

    def probe() -> str:
        probe_started.set()
        if not release_probe.wait(timeout=5):
            raise AssertionError("test did not release the half-open probe")
        return "recovered"

    def run_probe() -> None:
        try:
            results.append(state.run(probe))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def blocked_call() -> str:
        nonlocal blocked_call_ran
        blocked_call_ran = True
        return "must not run"

    thread = threading.Thread(target=run_probe, daemon=True)
    thread.start()
    try:
        assert probe_started.wait(timeout=5)
        with pytest.raises(CircuitOpenError, match="probe already in progress"):
            state.run(blocked_call)
    finally:
        release_probe.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert not blocked_call_ran
    assert errors == []
    assert results == ["recovered"]
    assert state.run(lambda: "closed") == "closed"


def test_half_open_allows_exactly_one_async_probe() -> None:
    async def scenario() -> None:
        clock = _ManualClock()
        state = _CircuitState(failure_threshold=1, reset_timeout_seconds=10, clock=clock)

        async def outage() -> NoReturn:
            raise RuntimeError("provider unavailable")

        with pytest.raises(RuntimeError, match="provider unavailable"):
            await state.arun(outage)
        clock.now = 11.0

        probe_started = asyncio.Event()
        release_probe = asyncio.Event()
        blocked_call_ran = False

        async def probe() -> str:
            probe_started.set()
            await release_probe.wait()
            return "recovered"

        async def blocked_call() -> str:
            nonlocal blocked_call_ran
            blocked_call_ran = True
            return "must not run"

        task = asyncio.create_task(state.arun(probe))
        try:
            await asyncio.wait_for(probe_started.wait(), timeout=5)
            with pytest.raises(CircuitOpenError, match="probe already in progress"):
                await state.arun(blocked_call)
        finally:
            release_probe.set()

        assert await asyncio.wait_for(task, timeout=5) == "recovered"
        assert not blocked_call_ran
        assert await state.arun(probe) == "recovered"

    asyncio.run(scenario())


def test_late_success_from_old_generation_cannot_close_new_open_circuit() -> None:
    state = _CircuitState(failure_threshold=1, reset_timeout_seconds=60)
    old_call_started = threading.Event()
    release_old_call = threading.Event()
    results: list[str] = []
    errors: list[BaseException] = []

    def slow_success() -> str:
        old_call_started.set()
        if not release_old_call.wait(timeout=5):
            raise AssertionError("test did not release the old-generation call")
        return "old success"

    def run_old_call() -> None:
        try:
            results.append(state.run(slow_success))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=run_old_call, daemon=True)
    thread.start()
    try:
        assert old_call_started.wait(timeout=5)
        _open(state)
        assert state.is_open
    finally:
        release_old_call.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert results == ["old success"]
    assert state.is_open
    with pytest.raises(CircuitOpenError):
        state.run(lambda: "must not close")


def test_late_failure_from_old_generation_cannot_restart_new_open_cooldown() -> None:
    clock = _ManualClock()
    state = _CircuitState(failure_threshold=1, reset_timeout_seconds=10, clock=clock)
    old_call_started = threading.Event()
    release_old_call = threading.Event()
    errors: list[BaseException] = []

    def slow_failure() -> NoReturn:
        old_call_started.set()
        if not release_old_call.wait(timeout=5):
            raise AssertionError("test did not release the old-generation call")
        raise RuntimeError("late provider failure")

    def run_old_call() -> None:
        try:
            state.run(slow_failure)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_old_call, daemon=True)
    thread.start()
    try:
        assert old_call_started.wait(timeout=5)
        _open(state)  # opens generation 1 at t=0
        clock.now = 5.0
    finally:
        release_old_call.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    clock.now = 10.1
    assert state.run(lambda: "recovered") == "recovered"


def test_ignored_half_open_failure_releases_probe_without_changing_health() -> None:
    clock = _ManualClock()
    state = _CircuitState(failure_threshold=1, reset_timeout_seconds=10, clock=clock)
    _open(state)
    clock.now = 11.0

    def request_error() -> NoReturn:
        raise ValueError("bad request")

    with pytest.raises(ValueError, match="bad request"):
        state.run(request_error, should_count_failure=lambda _exc: False)

    assert state.trips == 1
    assert state.run(lambda: "recovered") == "recovered"
