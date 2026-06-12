"""In-process job runner with SSE-friendly event buffers.

Long actions (world seed, extraction, theme sweep) run in a worker thread; progress
callbacks append to a per-job event buffer that an SSE endpoint replays and then tails.
Scope is deliberately single-instance (matches the product's desktop/intranet form):
multi-instance deployments would swap this for a shared queue behind the same contract.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_TERMINAL = {"done", "failed"}
_MAX_JOBS = 100


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"  # queued | running | done | failed
    events: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._cond = threading.Condition()

    def submit(
        self, kind: str, runner: Callable[[Callable[[str, dict[str, Any]], None]], dict[str, Any]]
    ) -> Job:
        """Start `runner` in a worker thread. The runner receives an `emit(type, data)`
        progress callback and returns the job result dict."""
        job = Job(id=uuid.uuid4().hex[:16], kind=kind)
        with self._cond:
            self._jobs[job.id] = job
            self._prune_locked()

        def emit(event_type: str, data: dict[str, Any]) -> None:
            self._append(job.id, event_type, data)

        def work() -> None:
            self._set_status(job.id, "running")
            try:
                result = runner(emit)
            except Exception as e:  # noqa: BLE001 - failures become job state, not crashes
                with self._cond:
                    job.error = f"{type(e).__name__}: {e}"
                self._append(job.id, "failed", {"error": job.error})
                self._set_status(job.id, "failed")
                return
            with self._cond:
                job.result = result
            self._append(job.id, "done", {})
            self._set_status(job.id, "done")

        threading.Thread(target=work, name=f"owjob-{kind}-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._cond:
            return self._jobs.get(job_id)

    def wait_events(
        self, job_id: str, start_index: int, *, timeout: float = 10.0
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Return (new events since start_index, next index, job is terminal). Blocks up
        to `timeout` when nothing new is available, so the SSE loop can heartbeat."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                job = self._jobs.get(job_id)
                if job is None:
                    return [], start_index, True
                if len(job.events) > start_index or job.status in _TERMINAL:
                    new = job.events[start_index:]
                    return new, start_index + len(new), job.status in _TERMINAL
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return [], start_index, False
                self._cond.wait(remaining)

    def _append(self, job_id: str, event_type: str, data: dict[str, Any]) -> None:
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.events.append({"type": event_type, "data": data, "ts": time.time()})
            self._cond.notify_all()

    def _set_status(self, job_id: str, status: str) -> None:
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = status
            self._cond.notify_all()

    def _prune_locked(self) -> None:
        if len(self._jobs) <= _MAX_JOBS:
            return
        terminal = sorted(
            (j for j in self._jobs.values() if j.status in _TERMINAL),
            key=lambda j: j.created_at,
        )
        for stale in terminal[: len(self._jobs) - _MAX_JOBS]:
            self._jobs.pop(stale.id, None)
