# SQLite 黑板：append-only 消息板，多 agent 间唯一共享状态（可复盘、无隐藏内存态）。
"""SQLite-backed append-only blackboard for the multi-agent system.

Architecture rationale (SUPERVISOR_rubric P3-3):
    "Must have an explicit communication channel — SQLite blackboard (shared table +
     append-only write), not function return values in the same call stack."

This class wraps the ``agent_messages`` table added to SQLiteStore.  The key invariant is:
- ``post_message`` only INSERTs — payload is immutable after write.
- ``claim_task`` uses a conditional UPDATE that only succeeds if status='pending', preventing
  double-claiming when two workers race (optimistic locking, $0 atomicity from SQLite's WAL).
- ``update_status`` only touches the ``status`` column — payload stays frozen.

No business logic lives here.  Routing, decomposition, and synthesis belong to the agents.

Thread-safety (parallel stage dispatch):
    The parallel fan-out waves in :class:`~owcopilot.multi_agent.session.MultiAgentSession` drain
    ONE shared connection from a pool of consumer threads.  Although SQLite is compiled SERIALIZED
    (``threadsafety==3``), the Python ``sqlite3`` module's *implicit transaction* management is not
    safe for genuinely concurrent use of a single connection object (concurrent
    ``execute``/``commit`` race the connection's transaction state — "cannot start a transaction
    within a transaction").  A ``threading.RLock`` therefore serialises each RAW statement here.
    It is deliberately FINE-GRAINED: in ``claim_task`` the SELECT and the conditional UPDATE are
    two SEPARATE locked sections, so the cross-thread TOCTOU window between them stays open and the
    optimistic ``UPDATE ... WHERE status='pending'`` remains the genuinely contended claim arbiter
    (its ``rowcount==0`` loss branch is reachable).  This is NOT a coarse board-serialising lock
    (which would make SELECT+UPDATE atomic and the optimistic lock un-contendable) — it only makes
    the shared connection safe for the Python driver.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

from .messages import AgentMessage, MsgType


class AgentBlackboard:
    """Read/write interface to the append-only ``agent_messages`` table.

    Constructed with a sqlite3.Connection so it can share the connection used by
    SQLiteStore (same WAL file, no extra file handles) or accept an in-memory
    connection for tests.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        # Serialises each raw statement so a pool of consumer threads may share this one
        # connection (see the module docstring: the Python sqlite3 transaction manager is not
        # concurrency-safe). Fine-grained by design — it does NOT wrap the claim decision.
        self._lock = threading.RLock()
        self._ensure_table()

    # ------------------------------------------------------------------
    # Schema bootstrap (idempotent — safe to call multiple times)
    # ------------------------------------------------------------------

    def _ensure_table(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
            CREATE TABLE IF NOT EXISTS agent_messages (
                id           TEXT PRIMARY KEY,
                session_id   TEXT NOT NULL,
                from_agent   TEXT NOT NULL,
                to_agent     TEXT NOT NULL,
                msg_type     TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending'
            );

            CREATE INDEX IF NOT EXISTS idx_agent_messages_session
                ON agent_messages(session_id, to_agent, status);

            CREATE INDEX IF NOT EXISTS idx_agent_messages_type
                ON agent_messages(session_id, msg_type);

            CREATE INDEX IF NOT EXISTS idx_agent_messages_created
                ON agent_messages(created_at);
            """
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Write path — payload is immutable after this call
    # ------------------------------------------------------------------

    def post_message(self, msg: AgentMessage) -> str:
        """Insert a new message.  Returns ``msg.id``.  Never updates an existing row."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO agent_messages
                    (id, session_id, from_agent, to_agent, msg_type, payload_json,
                     created_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg.id,
                    msg.session_id,
                    msg.from_agent,
                    msg.to_agent,
                    msg.msg_type,
                    msg.payload_json(),
                    msg.created_at,
                    msg.status,
                ),
            )
            self._conn.commit()
        return msg.id

    # ------------------------------------------------------------------
    # Claim path — optimistic locking prevents double-claim
    # ------------------------------------------------------------------

    def claim_task(self, agent_id: str, session_id: str) -> AgentMessage | None:
        """Atomically claim the oldest pending task_assign addressed to ``agent_id``.

        Uses a conditional UPDATE: only rows with status='pending' and to_agent=agent_id
        are eligible.  Returns the claimed message, or None if no eligible task exists.
        This prevents two workers that share the same agent_id from both picking up the
        same task (though in practice each worker has a unique agent_id).
        """
        # Find the oldest pending task for this agent (locked section 1 — raw SELECT only).
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id FROM agent_messages
                WHERE session_id = ? AND to_agent = ? AND msg_type = 'task_assign'
                  AND status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (session_id, agent_id),
            ).fetchone()

        if row is None:
            return None

        msg_id = str(row["id"])

        # Optimistic update (locked section 2 — SEPARATE from the SELECT above, so the cross-thread
        # TOCTOU window stays open): only succeeds if still pending. Two consumers that both read
        # this row as pending race here; exactly one UPDATE flips it and the other sees rowcount 0.
        with self._lock:
            changed = self._conn.execute(
                """
                UPDATE agent_messages
                SET status = 'claimed'
                WHERE id = ? AND status = 'pending'
                """,
                (msg_id,),
            ).rowcount
            self._conn.commit()

        if changed == 0:
            return None  # someone else claimed it first

        return self._get_by_id(msg_id)

    def claim_assignment(self, msg_id: str) -> AgentMessage | None:
        """Claim ONE specific ``task_assign`` by id — the exactly-once idempotent gate for Send.

        Unlike :meth:`claim_task` (which selects the OLDEST pending message for a mailbox and is the
        genuinely-contended arbiter the concurrency primitive tests pin down), this claims a
        SPECIFIC assignment id that a LangGraph ``Send`` routed straight to a worker node. The
        conditional ``UPDATE ... WHERE id=? AND status='pending'`` is atomic on its own, so it needs
        no split TOCTOU window: it flips the row exactly once. A re-dispatched or recovered ``Send``
        for the same id finds the row already ``claimed`` / ``done`` and gets ``None`` — the gate
        that stops one assignment from being executed twice. The ``msg_type='task_assign'`` guard
        keeps a stray id (a result / verify row) from ever being claimed as work.
        """
        with self._lock:
            changed = self._conn.execute(
                """
                UPDATE agent_messages
                SET status = 'claimed'
                WHERE id = ? AND status = 'pending' AND msg_type = 'task_assign'
                """,
                (msg_id,),
            ).rowcount
            self._conn.commit()

        if changed == 0:
            return None  # already claimed/done, or not a pending task_assign (idempotent gate)

        return self._get_by_id(msg_id)

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    def read_messages(
        self,
        session_id: str,
        *,
        msg_type: MsgType | None = None,
        from_agent: str | None = None,
        to_agent: str | None = None,
        status: str | None = None,
    ) -> list[AgentMessage]:
        """Return messages for a session, optionally filtered."""
        clauses: list[str] = ["session_id = ?"]
        values: list[Any] = [session_id]
        if msg_type is not None:
            clauses.append("msg_type = ?")
            values.append(msg_type)
        if from_agent is not None:
            clauses.append("from_agent = ?")
            values.append(from_agent)
        if to_agent is not None:
            clauses.append("to_agent = ?")
            values.append(to_agent)
        if status is not None:
            clauses.append("status = ?")
            values.append(status)
        where = " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM agent_messages WHERE {where} ORDER BY created_at ASC",  # noqa: S608
                values,
            ).fetchall()
        return [AgentMessage.from_row(dict(row)) for row in rows]

    def get_message(self, msg_id: str) -> AgentMessage | None:
        """Fetch a single message by id."""
        return self._get_by_id(msg_id)

    def session_flow(self, session_id: str) -> list[AgentMessage]:
        """All messages for a session in creation order — useful for debugging / demo."""
        return self.read_messages(session_id)

    # ------------------------------------------------------------------
    # Status update — only field that may change after insert
    # ------------------------------------------------------------------

    def update_status(self, msg_id: str, status: str) -> None:
        """Mark a message done or failed.  Payload is never touched."""
        allowed = {"claimed", "done", "failed"}
        if status not in allowed:
            raise ValueError(f"status must be one of {allowed}, got {status!r}")
        with self._lock:
            self._conn.execute(
                "UPDATE agent_messages SET status = ? WHERE id = ?",
                (status, msg_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_by_id(self, msg_id: str) -> AgentMessage | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agent_messages WHERE id = ?", (msg_id,)
            ).fetchone()
        if row is None:
            return None
        return AgentMessage.from_row(dict(row))
