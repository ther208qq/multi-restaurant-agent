"""Task46: follow-up turns can switch the window without losing the thread.

The scripted model emits real tool calls, so the graph's tool loop runs against
the test database. That is what makes these tests meaningful: the second turn
chooses a different window, and the response, the trace and the baseline must
all move with it while the thread identity stays the same.
"""

from datetime import date, timedelta
from typing import Any, List

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver
from pydantic import Field

from backend.app.agent import diagnosis_investigation
from backend.app.agent.time_range import resolve_time_range
from backend.app.main import app


class ToolCallingChatModel(BaseChatModel):
    responses: List[AIMessage]
    index: int = 0
    seen: List[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-tool-calling-investigation"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        position = min(self.index, len(self.responses) - 1)
        self.index += 1
        return ChatResult(
            generations=[ChatGeneration(message=self.responses[position])]
        )


def _call(name: str, call_id: str, **args) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


FINAL = (
    '{"answer": "已按新窗口复查。", "facts": [], "inferences": [], '
    '"hypotheses": [], "checks": []}'
)


async def _login(client, username: str, password: str):
    response = await client.post(
        "/api/staff/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body["access_token"], body["user"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def checkpointer():
    saver = MemorySaver()
    app.state.checkpointer = saver
    try:
        yield saver
    finally:
        del app.state.checkpointer


async def _first_turn(client, token: str, question: str = "最近生意怎么样？"):
    response = await client.post(
        "/api/staff/diagnosis/investigate",
        json={"question": question, "branch_id": 1},
        headers=_headers(token),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _patch_model(monkeypatch, model):
    monkeypatch.setattr(
        diagnosis_investigation, "build_diagnosis_chat_model", lambda: model
    )


def _assert_adjacent_baseline(window: dict) -> None:
    start = date.fromisoformat(window["start_date"])
    end = date.fromisoformat(window["end_date"])
    span = (end - start).days + 1
    baseline_start = date.fromisoformat(window["baseline_start_date"])
    baseline_end = date.fromisoformat(window["baseline_end_date"])
    assert baseline_end == start - timedelta(days=1)
    assert (baseline_end - baseline_start).days + 1 == span


async def test_follow_up_switches_window_and_updates_the_trace(
    async_client, monkeypatch, checkpointer
):
    expected = resolve_time_range("上个月")
    model = ToolCallingChatModel(
        responses=[
            AIMessage(content="第一轮：默认最近 7 天。"),
            _call("resolve_time_range", "c1", expression="上个月"),
            _call(
                "get_diagnosis",
                "c2",
                start_date=str(expected["start_date"]),
                end_date=str(expected["end_date"]),
            ),
            AIMessage(content=FINAL),
        ]
    )
    _patch_model(monkeypatch, model)
    token, _user = await _login(async_client, "admin", "admin123")

    first = await _first_turn(async_client, token)
    thread_id = first["thread_id"]

    second = await async_client.post(
        "/api/staff/diagnosis/investigate",
        json={"question": "那上个月呢？", "thread_id": thread_id},
        headers=_headers(token),
    )

    assert second.status_code == 200, second.text
    body = second.json()
    assert body["thread_id"] == thread_id
    assert body["resolved_window"]["source"] == "question"
    assert body["resolved_window"]["start_date"] == str(expected["start_date"])
    assert body["resolved_window"]["end_date"] == str(expected["end_date"])
    _assert_adjacent_baseline(body["resolved_window"])

    # The second turn really investigated the new window.
    tools = [step["tool"] for step in body["steps"]]
    assert tools == ["resolve_time_range", "get_diagnosis"]
    diagnosis_step = body["steps"][1]
    assert diagnosis_step["arguments"]["start_date"] == str(expected["start_date"])
    # Task59: resolve_time_range is navigation and does not consume the budget.
    assert body["budget"]["tool_calls_used"] == 1

    # Memory: the model saw the first turn's question and answer.
    replayed = [str(message.content) for message in model.seen[-1]]
    assert any("最近生意怎么样" in content for content in replayed)
    assert any("第一轮" in content for content in replayed)


async def test_follow_up_weekend_switches_to_the_weekend_window(
    async_client, monkeypatch, checkpointer
):
    expected = resolve_time_range("那周末呢")
    model = ToolCallingChatModel(
        responses=[
            AIMessage(content="第一轮回答。"),
            _call("resolve_time_range", "c1", expression="那周末呢"),
            _call(
                "get_diagnosis",
                "c2",
                start_date=str(expected["start_date"]),
                end_date=str(expected["end_date"]),
            ),
            AIMessage(content=FINAL),
        ]
    )
    _patch_model(monkeypatch, model)
    token, _user = await _login(async_client, "admin", "admin123")

    first = await _first_turn(async_client, token)
    second = await async_client.post(
        "/api/staff/diagnosis/investigate",
        json={"question": "那周末呢？", "thread_id": first["thread_id"]},
        headers=_headers(token),
    )

    assert second.status_code == 200, second.text
    window = second.json()["resolved_window"]
    assert window["start_date"] == str(expected["start_date"])
    assert window["end_date"] == str(expected["end_date"])
    assert window["source"] == "question"
    _assert_adjacent_baseline(window)


async def test_follow_up_can_compare_peers_and_exposes_peer_evidence(
    async_client, monkeypatch, checkpointer
):
    expected = resolve_time_range("上周")
    model = ToolCallingChatModel(
        responses=[
            AIMessage(content="第一轮回答。"),
            _call("resolve_time_range", "c1", expression="上周"),
            _call(
                "compare_peers",
                "c2",
                start_date=str(expected["start_date"]),
                end_date=str(expected["end_date"]),
            ),
            AIMessage(content=FINAL),
        ]
    )
    _patch_model(monkeypatch, model)
    token, _user = await _login(async_client, "admin", "admin123")

    first = await _first_turn(async_client, token)
    second = await async_client.post(
        "/api/staff/diagnosis/investigate",
        json={"question": "和别的门店比呢？", "thread_id": first["thread_id"]},
        headers=_headers(token),
    )

    assert second.status_code == 200, second.text
    body = second.json()
    assert [step["tool"] for step in body["steps"]] == [
        "resolve_time_range",
        "compare_peers",
    ]
    assert body["resolved_window"]["start_date"] == str(expected["start_date"])
    assert any(item["ref"].startswith("peers") for item in body["evidence"])
    # The short ref stays for compatibility; the stable handle carries the turn,
    # so a later follow-up can cite this turn's peer comparison unambiguously.
    for item in body["evidence"]:
        assert item["thread_ref"] == f"t2.{item['ref']}"


async def test_a_mixed_window_is_rejected_and_the_retry_uses_the_resolved_one(
    async_client, monkeypatch, checkpointer
):
    expected = resolve_time_range("上周")
    model = ToolCallingChatModel(
        responses=[
            _call("resolve_time_range", "c1", expression="上周"),
            _call(
                "get_diagnosis",
                "c2",
                start_date="2020-01-01",
                end_date="2020-01-07",
            ),
            _call(
                "get_diagnosis",
                "c3",
                start_date=str(expected["start_date"]),
                end_date=str(expected["end_date"]),
            ),
            AIMessage(content=FINAL),
        ]
    )
    _patch_model(monkeypatch, model)
    token, _user = await _login(async_client, "admin", "admin123")

    response = await async_client.post(
        "/api/staff/diagnosis/investigate",
        json={"question": "上周生意怎么样？", "branch_id": 1},
        headers=_headers(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [step["tool"] for step in body["steps"]] == [
        "resolve_time_range",
        "get_diagnosis",
        "get_diagnosis",
    ]
    rejected, retried = body["steps"][1], body["steps"][2]
    assert rejected["status"] == "error"
    assert rejected["window"] == {
        "start_date": "2020-01-01",
        "end_date": "2020-01-07",
    }
    assert rejected["expected_window"] == {
        "start_date": str(expected["start_date"]),
        "end_date": str(expected["end_date"]),
    }
    assert retried["status"] == "ok"
    assert retried["window"] == rejected["expected_window"]
    assert body["resolved_window"]["start_date"] == str(expected["start_date"])
    assert body["resolved_window"]["end_date"] == str(expected["end_date"])


async def test_explicit_request_dates_win_over_the_question_expression(
    async_client, monkeypatch, checkpointer
):
    model = ToolCallingChatModel(
        responses=[
            _call("resolve_time_range", "c1", expression="上个月"),
            _call(
                "get_diagnosis",
                "c2",
                start_date="2026-09-01",
                end_date="2026-09-07",
            ),
            AIMessage(content=FINAL),
        ]
    )
    _patch_model(monkeypatch, model)
    token, _user = await _login(async_client, "admin", "admin123")

    response = await async_client.post(
        "/api/staff/diagnosis/investigate",
        json={
            "question": "看下上个月，但我已经选了日期",
            "branch_id": 1,
            "start_date": "2026-09-01",
            "end_date": "2026-09-07",
        },
        headers=_headers(token),
    )

    assert response.status_code == 200, response.text
    window = response.json()["resolved_window"]
    assert window["source"] == "explicit"
    assert window["start_date"] == "2026-09-01"
    assert window["end_date"] == "2026-09-07"
    _assert_adjacent_baseline(window)
    assert any("区间优先" in note for note in window["notes"])


async def test_a_thread_cannot_switch_branch(
    async_client, monkeypatch, checkpointer
):
    model = ToolCallingChatModel(responses=[AIMessage(content="第一轮回答。")])
    _patch_model(monkeypatch, model)
    token, _user = await _login(async_client, "admin", "admin123")

    first = await _first_turn(async_client, token)
    response = await async_client.post(
        "/api/staff/diagnosis/investigate",
        json={
            "question": "换一家店看看",
            "thread_id": first["thread_id"],
            "branch_id": 2,
        },
        headers=_headers(token),
    )

    assert response.status_code == 403


async def test_another_staff_member_cannot_continue_the_thread(
    async_client, monkeypatch, checkpointer
):
    model = ToolCallingChatModel(
        responses=[AIMessage(content="管理员的第一轮回答。")]
    )
    _patch_model(monkeypatch, model)
    admin_token, _admin = await _login(async_client, "admin", "admin123")
    first = await _first_turn(async_client, admin_token)

    staff_token, _staff = await _login(async_client, "downtown_staff", "staff123")
    response = await async_client.post(
        "/api/staff/diagnosis/investigate",
        json={"question": "我也看看", "thread_id": first["thread_id"]},
        headers=_headers(staff_token),
    )

    assert response.status_code == 403
