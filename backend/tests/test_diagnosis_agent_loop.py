"""Task40: the investigation loop is LangGraph's, and it stops at a hard cap.

A scripted chat model stands in for the real provider: it emits tool calls in a
fixed order, so the test observes the graph's actual ReAct behaviour without a
network call. The second test makes the model call tools forever; the call
budget and recursion limit must both hold.
"""

import json
from typing import Any, List

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from pydantic import Field

from backend.app.agent.harness import (
    ToolCallBudget,
    build_diagnosis_agent,
    run_agent_turn,
)


class ScriptedChatModel(BaseChatModel):
    """Return queued AIMessages; every extra call repeats the last one."""

    responses: List[AIMessage]
    index: int = 0
    seen: List[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-diagnosis"

    def bind_tools(self, tools, **kwargs):  # noqa: D401 - LangChain hook
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        position = min(self.index, len(self.responses) - 1)
        self.index += 1
        template = self.responses[position]
        # Re-emit a fresh message each time: reusing one AIMessage object gives
        # the graph an identical message/tool-call id, and the reducer treats
        # the second call as a duplicate instead of a new step.
        message = AIMessage(
            content=template.content,
            tool_calls=[
                {**call, "id": f"{call.get('id', 'call')}-{self.index}"}
                for call in template.tool_calls
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def _call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


@pytest.fixture
def executed() -> List[str]:
    return []


def _make_tool(executed: List[str]):
    @tool
    def lookup(term: str) -> str:
        """Look up one term and return a JSON observation."""
        executed.append(term)
        return json.dumps(
            {"term": term, "evidence": [{"ref": "f1.e1", "value": 42}]},
            ensure_ascii=False,
        )

    return lookup


async def test_agent_completes_several_tool_steps_without_a_handwritten_loop(executed):
    model = ScriptedChatModel(
        responses=[
            _call("lookup", {"term": "revenue"}, "call-1"),
            _call("lookup", {"term": "dishes"}, "call-2"),
            AIMessage(content="销售额下降主要来自单品销量下滑。"),
        ]
    )
    budget = ToolCallBudget(limit=6)
    agent = build_diagnosis_agent(
        model, [_make_tool(executed)], budget, checkpointer=MemorySaver()
    )

    turn = await run_agent_turn(
        agent, question="最近 7 天为什么下降？", thread_id="thread-1"
    )

    assert turn.error is None
    assert turn.answer == "销售额下降主要来自单品销量下滑。"
    assert executed == ["revenue", "dishes"]
    assert budget.used == 2
    assert budget.calls == ["lookup", "lookup"]
    assert budget.exhausted is False
    # Each model turn sees the growing message history: the tool results were
    # returned to the model by the graph, not by test-side bookkeeping.
    assert any(
        any(getattr(m, "type", "") == "tool" for m in seen) for seen in model.seen
    )


async def test_budget_and_recursion_limit_stop_a_model_that_never_concludes(executed):
    model = ScriptedChatModel(
        responses=[_call("lookup", {"term": "loop"}, "call-loop")]
    )
    budget = ToolCallBudget(limit=2)
    agent = build_diagnosis_agent(
        model, [_make_tool(executed)], budget, checkpointer=MemorySaver()
    )

    turn = await run_agent_turn(
        agent,
        question="一直查下去",
        thread_id="thread-2",
        recursion_limit=8,
    )

    assert turn.error == "recursion_limit"
    assert executed == ["loop", "loop"]
    assert budget.used == 2
    assert budget.exhausted is True
    assert budget.reason == "tool_call_limit"
