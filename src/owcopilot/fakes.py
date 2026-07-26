# 离线测试替身，集中放一处。
"""Offline test doubles, collected in one place.

``MockProvider`` is the deterministic, dependency-free stand-in for a model: it echoes the prompt
so tests can exercise the gateway (routing, caching, telemetry) offline at $0. It is kept out of
``llm/gateway.py`` (real implementations only) and re-exported from there for compatibility.

``scripted_chat_model`` is the LangChain-standard equivalent for the agent stack: it wraps
``langchain_core``'s ``GenericFakeChatModel`` so a test can script a tool-calling conversation
(which tools get called, with which args, and the final answer) and drive the real agent loop
deterministically. Tests inject it at the model boundary; the production path stays single and
real (no fake provider is ever routed in the product).

Note: ``HashingEmbedder`` is **not** a test double. It is the real default ``SemanticCache``
embedder, so it lives in ``llm/cache.py``. Feature-specific offline providers (QA, drafts,
world-seed, …) live next to their feature in that package's ``offline.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage


class MockProvider:
    """Deterministic, offline provider for tests. Token counts approximated as len/4."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        text = f"[mock:{model}] " + (user[:60] if user else "")
        in_tok = max(1, (len(system) + len(user)) // 4)
        out_tok = max(1, len(text) // 4)
        return text, in_tok, out_tok


# One scripted model turn: a final text answer, a single tool call, or several
# parallel tool calls emitted in the same assistant message.
ToolCall = tuple[str, dict[str, Any]]
ScriptTurn = str | ToolCall | list[ToolCall]


class _ToolBindingFakeChatModel(GenericFakeChatModel):
    """``GenericFakeChatModel`` that accepts ``bind_tools``.

    The scripted fake decides its own tool calls, so binding is a no-op; the override only
    exists so ``create_agent`` (which always calls ``bind_tools``) works on every
    ``langchain_core`` version, including those where the base fake raises NotImplementedError.
    """

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        return self


def scripted_chat_model(script: Sequence[ScriptTurn]) -> GenericFakeChatModel:
    """Build a LangChain fake chat model that replays ``script``, one turn per model call.

    Each turn becomes one assistant message: a ``str`` is a plain final answer;
    ``(tool_name, args)`` is a single tool call; a list of such tuples is parallel tool calls.
    Tool-call ids are deterministic (``call_0``, ``call_1``, …) so transcripts are repeatable.
    """
    messages: list[AIMessage] = []
    call_seq = 0
    for turn in script:
        if isinstance(turn, str):
            messages.append(AIMessage(content=turn))
            continue
        calls = [turn] if isinstance(turn, tuple) else list(turn)
        tool_calls = []
        for name, args in calls:
            tool_calls.append(
                {"name": name, "args": args, "id": f"call_{call_seq}", "type": "tool_call"}
            )
            call_seq += 1
        messages.append(AIMessage(content="", tool_calls=tool_calls))
    return _ToolBindingFakeChatModel(messages=iter(messages))


__all__ = ["MockProvider", "ScriptTurn", "ToolCall", "scripted_chat_model"]
