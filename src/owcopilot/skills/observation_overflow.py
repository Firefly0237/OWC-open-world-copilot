# 可恢复观测溢出：被上下文预算截断的观测尾部不销毁，全文按 obs_id 存入 run 级存储，
# read_observation 只读技能按需还原。存储经 ContextVar 出带（同 mcp_server._shared_project 先例），
# 保持技能签名干净，让 agent 把变量指向自己的 self._overflow。
"""Recoverable observation overflow — the store behind the ``read_observation`` skill.

When a tool result is too large to feed back into the prompt in full, the ReAct loop truncates it
for context budget but keeps the *complete* text in a run-scoped store keyed by an ``obs_id``. This
is Manus principle ③ (compression must be restorable): drop the bytes from the live context, but
never destroy them — the file/URL/reference is preserved so the tail can be restored on demand
(https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus). The
``read_observation`` skill is that restore path.

The truncation marker is written per Anthropic's "Writing effective tools for AI agents": a
truncated response must be *specific and actionable* and carry the technical identifier the agent
needs for the follow-up call — so the marker embeds the ``obs_id`` and names ``read_observation`` as
the exact retrieval call (https://www.anthropic.com/engineering/writing-tools-for-agents).

The store is passed out of band through a :class:`ContextVar` rather than a handler parameter — the
same pattern as :data:`owcopilot.mcp_server.tools._shared_project`. That keeps the
``read_observation`` signature clean (only the model-facing ``obs_id``) and lets the agent point the
var at its own ``self._overflow`` dict for the duration of a run. Callers are single-threaded and
sequential, so the var is visible to every dispatch in the run; the token is reset on exit so
nothing leaks across runs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from ..llm.tokenizer import count_tokens, truncate_to_token_budget
from . import SkillError

# The run-scoped overflow: obs_id -> the FULL untruncated observation text. Unset (None) outside an
# agent run, so ``read_observation`` gives a guided out-of-run error instead of crashing.
_overflow_store: ContextVar[dict[str, str] | None] = ContextVar(
    "owc_observation_overflow", default=None
)


@contextmanager
def bind_overflow(store: dict[str, str]) -> Iterator[None]:
    """Bind *store* as the active overflow for ``read_observation`` for the duration of the block.

    The agent passes its own ``self._overflow`` dict so the truncated observation tails written
    during the loop are readable by the ``read_observation`` skill dispatched inside that same loop.
    The token is reset on exit, so a later ``read_observation`` outside any run raises the guided
    out-of-run error rather than reading a stale store — no cross-run leak.
    """
    token = _overflow_store.set(store)
    try:
        yield
    finally:
        _overflow_store.reset(token)


def truncate_observation(
    store: dict[str, str],
    raw_dump: str,
    step_idx: int,
    *,
    token_limit: int,
    char_cap: int,
) -> tuple[str, bool]:
    """Truncate an oversized tool result for the prompt while keeping the full text recoverable.

    Returns ``(observation, truncated)``.

    The primary budget is measured in **tokens** (via :func:`count_tokens`, cl100k) so the cap
    matches the unit of the ReAct transcript budget: a CJK-heavy result is not under-counted the
    way a char cap would (4000 chars is ~1500 tokens of dense JSON but ~2000–4000 tokens of CJK).
    *char_cap* is a secondary ceiling that also catches token-cheap-but-char-huge dumps.

    - Within BOTH limits (``token_count <= token_limit`` and ``len(raw_dump) <= char_cap``): returns
      ``(raw_dump, False)`` unchanged — byte-identical to the pre-overflow path, with no store
      write, so an observation that fits is never marked or mutated.
    - Otherwise: cuts the head to *token_limit* tokens (canonical tiktoken truncation, then clamped
      to *char_cap* chars), writes the FULL ``raw_dump`` into *store* under a fresh, collision-free
      ``obs_id`` (``obs-{step_idx}-{seq}`` where ``seq`` is the number of tails stored so far), and
      returns a guided marker that names the exact ``read_observation`` call to restore the rest.
    """
    total_chars = len(raw_dump)
    total_tokens = count_tokens(raw_dump)
    if total_tokens <= token_limit and total_chars <= char_cap:
        return raw_dump, False
    head, kept = truncate_to_token_budget(raw_dump, token_limit)
    if len(head) > char_cap:
        head = head[:char_cap]
    obs_id = f"obs-{step_idx}-{len(store)}"
    store[obs_id] = raw_dump
    dropped_chars = total_chars - len(head)
    marker = (
        f"{head}… [truncated: kept first {kept} of {total_tokens} tokens "
        f"({dropped_chars} of {total_chars} chars dropped). "
        f'The full observation is stored; call read_observation(obs_id="{obs_id}") '
        f"to read it in full.]"
    )
    return marker, True


def read_observation(obs_id: str) -> dict[str, Any]:
    """Restore the full text of an observation that was truncated for context budget.

    Deterministic, $0, READ_ONLY: the returned ``content`` is the exact original tool output stored
    before truncation, so the ReAct invariant holds (the observation is real tool output, never
    model-authored). Errors are guided and actionable — they name the concrete next step (which ids
    are valid, or that the tool is only meaningful inside a run) rather than raising a raw crash.
    """
    store = _overflow_store.get()
    if store is None:
        raise SkillError(
            "read_observation is only available during an agent run "
            "(there is no truncated observation to restore outside one)."
        )
    if obs_id not in store:
        valid = ", ".join(sorted(store)) or "(none)"
        raise SkillError(f"unknown obs_id {obs_id!r}. Valid obs_id values from this run: {valid}.")
    content = store[obs_id]
    return {"obs_id": obs_id, "content": content, "chars": len(content)}
