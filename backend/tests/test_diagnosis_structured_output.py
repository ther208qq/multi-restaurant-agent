"""Task57: structured report output plus exactly one guardrail repair.

Two layers are exercised:

* the graph returns the report through ``create_agent(response_format=...)``,
  which ends on a structured ToolMessage rather than an AIMessage -- that must
  not be mistaken for a recursion-limit stop;
* when the guardrails drop a claim, the model is re-asked once with the precise
  reasons. A clean repair is used; anything else falls back to the
  deterministic summary, and forged refs/numbers never reach the response.
"""

import json
from datetime import date
from typing import Any, List

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from backend.app.agent import diagnosis_investigation
from backend.app.agent.diagnosis_investigation import (
    _compose_answer_and_report,
    _repair_if_needed,
)
from backend.app.agent.diagnosis_report import (
    STRUCTURED_REPORT_TOOL_NAME,
    AgentClaim,
    AgentInvestigationReport,
    report_to_dict,
)
from backend.app.agent.diagnosis_tools import DiagnosisToolContext
from backend.app.agent.harness import (
    RunTrace,
    ToolCallBudget,
    _memo_for_turn,
    run_harness_turn,
)
from backend.app.agent.time_range import resolve_explicit_range
from backend.app.main import app
from backend.app.models.user import StaffUser


class ScriptedChatModel(BaseChatModel):
    """Emit queued AIMessages in order; every extra call repeats the last one."""

    responses: List[AIMessage]
    index: int = 0
    seen: List[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-structured-output"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        position = min(self.index, len(self.responses) - 1)
        self.index += 1
        return ChatResult(
            generations=[ChatGeneration(message=self.responses[position])]
        )


class RepairModel:
    """A plain chat model for the repair call; records what it was asked."""

    def __init__(self, payloads: List[Any]) -> None:
        self.payloads = list(payloads)
        self.prompts: List[Any] = []

    async def ainvoke(self, messages):
        self.prompts.append(list(messages))
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return AIMessage(content=json.dumps(payload, ensure_ascii=False))


def _structured_call(**args) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": STRUCTURED_REPORT_TOOL_NAME,
                "args": args,
                "id": "report-1",
                "type": "tool_call",
            }
        ],
    )


def _diagnosis() -> dict:
    return {
        "branch_id": 1,
        "branch_name": "市中心店",
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


def _window() -> dict:
    return resolve_explicit_range(
        date(2026, 10, 10), date(2026, 10, 16), today=date(2026, 10, 17)
    )


def _context(trace: RunTrace) -> DiagnosisToolContext:
    return DiagnosisToolContext(
        staff=StaffUser(id=1, username="admin", role="admin", branch_id=None),
        session=object(),
        branch_id=1,
        trace=trace,
    )


def _compose(structured, *, trace=None, context=None):
    return _compose_answer_and_report(
        agent_answer="",
        structured=structured,
        diagnosis=_diagnosis(),
        context=context or _context(trace or RunTrace()),
        budget=ToolCallBudget(used=1),
        window=_window(),
        question="最近生意为什么下降？",
        trace=trace,
    )


async def _login(client, username: str, password: str):
    response = await client.post(
        "/api/staff/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def checkpointer():
    from langgraph.checkpoint.memory import MemorySaver

    saver = MemorySaver()
    app.state.checkpointer = saver
    try:
        yield saver
    finally:
        del app.state.checkpointer


def _patch_model(monkeypatch, model):
    monkeypatch.setattr(
        diagnosis_investigation, "build_diagnosis_chat_model", lambda: model
    )


# --------------------------------------------------------------------------- #
# The schema and the graph
# --------------------------------------------------------------------------- #
def test_report_schema_serializes_to_the_existing_wire_shape():
    report = AgentInvestigationReport(
        answer="营业额下降。",
        facts=[AgentClaim(text="本期营业额 4000 元。", evidence_refs=["f0.e0"])],
    )

    dumped = report_to_dict(report)

    assert set(dumped) == {"answer", "facts", "inferences", "hypotheses", "checks"}
    assert dumped["facts"][0] == {
        "text": "本期营业额 4000 元。",
        "evidence_refs": ["f0.e0"],
    }
    assert dumped["hypotheses"] == []


async def test_the_graph_returns_a_structured_response_not_a_step_limit():
    model = ScriptedChatModel(
        responses=[
            _structured_call(
                answer="本期营业额 4000 元。",
                facts=[
                    {
                        "text": "本期营业额 4000 元。",
                        "evidence_refs": ["f0.e0"],
                    }
                ],
            )
        ]
    )

    harness_turn = await run_harness_turn(
        tools=[],
        question="最近生意怎么样？",
        thread_id="structured-thread",
        model=model,
    )

    turn = harness_turn.turn
    assert turn.error is None, "the report ToolMessage is a normal completion"
    assert turn.structured["answer"] == "本期营业额 4000 元。"
    assert json.loads(turn.answer)["facts"][0]["evidence_refs"] == ["f0.e0"]


def test_a_historical_structured_answer_survives_the_context_memo():
    messages = [
        HumanMessage(content="第一轮问题"),
        _structured_call(
            answer="本期营业额 4000 元。",
            facts=[{"text": "本期营业额 4000 元。", "evidence_refs": ["f0.e0"]}],
        ),
        ToolMessage(
            content="Returning structured response: ...",
            tool_call_id="report-1",
            name=STRUCTURED_REPORT_TOOL_NAME,
        ),
    ]

    memo, answer = _memo_for_turn(messages, turn_index=1)

    assert answer == "本期营业额 4000 元。"
    assert "本轮回答摘要" in memo
    assert "f0.e0" in memo
    # The report tool is the answer, not an extra investigation step.
    assert f"{STRUCTURED_REPORT_TOOL_NAME}(" not in memo


# --------------------------------------------------------------------------- #
# One-shot repair
# --------------------------------------------------------------------------- #
def _forged_report() -> dict:
    return {
        "answer": "营业额下降，可能与客流有关。",
        "facts": [{"text": "本期营业额下降。", "evidence_refs": ["f9.e9"]}],
        "inferences": [
            {
                "text": "客流减少导致营业额下降。",
                "evidence_refs": ["f0.e0"],
            }
        ],
        "hypotheses": [],
        "checks": [],
    }


async def test_a_forged_ref_is_repaired_with_the_reasons_and_allowed_refs():
    trace = RunTrace()
    composed = _compose(_forged_report(), trace=trace)
    assert composed["report"]["dropped"] == 1
    assert any("f9.e9" in problem for problem in composed["problems"])

    model = RepairModel(
        [
            {
                "answer": "本期营业额 4000 元，上期 5000 元。",
                "facts": [
                    {
                        "text": "本期营业额 4000 元，上期 5000 元。",
                        "evidence_refs": ["f0.e0", "f0.e1"],
                    }
                ],
                "inferences": [
                    {
                        "text": "营业额下降与本期数据一致。",
                        "evidence_refs": ["f0.e0"],
                    }
                ],
                "hypotheses": [],
                "checks": [],
            }
        ]
    )

    repaired = await _repair_if_needed(
        composed=composed,
        diagnosis=_diagnosis(),
        model=model,
        question="最近生意为什么下降？",
        context=_context(trace),
        budget=ToolCallBudget(used=1),
        window=_window(),
        trace=trace,
        thread_evidence=[],
        turn_index=1,
    )

    assert repaired["report"]["dropped"] == 0
    assert repaired["report"]["source"] == "mixed"
    assert repaired["answer"] == "本期营业额 4000 元，上期 5000 元。"
    assert any("修正一次" in note for note in repaired["notes"])
    assert trace.repair == {
        "attempted": True,
        "succeeded": True,
        "residual": [],
        "reason": None,
    }
    prompt = model.prompts[0][-1].content
    assert "f9.e9" in prompt
    assert "f0.e0" in prompt
    assert "只能引用这些" in prompt


async def test_a_repaired_number_is_still_checked_and_falls_back():
    trace = RunTrace()
    composed = _compose(_forged_report(), trace=trace)
    model = RepairModel(
        [
            {
                "answer": "营业额下降了 88%。",
                "facts": [
                    {
                        "text": "营业额下降了 88%。",
                        "evidence_refs": ["f0.e0"],
                    }
                ],
                "inferences": [],
                "hypotheses": [],
                "checks": [],
            }
        ]
    )

    repaired = await _repair_if_needed(
        composed=composed,
        diagnosis=_diagnosis(),
        model=model,
        question="最近生意为什么下降？",
        context=_context(trace),
        budget=ToolCallBudget(used=1),
        window=_window(),
        trace=trace,
        thread_evidence=[],
        turn_index=1,
    )

    assert "88" not in repaired["answer"]
    assert "f9.e9" not in json.dumps(repaired["report"], ensure_ascii=False)
    assert any("确定性诊断摘要" in note for note in repaired["notes"])
    assert trace.repair["attempted"] is True
    assert trace.repair["succeeded"] is False
    assert trace.repair["reason"] == "residual_violations"
    assert "answer_repair_failed" in trace.degradation_reasons


async def test_an_unusable_repair_response_falls_back_to_the_deterministic_answer():
    trace = RunTrace()
    composed = _compose(_forged_report(), trace=trace)
    model = RepairModel([RuntimeError("provider down")])

    repaired = await _repair_if_needed(
        composed=composed,
        diagnosis=_diagnosis(),
        model=model,
        question="最近生意为什么下降？",
        context=_context(trace),
        budget=ToolCallBudget(used=1),
        window=_window(),
        trace=trace,
        thread_evidence=[],
        turn_index=1,
    )

    assert repaired["report"]["dropped"] == 1
    assert any("确定性诊断摘要" in note for note in repaired["notes"])
    assert trace.repair["reason"] == "repair_unusable"
    assert "answer_repair_failed" in trace.degradation_reasons


# --------------------------------------------------------------------------- #
# Endpoint: structured output is additive, repair happens once
# --------------------------------------------------------------------------- #
async def test_endpoint_uses_the_structured_answer_without_new_fields(
    async_client, monkeypatch, checkpointer
):
    model = ScriptedChatModel(
        responses=[
            _structured_call(
                answer="最近经营平稳，未发现明显异常。",
                facts=[],
                inferences=[],
                hypotheses=[],
                checks=[],
            )
        ]
    )
    _patch_model(monkeypatch, model)
    token = await _login(async_client, "admin", "admin123")

    response = await async_client.post(
        "/api/staff/diagnosis/investigate",
        json={"question": "最近生意怎么样？", "branch_id": 1},
        headers=_headers(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"] == "最近经营平稳，未发现明显异常。"
    assert body["report"]["facts"]
    assert set(body) == {
        "thread_id",
        "branch_id",
        "branch_name",
        "status",
        "answer",
        "resolved_window",
        "steps",
        "evidence",
        "hypotheses",
        "report",
        "diagnosis",
        "budget",
        "notes",
    }


async def test_endpoint_repairs_an_invented_number_once(
    async_client, monkeypatch, checkpointer
):
    model = ScriptedChatModel(
        responses=[
            _structured_call(answer="营业额下降了 88%。"),
            AIMessage(
                content=json.dumps(
                    {
                        "answer": "营业额有所下降，详见证据。",
                        "facts": [],
                        "inferences": [],
                        "hypotheses": [],
                        "checks": [],
                    },
                    ensure_ascii=False,
                )
            ),
        ]
    )
    _patch_model(monkeypatch, model)
    token = await _login(async_client, "admin", "admin123")

    response = await async_client.post(
        "/api/staff/diagnosis/investigate",
        json={"question": "最近生意怎么样？", "branch_id": 1},
        headers=_headers(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"] == "营业额有所下降，详见证据。"
    assert "88" not in json.dumps(body["report"], ensure_ascii=False)
    assert any("修正一次" in note for note in body["notes"])
    assert model.index == 2, "exactly one repair call"


async def test_endpoint_falls_back_when_repair_is_still_forged(
    async_client, monkeypatch, checkpointer
):
    forged = {
        "answer": "营业额下降了 88%。",
        "facts": [{"text": "营业额下降了 88%。", "evidence_refs": ["f9.e9"]}],
        "inferences": [],
        "hypotheses": [],
        "checks": [],
    }
    model = ScriptedChatModel(
        responses=[
            _structured_call(**forged),
            AIMessage(content=json.dumps(forged, ensure_ascii=False)),
        ]
    )
    _patch_model(monkeypatch, model)
    token = await _login(async_client, "admin", "admin123")

    response = await async_client.post(
        "/api/staff/diagnosis/investigate",
        json={"question": "最近生意怎么样？", "branch_id": 1},
        headers=_headers(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert "f9.e9" not in json.dumps(body, ensure_ascii=False)
    assert "88" not in body["answer"]
    assert any("确定性诊断摘要" in note for note in body["notes"])
    assert body["report"]["dropped"] >= 1
    assert model.index == 2, "the repair is attempted exactly once"
