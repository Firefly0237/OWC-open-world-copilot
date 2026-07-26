# Checkpoint 持久化 + 生命周期：断点续跑/HITL 用的执行态存储（独立 checkpoints.sqlite）。
# 与业务 runtime.sqlite 分文件（避写锁争用、易清理），SqliteSaver 自身不设 busy_timeout 由此处补。
# 生产级生命周期：一张自管的 checkpoint_threads 登记表（created/last_touched/source）驱动
# TTL 过期清理 + 线程数上限封顶，每次装配 run 前 sweep 一次，让盘占用有界、不无限增长。
"""Checkpoint persistence + lifecycle for agent resume / HITL.

The LangGraph ``SqliteSaver`` stores execution-state channels (message history, pending writes)
keyed by ``thread_id`` — the substrate a resumed run (``invoke(None)``) or a T6 ``interrupt``
continues from. This module owns three things the raw saver does not:

1. **An independent DB file** ``checkpoints.sqlite`` (design §4): execution-state blobs never
   pollute the business/audit ``runtime.sqlite`` and the two write paths never contend on one
   file's WAL write lock.
2. **A ``busy_timeout`` on the connection** — ``SqliteSaver`` sets ``journal_mode=WAL`` in
   ``setup()`` but never a busy timeout, so a bare saver connection can surface transient
   contention as an immediate "database is locked"; we set it before handing the connection over.
3. **A production-grade lifecycle** — a self-managed ``checkpoint_threads`` registry
   (``created_at`` / ``last_touched_at`` / ``source``) drives a TTL sweep (drop threads untouched
   for longer than the TTL) plus a newest-N cap, so opportunistically sweeping once per run keeps
   the checkpoint file bounded without a separate cron. ``delete_thread`` removes both the saver's
   ``checkpoints``/``writes`` rows and the registry row.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from langgraph.checkpoint.sqlite import SqliteSaver

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

# Lifecycle defaults (overridable per-run via env for a studio's retention policy). Fourteen days
# comfortably outlives any realistic "crash then resume tomorrow" window while a 1000-thread cap
# bounds the file even if the TTL is set very long.
_DEFAULT_TTL_DAYS = 14.0
_DEFAULT_MAX_THREADS = 1000

_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS checkpoint_threads (
    thread_id       TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    last_touched_at TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT ''
)
"""


def _now_iso() -> str:
    # UTC ISO-8601 with a fixed-width ``+00:00`` offset, so string ordering == chronological order
    # (what the TTL/cap sweeps rely on).
    return datetime.now(UTC).isoformat()


def _env_ttl_seconds() -> float:
    return float(os.getenv("OWCOPILOT_CHECKPOINT_TTL_DAYS", str(_DEFAULT_TTL_DAYS))) * 86400.0


def _env_max_threads() -> int:
    return int(os.getenv("OWCOPILOT_CHECKPOINT_MAX_THREADS", str(_DEFAULT_MAX_THREADS)))


def resolve_thread_id(
    checkpointer: BaseCheckpointSaver | None,
    *,
    thread_id: str | None,
    resume: bool,
) -> str | None:
    """Resolve the effective ``thread_id`` for one run — the single policy both agent paths share.

    * No checkpointer: ``thread_id``/``resume`` are meaningless, so passing either is a misuse that
      fails loudly (never a silent no-op) and the default path returns ``None`` (byte-identical to
      the pre-T4 behaviour — no ``configurable`` key on the config).
    * Resume: an explicit ``thread_id`` is required — there is nothing to continue without one.
    * Fresh run with a checkpointer: honour an explicit ``thread_id`` (pin it for later resume) or
      mint a unique one (the "always-on, auto thread_id per run" default). A fresh UUID guarantees
      the run's produced messages stay byte-identical to a no-checkpointer run (verified: a new
      thread accumulates nothing from a prior thread).
    """
    if checkpointer is None:
        if thread_id is not None or resume:
            raise ValueError(
                "thread_id/resume 需要一个 checkpointer 才有意义；"
                "当前未挂 checkpointer（请通过装配层注入，或不要传 thread_id/resume）。"
            )
        return None
    if resume:
        if not thread_id:
            raise ValueError(
                "续跑（resume）必须指定要续的 thread_id；"
                "请用 --thread-id <ID> 指出上次那次未完成的 run。"
            )
        return thread_id
    return thread_id or str(uuid.uuid4())


class CheckpointStore:
    """A ``SqliteSaver`` bound to an independent ``checkpoints.sqlite`` plus its lifecycle registry.

    Constructed once per CLI run at the assembly layer. Hand :attr:`saver` to
    ``create_agent(checkpointer=...)`` / ``StateGraph.compile(checkpointer=...)``; call
    :meth:`sweep` (bounded cleanup) and :meth:`record_thread` before the run; :meth:`close` after.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: float | None = None,
        max_threads: int | None = None,
    ) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the LangGraph loop commits checkpoints from worker threads; the
        # saver's own threading.Lock serialises those, and our registry writes happen only on the
        # main thread OUTSIDE invoke, so one shared connection is safe (same precedent as store.py).
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.saver = SqliteSaver(self._conn)
        self.saver.setup()  # idempotent CREATE TABLE checkpoints/writes
        self._conn.execute(_REGISTRY_DDL)
        self._conn.commit()
        self.ttl_seconds = ttl_seconds if ttl_seconds is not None else _env_ttl_seconds()
        self.max_threads = max_threads if max_threads is not None else _env_max_threads()

    def record_thread(self, thread_id: str, *, source: str = "") -> None:
        """Register (or touch) a thread so the lifecycle sweep can age it by wall-clock time."""
        now = _now_iso()
        self._conn.execute(
            "INSERT INTO checkpoint_threads (thread_id, created_at, last_touched_at, source) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(thread_id) DO UPDATE SET last_touched_at = excluded.last_touched_at",
            (thread_id, now, now, source),
        )
        self._conn.commit()

    def thread_exists(self, thread_id: str) -> bool:
        """True iff the saver holds a real checkpoint for ``thread_id`` (a resumable run exists)."""
        return self.saver.get_tuple({"configurable": {"thread_id": thread_id}}) is not None

    def delete_thread(self, thread_id: str) -> None:
        """Drop a thread's checkpoints/writes AND its registry row (full lifecycle removal)."""
        self.saver.delete_thread(thread_id)
        self._conn.execute("DELETE FROM checkpoint_threads WHERE thread_id = ?", (thread_id,))
        self._conn.commit()

    def sweep(self) -> int:
        """Bounded cleanup: drop TTL-expired threads, then trim to the newest-N cap. Returns count.

        Only registry-tracked threads are managed (every CLI-created thread is recorded here), so a
        thread written by direct API use without registration is left untouched — the sweep never
        deletes checkpoints it does not own the lifecycle for.
        """
        return self._sweep_expired() + self._sweep_over_cap()

    def list_threads(self) -> list[dict[str, str]]:
        """Registry rows newest-first (introspection / tests)."""
        cur = self._conn.execute(
            "SELECT thread_id, created_at, last_touched_at, source "
            "FROM checkpoint_threads ORDER BY last_touched_at DESC"
        )
        return [
            {"thread_id": r[0], "created_at": r[1], "last_touched_at": r[2], "source": r[3]}
            for r in cur.fetchall()
        ]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def _sweep_expired(self) -> int:
        if self.ttl_seconds <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(seconds=self.ttl_seconds)).isoformat()
        rows = self._conn.execute(
            "SELECT thread_id FROM checkpoint_threads WHERE last_touched_at < ?", (cutoff,)
        ).fetchall()
        for (thread_id,) in rows:
            self.delete_thread(thread_id)
        return len(rows)

    def _sweep_over_cap(self) -> int:
        if self.max_threads <= 0:
            return 0
        rows = self._conn.execute(
            "SELECT thread_id FROM checkpoint_threads ORDER BY last_touched_at DESC"
        ).fetchall()
        excess = rows[self.max_threads :]
        for (thread_id,) in excess:
            self.delete_thread(thread_id)
        return len(excess)


__all__ = ["CheckpointStore", "resolve_thread_id"]
