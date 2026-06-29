"""AgentMessage protocol — the immutable envelope that flows through the SQLite blackboard.

Every message has a stable ``id``, carries ``from_agent``/``to_agent`` routing fields, and holds a
``payload`` that is frozen on first write. The ``status`` field is the ONLY mutable column.

Design rationale (matches SUPERVISOR_rubric P3-3, P3-4, P3-6):
- Explicit ``from_agent`` / ``to_agent`` → every blackboard row is attributed to a specific agent.
- ``payload`` carries both ``original_goal`` and ``subtask`` in task_assign messages (P3-4).
- Immutability of payload (enforced at blackboard layer) → no post-hoc revision by workers.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

# The closed set of message types in the orchestrator-worker protocol.
MsgType = Literal[
    "task_assign",   # orchestrator → worker: execute a subtask
    "task_result",   # worker → blackboard: subtask output
    "verify_request",  # orchestrator → verifier: validate a task_result
    "verify_result",   # verifier → blackboard: pass/fail verdict
    "synthesize",    # orchestrator: final report after all workers + verifier done
]


@dataclass
class AgentMessage:
    """Immutable-payload message that moves between agents via the SQLite blackboard.

    ``payload`` must be a JSON-serialisable dict; it is serialised on write and never mutated
    after insertion (the blackboard enforces this — only ``status`` is updated in-place).
    """

    session_id: str          # ties all messages in one multi-agent run together
    from_agent: str          # agent_id of the sender
    to_agent: str            # agent_id of the target, or "broadcast"
    msg_type: MsgType
    payload: dict[str, Any]  # structured content; frozen after first write
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    status: str = "pending"  # pending → claimed → done | failed

    def payload_json(self) -> str:
        """Serialise payload for storage."""
        return json.dumps(self.payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AgentMessage:
        """Reconstruct from a SQLite row dict."""
        return cls(
            id=row["id"],
            session_id=row["session_id"],
            from_agent=row["from_agent"],
            to_agent=row["to_agent"],
            msg_type=row["msg_type"],
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
            status=row["status"],
        )


# ---------------------------------------------------------------------------
# Typed payload helpers — used to build and unpack payloads with confidence.
# These are plain dataclasses, not part of the AgentMessage schema; the dict
# representation is what gets stored.
# ---------------------------------------------------------------------------


@dataclass
class TaskAssignPayload:
    """Payload for a task_assign message (orchestrator → worker).

    Both ``original_goal`` AND ``subtask`` are required (SUPERVISOR_rubric P3-4:
    "Worker prompt must contain overall_goal and sub_task"). Injecting the
    original goal prevents cascade hallucination where a worker only sees its
    narrow subtask and forgets global constraints.
    """

    original_goal: str         # the high-level goal passed to the orchestrator
    subtask: str               # this worker's specific job
    allowed_skills: list[str]  # tool whitelist for minimal attack surface
    max_steps: int = 4

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_goal": self.original_goal,
            "subtask": self.subtask,
            "allowed_skills": self.allowed_skills,
            "max_steps": self.max_steps,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaskAssignPayload:
        return cls(
            original_goal=d["original_goal"],
            subtask=d["subtask"],
            allowed_skills=d.get("allowed_skills", []),
            max_steps=d.get("max_steps", 4),
        )


@dataclass
class TaskResultPayload:
    """Payload for a task_result message (worker → blackboard)."""

    task_msg_id: str       # references the task_assign this answers
    worker_role: str       # "diagnosis" | "repair_proposal" (for human readability)
    final_answer: str      # agent's Final Answer text
    # The worker's STRUCTURED claim of how many open errors it found, taken from the real
    # integer in its own ``audit_project`` tool-call result (NOT a substring count of its
    # prose).  ``None`` means the worker made no verifiable audit-backed claim (it never
    # invoked ``audit_project``) — an honest "no claim" the verifier must not read as 0.
    open_errors: int | None
    stop_reason: str       # "finished" | "max_steps"
    step_count: int        # number of ReAct steps taken

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_msg_id": self.task_msg_id,
            "worker_role": self.worker_role,
            "final_answer": self.final_answer,
            "open_errors": self.open_errors,
            "stop_reason": self.stop_reason,
            "step_count": self.step_count,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaskResultPayload:
        return cls(
            task_msg_id=d["task_msg_id"],
            worker_role=d["worker_role"],
            final_answer=d["final_answer"],
            # Default to None ("no claim"), NOT 0 — a missing field is not a claim of zero.
            open_errors=d.get("open_errors"),
            stop_reason=d.get("stop_reason", "finished"),
            step_count=d.get("step_count", 0),
        )


@dataclass
class VerifyResultPayload:
    """Payload for a verify_result message (verifier → blackboard)."""

    target_msg_id: str              # the task_result message id that was verified
    verdict: Literal["pass", "fail", "needs_more"]
    rationale: str                  # human-readable explanation
    open_errors_verified: int       # what the verifier's independent audit found
    # What the worker asserted (for delta analysis).  ``None`` means the worker made no
    # verifiable error-count claim — the verifier reports ground truth without a delta.
    worker_claimed_errors: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_msg_id": self.target_msg_id,
            "verdict": self.verdict,
            "rationale": self.rationale,
            "open_errors_verified": self.open_errors_verified,
            "worker_claimed_errors": self.worker_claimed_errors,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VerifyResultPayload:
        return cls(
            target_msg_id=d["target_msg_id"],
            verdict=d["verdict"],
            rationale=d["rationale"],
            open_errors_verified=d.get("open_errors_verified", 0),
            worker_claimed_errors=d.get("worker_claimed_errors"),
        )
