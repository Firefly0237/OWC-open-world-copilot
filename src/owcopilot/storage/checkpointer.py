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

import json
import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

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

_MODEL_BUDGET_DDL = """
CREATE TABLE IF NOT EXISTS checkpoint_model_budget (
    thread_id       TEXT PRIMARY KEY,
    contract_json   TEXT NOT NULL,
    used            INTEGER NOT NULL,
    by_kind_json    TEXT NOT NULL,
    updated_at      TEXT NOT NULL
)
"""


class CheckpointModelBudgetLedger:
    """Crash-durable pre-dispatch reservations beside a ``SqliteSaver`` checkpoint.

    LangGraph channel writes are committed at graph-node boundaries. Compression, generate and
    reflection calls can happen inside or outside those boundaries, so a process kill immediately
    after dispatch could otherwise lose their in-memory increment. This side ledger uses the same
    checkpoint SQLite connection and saver lock, committing a conservative reservation *before*
    every provider dispatch. On resume the channel count is reconciled with this durable maximum.
    """

    def __init__(self, saver: BaseCheckpointSaver[Any]) -> None:
        conn = getattr(saver, "conn", None)
        lock = getattr(saver, "lock", None)
        if not isinstance(conn, sqlite3.Connection) or lock is None:
            raise TypeError(
                "crash-durable model budgets require a SqliteSaver-compatible checkpointer"
            )
        self._conn = conn
        self._lock: threading.Lock = lock
        with self._lock:
            self._conn.execute(_MODEL_BUDGET_DDL)
            self._conn.commit()

    @staticmethod
    def _contract_json(contract: dict[str, object]) -> str:
        return json.dumps(contract, sort_keys=True, separators=(",", ":"))

    def initialise(
        self, thread_id: str, contract: dict[str, object], *, fresh: bool
    ) -> tuple[int, dict[str, int]]:
        encoded = self._contract_json(contract)
        with self._lock:
            if fresh:
                self._conn.execute(
                    "INSERT INTO checkpoint_model_budget "
                    "(thread_id, contract_json, used, by_kind_json, updated_at) "
                    "VALUES (?, ?, 0, '{}', ?) "
                    "ON CONFLICT(thread_id) DO UPDATE SET "
                    "contract_json=excluded.contract_json, used=0, by_kind_json='{}', "
                    "updated_at=excluded.updated_at",
                    (thread_id, encoded, _now_iso()),
                )
                self._conn.commit()
                return 0, {}
            row = self._conn.execute(
                "SELECT contract_json, used, by_kind_json FROM checkpoint_model_budget "
                "WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"checkpoint thread {thread_id!r} has no durable model-budget ledger"
                )
            if row[0] != encoded:
                raise ValueError("durable model-budget contract differs from checkpoint contract")
            decoded = json.loads(row[2])
            by_kind = {str(key): int(value) for key, value in decoded.items()}
            return int(row[1]), by_kind

    def reserve(
        self,
        thread_id: str,
        contract: dict[str, object],
        kind: str,
        *,
        total_limit: int,
        kind_limit: int | None = None,
    ) -> tuple[int, int]:
        encoded = self._contract_json(contract)
        with self._lock:
            row = self._conn.execute(
                "SELECT contract_json, used, by_kind_json FROM checkpoint_model_budget "
                "WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if row is None or row[0] != encoded:
                raise ValueError("durable model-budget ledger is missing or has a stale contract")
            used = int(row[1])
            by_kind_raw = json.loads(row[2])
            kind_used = int(by_kind_raw.get(kind, 0))
            if used >= total_limit:
                raise ValueError(f"total model-call budget exhausted ({used}/{total_limit})")
            if kind_limit is not None and kind_used >= kind_limit:
                raise ValueError(f"{kind} model-call budget exhausted ({kind_used}/{kind_limit})")
            used += 1
            kind_used += 1
            by_kind_raw[kind] = kind_used
            self._conn.execute(
                "UPDATE checkpoint_model_budget SET used = ?, by_kind_json = ?, updated_at = ? "
                "WHERE thread_id = ?",
                (
                    used,
                    json.dumps(by_kind_raw, sort_keys=True, separators=(",", ":")),
                    _now_iso(),
                    thread_id,
                ),
            )
            self._conn.commit()
            return used, kind_used

    def snapshot(self, thread_id: str) -> tuple[int, dict[str, int]] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT used, by_kind_json FROM checkpoint_model_budget WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        if row is None:
            return None
        return int(row[0]), {
            str(key): int(value) for key, value in json.loads(row[1]).items()
        }


def _now_iso() -> str:
    # UTC ISO-8601 with a fixed-width ``+00:00`` offset, so string ordering == chronological order
    # (what the TTL/cap sweeps rely on).
    return datetime.now(UTC).isoformat()


def _env_ttl_seconds() -> float:
    return float(os.getenv("OWCOPILOT_CHECKPOINT_TTL_DAYS", str(_DEFAULT_TTL_DAYS))) * 86400.0


def _env_max_threads() -> int:
    return int(os.getenv("OWCOPILOT_CHECKPOINT_MAX_THREADS", str(_DEFAULT_MAX_THREADS)))


def resolve_thread_id(
    checkpointer: BaseCheckpointSaver[Any] | None,
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
        self._conn.execute(_MODEL_BUDGET_DDL)
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
        self._conn.execute("DELETE FROM checkpoint_model_budget WHERE thread_id = ?", (thread_id,))
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


__all__ = ["CheckpointModelBudgetLedger", "CheckpointStore", "resolve_thread_id"]
