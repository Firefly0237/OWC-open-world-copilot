# T0 冒烟：标准假模型必须能确定性驱动 langchain create_agent 的 tool-calling 循环。
"""T0 smoke: the LangChain-standard scripted fake drives a real ``create_agent`` loop.

This is the load-bearing premise of the agent-normalization round: if a scripted
``GenericFakeChatModel`` can emit ``tool_calls`` that the real agent loop executes, the
bespoke offline text-ReAct providers are no longer needed as test drivers.
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain_core.tools import tool

from owcopilot.fakes import scripted_chat_model


@tool
def lookup_faction(name: str) -> str:
    """Look up a faction's standing by name."""
    return f"faction:{name} standing=hostile"


def _invoke(model, question: str):
    agent = create_agent(model, [lookup_faction])
    return agent.invoke({"messages": [("user", question)]})["messages"]


def test_scripted_tool_call_then_answer() -> None:
    model = scripted_chat_model(
        [
            ("lookup_faction", {"name": "Iron Pact"}),
            "Iron Pact is hostile.",
        ]
    )
    messages = _invoke(model, "What is the standing of Iron Pact?")

    tool_outputs = [m for m in messages if m.type == "tool"]
    assert len(tool_outputs) == 1
    assert "faction:Iron Pact standing=hostile" in str(tool_outputs[0].content)
    assert messages[-1].content == "Iron Pact is hostile."


def test_scripted_parallel_tool_calls() -> None:
    model = scripted_chat_model(
        [
            [("lookup_faction", {"name": "A"}), ("lookup_faction", {"name": "B"})],
            "both hostile",
        ]
    )
    messages = _invoke(model, "Check A and B.")

    tool_outputs = [str(m.content) for m in messages if m.type == "tool"]
    assert len(tool_outputs) == 2
    assert any("faction:A " in c for c in tool_outputs)
    assert any("faction:B " in c for c in tool_outputs)
    assert messages[-1].content == "both hostile"


def test_scripted_text_only_answer() -> None:
    model = scripted_chat_model(["plain answer, no tools"])
    messages = _invoke(model, "hi")

    assert not [m for m in messages if m.type == "tool"]
    assert messages[-1].content == "plain answer, no tools"


def test_deterministic_tool_call_ids() -> None:
    script = [("lookup_faction", {"name": "X"}), "done"]
    first = scripted_chat_model(script)
    second = scripted_chat_model(script)
    m1 = first.invoke("q")
    m2 = second.invoke("q")
    assert m1.tool_calls == m2.tool_calls
    assert m1.tool_calls[0]["id"] == "call_0"
