"""Task52: the diagnosis harness baseline records behaviour without changing it.

These are contract tests for ``agent/harness.py`` and the instrumentation
points in ``guard_tools`` / ``_compose_answer_and_report``. The cases cover the
configuration defaults, the evidence registry contract, trace aggregation, and
the guarded tool outcomes (success, budget refusal, timeout, failure) plus the
free ``resolve_time_range`` budget exemption and slow-model deadline.
"""

import asyncio
import json
from datetime import date
from typing import Any, List

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import Field

from backend.app.agent.diagnosis_investigation import _compose_answer_and_report
from backend.app.agent.diagnosis_tools import DiagnosisToolContext
from backend.app.agent.harness import (
    CONTEXT_MEMO_HEADER,
    DEFAULT_RECURSION_LIMIT,
    DEFAULT_TOOL_CALLS_LIMIT,
    REF_TOKEN_PATTERN,
    HarnessConfig,
    RunTrace,
    ThreadEvidenceRegistry,
    ToolCallBudget,
    build_diagnosis_chat_model,
    collect_thread_evidence,
    compact_context,
    estimate_tokens,
    guard_tools,
    qualify_evidence_ref,
    run_harness_turn,
)
from backend.app.agent.time_range import resolve_explicit_range
from backend.app.models.user import StaffUser


class ScriptedChatModel(BaseChatModel):
    """Return queued AIMessages; every extra call repeats the last one."""

    responses: List[AIMessage]
    index: int = 0
    seen: List[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-harness"

    def bind_tools(self, tools, **kwargs):  # noqa: D401 - LangChain hook
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        position = min(self.index, len(self.responses) - 1)
        self.index += 1
        template = self.responses[position]
        message = AIMessage(
            content=template.content,
            tool_calls=[
                {**call, "id": f"{call.get('id', 'call')}-{self.index}"}
                for call in template.tool_calls
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


class SlowChatModel(ScriptedChatModel):
    """A provider that takes longer than the configured turn deadline."""

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        await asyncio.sleep(0.5)
        self.index += 1
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="太慢了"))]
        )


def test_default_config_matches_the_limits_the_agent_enforces_today():
    config = HarnessConfig()

    assert config.tool_calls_limit == DEFAULT_TOOL_CALLS_LIMIT
    assert config.recursion_limit == DEFAULT_RECURSION_LIMIT
    # Reserved limits are off until their tasks opt in: baseline behaviour is
    # unchanged when the default config is constructed.
    assert config.max_tool_output_chars is None
    assert config.context_token_budget is None
    assert config.context_char_budget is None
    assert config.tool_timeout_seconds is None
    assert config.turn_timeout_seconds is None


def test_config_rejects_impossible_limits():
    with pytest.raises(ValueError):
        HarnessConfig(tool_calls_limit=-1)
    with pytest.raises(ValueError):
        HarnessConfig(recursion_limit=0)
    with pytest.raises(ValueError):
        HarnessConfig(tool_timeout_seconds=0)


def test_estimate_tokens_is_deterministic_and_cjk_aware():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("故障") == 2
    payload = {"revenue": 4000, "label": "营业额"}
    assert estimate_tokens(payload) == estimate_tokens(payload)
    assert estimate_tokens(payload) > 0


class InMemoryEvidenceRegistry:
    """Test-local registry double.

    The harness keeps no production in-memory registry of its own -- the only
    one on the request path is ``ThreadEvidenceRegistry`` -- so the contract
    (unique refs, insertion order preserved) is pinned here instead.
    """

    def __init__(self, records=()):
        self._records = []
        self._index = {}
        for record in records:
            self.add(
                str(record["ref"]),
                str(record.get("label", "")),
                record.get("value"),
                str(record.get("detail", "")),
            )

    def add(self, ref, label, value=None, detail=""):
        if not ref:
            raise ValueError("evidence ref must not be empty.")
        if ref in self._index:
            raise ValueError(f"duplicate evidence ref: {ref}")
        record = {"ref": ref, "label": label, "value": value, "detail": detail}
        self._index[ref] = record
        self._records.append(record)
        return ref

    def get(self, ref):
        record = self._index.get(ref)
        return dict(record) if record is not None else None

    def refs(self):
        return set(self._index)

    def snapshot(self):
        return [dict(record) for record in self._records]

    def __len__(self):
        return len(self._records)


def test_evidence_registry_enforces_unique_refs_and_preserves_order():
    registry = InMemoryEvidenceRegistry()
    assert registry.add("f0.e0", "本期营业额", 4000.0, "元") == "f0.e0"
    registry.add("f0.e1", "上期营业额", 5000.0, "元")

    assert registry.get("f0.e0") == {
        "ref": "f0.e0",
        "label": "本期营业额",
        "value": 4000.0,
        "detail": "元",
    }
    assert registry.get("missing") is None
    assert registry.refs() == {"f0.e0", "f0.e1"}
    assert [item["ref"] for item in registry.snapshot()] == ["f0.e0", "f0.e1"]
    assert len(registry) == 2

    with pytest.raises(ValueError):
        registry.add("f0.e0", "重复")
    with pytest.raises(ValueError):
        registry.add("", "没有 ref")


def test_run_trace_aggregates_tool_usage_guardrails_and_degradation():
    trace = RunTrace(thread_id="diag-1-1-x", branch_id=1)
    trace.record_tool(
        name="get_diagnosis",
        arguments={"start_date": "2026-10-10", "end_date": "2026-10-16"},
        duration_ms=12.5,
        output={"status": "ok", "findings": []},
    )
    trace.record_tool(name="get_daily_series", status="error", error="boom")
    trace.record_tool(name="compare_peers", status="budget_exhausted", output="{}")
    trace.record_guardrails(
        dropped=2,
        source="rule",
        evidence_count=4,
        answer="受控摘要",
        violations=["未知引用"],
    )
    trace.record_degradation("answer_guardrail_violation")
    trace.record_degradation("answer_guardrail_violation")
    trace.record_budget({"tool_calls_used": 1, "tool_calls_limit": 6})
    trace.finish()

    summary = trace.summary()

    assert summary["tool_calls"] == 3
    assert summary["tool_calls_by_name"] == {
        "get_diagnosis": 1,
        "get_daily_series": 1,
        "compare_peers": 1,
    }
    assert summary["tool_failures"] == 1
    assert summary["tool_budget_refusals"] == 1
    assert summary["total_tool_ms"] == 12.5
    assert summary["total_output_chars"] > 0
    assert summary["estimated_output_tokens"] > 0
    assert summary["guardrail_dropped"] == 2
    assert summary["guardrail_source"] == "rule"
    assert summary["evidence_count"] == 4
    assert summary["degraded"] is True
    assert summary["degrade_reasons"] == ["answer_guardrail_violation"]
    assert summary["budget"]["tool_calls_used"] == 1
    assert summary["elapsed_ms"] is not None
    json.dumps(trace.as_dict(), ensure_ascii=False)


@tool
async def echo_tool(term: str) -> str:
    """Return the term unchanged."""
    return json.dumps({"term": term}, ensure_ascii=False)


@tool
async def failing_tool(term: str) -> str:
    """Always fail, so the guard records an error."""
    raise RuntimeError("service exploded")


@tool
async def slow_tool(term: str) -> str:
    """Take long enough for the opt-in tool timeout to fire."""
    await asyncio.sleep(0.2)
    return term


async def test_guard_tools_records_success_and_budget_refusal():
    trace = RunTrace()
    budget = ToolCallBudget(limit=1)
    guarded = guard_tools([echo_tool], budget, trace=trace)

    assert await guarded[0].ainvoke({"term": "revenue"}) == '{"term": "revenue"}'
    refusal = await guarded[0].ainvoke({"term": "dishes"})

    assert json.loads(refusal)["status"] == "budget_exhausted"
    assert [call.status for call in trace.tool_calls] == ["ok", "budget_exhausted"]
    assert trace.tool_calls[0].name == "echo_tool"
    assert trace.tool_calls[0].output_chars > 0
    assert trace.tool_calls[0].output_tokens > 0
    assert trace.tool_calls[0].duration_ms >= 0
    assert trace.tool_calls[1].output_chars > 0


async def test_guard_tools_records_failure_as_an_observation():
    trace = RunTrace()
    guarded = guard_tools([failing_tool], ToolCallBudget(limit=1), trace=trace)

    observation = json.loads(await guarded[0].ainvoke({"term": "x"}))

    assert observation["status"] == "error"
    assert observation["tool"] == "failing_tool"
    assert "RuntimeError" in observation["message"]
    assert "service exploded" in observation["message"]
    assert len(trace.tool_calls) == 1
    assert trace.tool_calls[0].status == "error"
    assert "RuntimeError" in trace.tool_calls[0].error
    assert trace.tool_calls[0].output_chars > 0
    assert "tool_failed" in trace.degradation_reasons


async def test_guard_tools_turns_an_opt_in_tool_timeout_into_an_observation():
    trace = RunTrace()
    guarded = guard_tools(
        [slow_tool],
        ToolCallBudget(limit=1),
        trace=trace,
        timeout_seconds=0.01,
    )

    observation = json.loads(await guarded[0].ainvoke({"term": "x"}))

    assert observation["status"] == "timeout"
    assert observation["tool"] == "slow_tool"
    assert "超时" in observation["message"]
    assert "尚未检查" in observation["message"]
    assert trace.tool_calls[0].status == "timeout"
    assert "TimeoutError" in trace.tool_calls[0].error
    assert "tool_timeout" in trace.degradation_reasons
    assert trace.summary()["tool_timeouts"] == 1
    assert trace.summary()["tool_failures"] == 0


@tool
async def resolve_time_range(question: str = "") -> str:
    """Pretend to resolve a relative time window without touching data."""
    return json.dumps(
        {"status": "ok", "start_date": "2026-10-10", "end_date": "2026-10-16"}
    )


async def test_resolve_time_range_does_not_consume_the_tool_budget():
    trace = RunTrace()
    budget = ToolCallBudget(limit=1)
    guarded = guard_tools([resolve_time_range, echo_tool], budget, trace=trace)

    first = json.loads(await guarded[0].ainvoke({"question": "上周"}))
    assert first["status"] == "ok"
    assert budget.used == 0
    assert not budget.exhausted

    assert await guarded[1].ainvoke({"term": "x"}) == '{"term": "x"}'
    assert budget.used == 1

    refused = json.loads(await guarded[1].ainvoke({"term": "y"}))
    assert refused["status"] == "budget_exhausted"
    assert budget.exhausted is True

    # Navigation still works after the data budget is exhausted.
    last = json.loads(await guarded[0].ainvoke({"question": "最近"}))
    assert last["status"] == "ok"
    # A refused call is traced but not recorded as an executed call.
    assert budget.calls == [
        "resolve_time_range",
        "echo_tool",
        "resolve_time_range",
    ]
    assert [call.status for call in trace.tool_calls] == [
        "ok",
        "ok",
        "budget_exhausted",
        "ok",
    ]


async def test_run_harness_turn_reports_a_missing_model_without_running_the_graph():
    result = await run_harness_turn(
        tools=[echo_tool],
        question="最近怎么样？",
        thread_id="thread-no-model",
        config=HarnessConfig(tool_calls_limit=3),
        model_factory=lambda: None,
    )

    assert result.model_available is False
    assert result.turn is None
    assert result.budget.limit == 3
    assert result.budget.used == 0


async def test_run_harness_turn_owns_agent_construction_and_budget():
    model = ScriptedChatModel(responses=[AIMessage(content="经营平稳。")])

    result = await run_harness_turn(
        tools=[echo_tool],
        question="最近怎么样？",
        thread_id="thread-with-model",
        config=HarnessConfig(tool_calls_limit=4),
        model=model,
    )

    assert result.model_available is True
    assert result.turn is not None
    assert result.turn.answer == "经营平稳。"
    assert result.budget.limit == 4
    assert result.budget.used == 0


async def test_run_harness_turn_times_out_a_slow_model():
    trace = RunTrace()
    model = SlowChatModel(responses=[AIMessage(content="太慢了")])

    with pytest.raises(asyncio.TimeoutError):
        await run_harness_turn(
            tools=[echo_tool],
            question="最近怎么样？",
            thread_id="thread-slow-model",
            config=HarnessConfig(turn_timeout_seconds=0.05),
            trace=trace,
            model=model,
        )

    assert "turn_timeout" in trace.degradation_reasons


async def test_a_slow_tool_degrades_in_turn_and_the_agent_still_answers():
    trace = RunTrace()
    model = ScriptedChatModel(
        responses=[
            _call_tool("slow_tool", {"term": "revenue"}, "call-slow"),
            AIMessage(content="工具超时，基于已有信息回答。"),
        ]
    )

    result = await run_harness_turn(
        tools=[slow_tool],
        question="最近怎么样？",
        thread_id="thread-slow-tool",
        config=HarnessConfig(tool_timeout_seconds=0.01),
        trace=trace,
        model=model,
    )

    assert result.turn is not None
    assert result.turn.answer == "工具超时，基于已有信息回答。"
    assert result.budget.used == 1
    assert trace.tool_calls[0].status == "timeout"
    assert "tool_timeout" in trace.degradation_reasons
    # The model really saw a timeout observation and continued the turn.
    tool_messages = [
        message for message in model.seen[-1] if isinstance(message, ToolMessage)
    ]
    assert tool_messages
    assert json.loads(tool_messages[-1].content)["status"] == "timeout"


@tool
async def history_tool(term: str) -> str:
    """Return one evidence item plus a bulky, turn-specific payload."""
    return json.dumps(
        {
            "status": "ok",
            "term": term,
            "bulk": f"{term}-" + "x" * 400,
            "evidence": [
                {
                    "ref": f"{term}.e1",
                    "label": "营业额",
                    "value": 4000.0,
                    "detail": "元",
                }
            ],
        },
        ensure_ascii=False,
    )


def _call_tool(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


async def test_context_middleware_memoizes_prior_turns_and_keeps_current_results_raw():
    from langgraph.checkpoint.memory import MemorySaver

    saver = MemorySaver()
    thread_id = "thread-context-memo"
    first_model = ScriptedChatModel(
        responses=[
            _call_tool("history_tool", {"term": "f0"}, "first"),
            AIMessage(content="第一轮结论：营业额 4000 元（f0.e1）。"),
        ]
    )
    first = await run_harness_turn(
        tools=[history_tool],
        question="第一轮问题",
        thread_id=thread_id,
        checkpointer=saver,
        model=first_model,
    )
    assert first.turn is not None

    second_model = ScriptedChatModel(
        responses=[
            _call_tool("history_tool", {"term": "daily1"}, "second"),
            AIMessage(content="第二轮结论。"),
        ]
    )
    trace = RunTrace(thread_id=thread_id, branch_id=1)
    second = await run_harness_turn(
        tools=[history_tool],
        question="第二轮问题",
        thread_id=thread_id,
        checkpointer=saver,
        trace=trace,
        model=second_model,
    )

    assert second.turn is not None
    assert second.turn.answer == "第二轮结论。"
    replayed = second_model.seen[-1]
    joined = "\n".join(str(message.content) for message in replayed)

    # Earlier turns are memoized: their raw bulk payload is gone, but the
    # evidence ref and its number, the question and the final answer remain.
    assert "f0-" + "x" * 400 not in joined
    assert CONTEXT_MEMO_HEADER in joined
    assert "f0.e1" in joined
    assert "4000.0" in joined
    assert "第一轮问题" in joined
    assert "第一轮结论" in joined

    # The current turn still sees its own raw tool result.
    current_tools = [
        str(message.content)
        for message in replayed
        if isinstance(message, ToolMessage)
    ]
    assert any("daily1-" + "x" * 400 in content for content in current_tools)

    # The checkpoint keeps the lossless history; only the model view changed.
    saved = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    raw_messages = saved.checkpoint["channel_values"]["messages"]
    raw_text = "\n".join(str(getattr(item, "content", "")) for item in raw_messages)
    assert "f0-" + "x" * 400 in raw_text
    assert "daily1-" + "x" * 400 in raw_text

    assert trace.context is not None
    assert trace.context["memoized_turns"] >= 1
    assert trace.summary()["context"] == trace.context


def test_thread_registry_qualifies_refs_and_keeps_legacy_aliases_safe():
    registry = ThreadEvidenceRegistry(
        [
            {
                "ref": "t1.f0.e0",
                "local_ref": "f0.e0",
                "turn": 1,
                "label": "第一期营业额",
                "value": 1000.0,
            },
            {
                "ref": "t2.f0.e0",
                "local_ref": "f0.e0",
                "turn": 2,
                "label": "第二期营业额",
                "value": 2000.0,
            },
            {
                "ref": "t1.daily1.e1",
                "local_ref": "daily1.e1",
                "turn": 1,
                "label": "日营业额",
                "value": 300.0,
            },
        ],
        current_turn=2,
    )

    assert qualify_evidence_ref("f0.e0", 3) == "t3.f0.e0"
    assert qualify_evidence_ref("t1.f0.e0", 3) == "t1.f0.e0"
    assert registry.refs() == {"t1.f0.e0", "t2.f0.e0", "t1.daily1.e1"}
    # A bare ref produced by both turns means the current turn when one exists.
    assert registry.get("f0.e0")["ref"] == "t2.f0.e0"
    # A bare ref produced by exactly one older turn keeps resolving.
    assert registry.get("daily1.e1")["ref"] == "t1.daily1.e1"
    assert registry.get("t1.f0.e0")["value"] == 1000.0
    # Fabricated refs stay unknown, qualified or not.
    assert registry.get("f9.e9") is None
    assert registry.get("t1.f9.e9") is None
    assert registry.get("t9.f0.e0") is None
    assert REF_TOKEN_PATTERN.findall(
        "见 t1.f0.e0 与 t2.daily1.e1，不是 t9.f9.e9。"
    ) == ["t1.f0.e0", "t2.daily1.e1", "t9.f9.e9"]


async def test_thread_evidence_is_persisted_and_history_refs_are_turn_qualified():
    from langgraph.checkpoint.memory import MemorySaver

    saver = MemorySaver()
    thread_id = "thread-thread-evidence"
    first_model = ScriptedChatModel(
        responses=[
            _call_tool("history_tool", {"term": "f0"}, "first"),
            AIMessage(content="第一轮结论：营业额 4000 元（f0.e1）。"),
        ]
    )
    first = await run_harness_turn(
        tools=[history_tool],
        question="第一轮问题",
        thread_id=thread_id,
        checkpointer=saver,
        model=first_model,
    )

    assert first.turn is not None
    assert first.turn.turn_index == 1
    first_refs = {record["ref"] for record in first.turn.thread_evidence}
    assert first_refs == {"t1.f0.e1"}
    assert first.turn.thread_evidence[0]["local_ref"] == "f0.e1"

    second_model = ScriptedChatModel(
        responses=[
            _call_tool("history_tool", {"term": "daily1"}, "second"),
            AIMessage(content="第二轮结论。"),
        ]
    )
    trace = RunTrace(thread_id=thread_id, branch_id=1)
    second = await run_harness_turn(
        tools=[history_tool],
        question="第二轮问题",
        thread_id=thread_id,
        checkpointer=saver,
        trace=trace,
        model=second_model,
    )

    assert second.turn is not None
    assert second.turn.turn_index == 2
    refs = {record["ref"] for record in second.turn.thread_evidence}
    assert {"t1.f0.e1", "t2.daily1.e1"} <= refs

    # The model view now qualifies the earlier ref, so it can cite it later.
    joined = "\n".join(str(message.content) for message in second_model.seen[-1])
    assert "t1.f0.e1" in joined
    assert "第一轮结论：营业额 4000 元（t1.f0.e1）" in joined
    # The current turn still uses the short ref its own tool emitted.
    assert "t2.daily1.e1" not in "\n".join(
        str(message.content)
        for message in second_model.seen[-1]
        if isinstance(message, ToolMessage)
    )

    # The registry lives in the checkpoint, not only in this process.
    saved = await saver.aget_tuple(
        {"configurable": {"thread_id": thread_id}}
    )
    persisted = saved.checkpoint["channel_values"]["thread_evidence"]
    persisted_refs = {record["ref"] for record in persisted}
    assert {"t1.f0.e1", "t2.daily1.e1"} <= persisted_refs

    # Messages remain lossless; the qualified registry is an additive channel.
    raw_messages = saved.checkpoint["channel_values"]["messages"]
    raw_text = "\n".join(str(getattr(item, "content", "")) for item in raw_messages)
    assert "f0-" + "x" * 400 in raw_text
    assert "daily1-" + "x" * 400 in raw_text


async def test_checkpoint_written_before_the_registry_channel_still_loads():
    from langchain.agents import create_agent
    from langgraph.checkpoint.memory import MemorySaver

    saver = MemorySaver()
    thread_id = "thread-legacy-evidence"
    old_model = ScriptedChatModel(
        responses=[
            _call_tool("history_tool", {"term": "f0"}, "legacy"),
            AIMessage(content="旧轮回答（f0.e1）。"),
        ]
    )
    legacy_agent = create_agent(
        model=old_model,
        tools=[history_tool],
        checkpointer=saver,
        name="legacy_investigator",
    )
    await legacy_agent.ainvoke(
        {"messages": [("user", "旧轮问题")]},
        config={"configurable": {"thread_id": thread_id}},
    )

    saved = await saver.aget_tuple(
        {"configurable": {"thread_id": thread_id}}
    )
    # The pre-Task55 checkpoint has messages only: no registry channel.
    assert "thread_evidence" not in saved.checkpoint["channel_values"]

    second_model = ScriptedChatModel(responses=[AIMessage(content="新轮回答。")])
    second = await run_harness_turn(
        tools=[history_tool],
        question="新轮问题",
        thread_id=thread_id,
        checkpointer=saver,
        model=second_model,
    )

    assert second.turn is not None
    assert second.turn.turn_index == 2
    refs = {record["ref"] for record in second.turn.thread_evidence}
    assert refs == {"t1.f0.e1"}
    joined = "\n".join(str(message.content) for message in second_model.seen[-1])
    assert "t1.f0.e1" in joined

    # The next checkpoint does have the channel; no migration step was needed.
    saved = await saver.aget_tuple(
        {"configurable": {"thread_id": thread_id}}
    )
    persisted = saved.checkpoint["channel_values"]["thread_evidence"]
    assert {record["ref"] for record in persisted} == {"t1.f0.e1"}


def test_collect_thread_evidence_rebuilds_turns_from_messages():
    messages = [
        HumanMessage(content="第一轮"),
        ToolMessage(
            content=json.dumps(
                {
                    "evidence": [
                        {"ref": "daily1.e1", "label": "日营业额", "value": 300.0}
                    ]
                }
            ),
            tool_call_id="c1",
        ),
        AIMessage(content="第一轮回答"),
        HumanMessage(content="第二轮"),
        ToolMessage(
            content=json.dumps(
                {
                    "evidence": [
                        {"ref": "daily1.e1", "label": "日营业额", "value": 500.0}
                    ]
                }
            ),
            tool_call_id="c2",
        ),
    ]

    records = collect_thread_evidence(messages)

    assert [record["ref"] for record in records] == [
        "t1.daily1.e1",
        "t2.daily1.e1",
    ]
    assert [record["value"] for record in records] == [300.0, 500.0]


def _budget_turn(index: int) -> list:
    ref = f"f{index}.e1"
    output = json.dumps(
        {
            "status": "ok",
            "revenue": 1000.0 * index,
            "evidence": [{"ref": ref, "label": "营业额", "value": 1000.0 * index}],
        },
        ensure_ascii=False,
    )
    return [
        HumanMessage(content=f"问题{index}"),
        _call_tool("get_diagnosis", {"turn": index}, f"c{index}"),
        ToolMessage(content=output, tool_call_id=f"c{index}", name="get_diagnosis"),
        AIMessage(content=f"回答{index}"),
    ]


def test_context_token_budget_drops_the_oldest_turns_first():
    messages = (
        _budget_turn(1)
        + _budget_turn(2)
        + _budget_turn(3)
        + [HumanMessage(content="当前问题")]
    )

    view, stats = compact_context(messages, token_budget=80)
    text = "\n".join(str(message.content) for message in view)

    assert stats["prior_turns"] == 3
    assert stats["dropped_turns"] >= 1
    assert stats["dropped_turns"] < 3
    assert "f1.e1" not in text
    assert "f3.e1" in text
    assert "更早的" in text
    assert "context_older_turns_dropped" in stats["reasons"]
    assert "当前问题" in text


def test_context_char_budget_compacts_current_tool_output_without_losing_refs():
    output = json.dumps(
        {
            "status": "ok",
            "bulk": "y" * 2000,
            "evidence": [
                {"ref": "daily1.e1", "label": "营业额", "value": 300.0, "detail": "元"}
            ],
        },
        ensure_ascii=False,
    )
    messages = [
        HumanMessage(content="当前问题"),
        _call_tool("get_daily_series", {}, "c1"),
        ToolMessage(content=output, tool_call_id="c1", name="get_daily_series"),
    ]

    view, stats = compact_context(messages, char_budget=200)
    tool_view = next(
        str(message.content) for message in view if isinstance(message, ToolMessage)
    )

    assert "y" * 100 not in tool_view
    assert "daily1.e1" in tool_view
    assert "300.0" in tool_view
    assert stats["truncated_tools"] == 1
    assert "context_tool_outputs_trimmed" in stats["reasons"]


def test_ollama_model_gets_the_configured_num_ctx(monkeypatch):
    import langchain_ollama

    from backend.app.agent import harness

    captured = {}

    class FakeChatOllama:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_ollama, "ChatOllama", FakeChatOllama)
    monkeypatch.setattr(harness.settings, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(harness.settings, "OLLAMA_NUM_CTX", 4096)

    build_diagnosis_chat_model()

    assert captured["num_ctx"] == 4096


def _diagnosis() -> dict:
    return {
        "branch_id": 1,
        "branch_name": "测试门店",
        "status": "ok",
        "notes": [],
        "current_period": {
            "start_date": date(2026, 10, 10),
            "end_date": date(2026, 10, 16),
            "order_count": 80,
            "cancelled_count": 4,
            "revenue": 4000.0,
            "average_order_value": 50.0,
            "units_sold": 160,
            "active_days": 7,
        },
        "baseline_period": {
            "start_date": date(2026, 10, 3),
            "end_date": date(2026, 10, 9),
            "order_count": 100,
            "cancelled_count": 2,
            "revenue": 5000.0,
            "average_order_value": 50.0,
            "units_sold": 200,
            "active_days": 7,
        },
        "findings": [
            {
                "metric": "revenue",
                "label": "营业额",
                "direction": "down",
                "severity": "warning",
                "current": 4000.0,
                "previous": 5000.0,
                "change_pct": -20.0,
                "menu_item_id": None,
                "evidence": [
                    {"ref": "f0.e0", "label": "本期营业额", "value": 4000.0, "detail": ""},
                    {"ref": "f0.e1", "label": "上期营业额", "value": 5000.0, "detail": ""},
                ],
            }
        ],
    }


def test_compose_records_guardrail_drops_on_the_trace():
    trace = RunTrace()
    context = DiagnosisToolContext(
        staff=StaffUser(id=1, username="admin", role="admin", branch_id=None),
        session=object(),
        branch_id=1,
        trace=trace,
    )
    window = resolve_explicit_range(
        date(2026, 10, 10), date(2026, 10, 16), today=date(2026, 10, 17)
    )
    answer = json.dumps(
        {
            "answer": "营业额下降了 88%。",
            "facts": [{"text": "猜的", "evidence_refs": ["f9.e9"]}],
        },
        ensure_ascii=False,
    )

    result = _compose_answer_and_report(
        agent_answer=answer,
        diagnosis=_diagnosis(),
        context=context,
        budget=ToolCallBudget(used=1),
        window=window,
        question="最近生意为什么下降？",
        trace=trace,
    )

    assert "88" not in result["answer"]
    assert trace.guardrail_dropped == 1
    assert trace.guardrail_source == "rule"
    assert trace.guardrail_violations
    assert trace.evidence_count == 2
    assert trace.answer_chars == len(result["answer"])
    assert "all_claims_rejected" in trace.degradation_reasons
    assert "answer_guardrail_violation" in trace.degradation_reasons
