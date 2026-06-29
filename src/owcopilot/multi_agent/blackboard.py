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
"""

from __future__ import annotations

import sqlite3
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
        self._ensure_table()

    # ------------------------------------------------------------------
    # Schema bootstrap (idempotent — safe to call multiple times)
    # ------------------------------------------------------------------

    def _ensure_table(self) -> None:
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
        # Find the oldest pending task for this agent
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

        # Optimistic update: only succeeds if still pending (no TOCTOU gap in WAL mode)
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
        self._conn.execute(
            "UPDATE agent_messages SET status = ? WHERE id = ?",
            (status, msg_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_by_id(self, msg_id: str) -> AgentMessage | None:
        row = self._conn.execute(
            "SELECT * FROM agent_messages WHERE id = ?", (msg_id,)
        ).fetchone()
        if row is None:
            return None
        return AgentMessage.from_row(dict(row))
