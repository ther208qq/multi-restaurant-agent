"""Task44: the investigate endpoint, its scope rules and thread memory.

The chat model is scripted, so the graph really runs (system prompt, tool
binding, checkpointer) without a provider call. The diagnosis itself runs on
the isolated test database.
"""

import asyncio
from typing import Any, List

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver
from pydantic import Field

from backend.app.agent import diagnosis_investigation
from backend.app.agent.diagnosis_investigation import new_thread_id
from backend.app.main import app


class ScriptedChatModel(BaseChatModel):
    responses: List[AIMessage]
    index: int = 0
    seen: List[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-investigation"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        position = min(self.index, len(self.responses) - 1)
        self.index += 1
        return ChatResult(
            generations=[ChatGeneration(message=self.responses[position])]
        )


class SlowChatModel(ScriptedChatModel):
    """A provider slower than the turn deadline."""

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        await asyncio.sleep(0.5)
        self.index += 1
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="太慢了"))]
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


async def test_investigate_requires_staff_auth(async_client):
    response = await async_client.post(
        "/api/staff/diagnosis/investigate", json={"question": "最近怎么样？"}
    )
    assert response.status_code == 401


async def test_branch_staff_cannot_investigate_another_branch(async_client):
    token, _user = await _login(async_client, "downtown_staff", "staff123")
    response = await async_client.post(
        "/api/staff/diagnosis/investigate",
        json={"question": "最近怎么样？", "branch_id": 2},
        headers=_headers(token),
    )
    assert response.status_code == 403


async def test_admin_gets_a_full_investigation_response(
    async_client, monkeypatch, checkpointer
):
    model = ScriptedChatModel(
        responses=[AIMessage(content="最近 7 天经营平稳，未发现明显异常。")]
    )
    monkeypatch.setattr(
        diagnosis_investigation, "build_diagnosis_chat_model", lambda: model
    )
    token, user = await _login(async_client, "admin", "admin123")

    response = await async_client.post(
        "/api/staff/diagnosis/investigate",
        json={"question": "最近生意怎么样？", "branch_id": 1},
        headers=_headers(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["thread_id"].startswith(f"diag-{user['id']}-1-")
    assert body["branch_id"] == 1
    assert body["answer"] == "最近 7 天经营平稳，未发现明显异常。"
    assert body["status"] in {"ok", "insufficient_data"}
    assert body["resolved_window"]["start_date"]
    assert body["resolved_window"]["baseline_end_date"] < body["resolved_window"]["start_date"]
    assert body["diagnosis"] is not None
    assert body["report"]["facts"] or body["report"]["checks"]
    assert body["budget"]["tool_calls_limit"] == 6
    assert body["budget"]["tool_calls_used"] == 0
    assert body["budget"]["exhausted"] is False


async def test_a_slow_model_degrades_to_the_deterministic_answer(
    async_client, monkeypatch, checkpointer
):
    model = SlowChatModel(responses=[AIMessage(content="太慢了")])
    monkeypatch.setattr(
        diagnosis_investigation, "build_diagnosis_chat_model", lambda: model
    )
    monkeypatch.setattr(
        diagnosis_investigation.settings, "DIAGNOSIS_TURN_TIMEOUT_SECONDS", 0.05
    )
    token, _user = await _login(async_client, "admin", "admin123")

    response = await async_client.post(
        "/api/staff/diagnosis/investigate",
        json={"question": "最近生意怎么样？", "branch_id": 1},
        headers=_headers(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "llm_unavailable"
    assert body["answer"]
    assert body["diagnosis"] is not None
    assert any("0.05 秒" in note for note in body["notes"])
    assert body["budget"]["tool_calls_used"] == 0


async def test_same_thread_resumes_with_the_previous_conversation(
    async_client, monkeypatch, checkpointer
):
    model = ScriptedChatModel(
        responses=[
            AIMessage(content="第一轮：营业额下降。"),
            AIMessage(content="第二轮：原因是单品销量下滑。"),
        ]
    )
    monkeypatch.setattr(
        diagnosis_investigation, "build_diagnosis_chat_model", lambda: model
    )
    token, _user = await _login(async_client, "admin", "admin123")

    first = await async_client.post(
        "/api/staff/diagnosis/investigate",
        json={"question": "第一轮问题：最近生意怎么样？", "branch_id": 1},
        headers=_headers(token),
    )
    assert first.status_code == 200
    thread_id = first.json()["thread_id"]

    second = await async_client.post(
        "/api/staff/diagnosis/investigate",
        json={"question": "第二轮问题：那是什么原因？", "thread_id": thread_id},
        headers=_headers(token),
    )
    assert second.status_code == 200, second.text
    assert second.json()["thread_id"] == thread_id
    assert second.json()["answer"] == "第二轮：原因是单品销量下滑。"

    replayed = [str(message.content) for message in model.seen[-1]]
    assert any("第一轮问题" in content for content in replayed)
    assert any("第一轮：营业额下降" in content for content in replayed)


async def test_thread_ownership_and_shape_are_enforced(async_client, checkpointer):
    token, user = await _login(async_client, "admin", "admin123")
    foreign = new_thread_id(user["id"] + 1, 1)

    response = await async_client.post(
        "/api/staff/diagnosis/investigate",
        json={"question": "继续", "thread_id": foreign},
        headers=_headers(token),
    )
    assert response.status_code == 403

    malformed = await async_client.post(
        "/api/staff/diagnosis/investigate",
        json={"question": "继续", "thread_id": "not-a-thread"},
        headers=_headers(token),
    )
    assert malformed.status_code == 404


async def test_missing_model_degrades_to_the_deterministic_answer(
    async_client, monkeypatch
):
    monkeypatch.setattr(
        diagnosis_investigation, "build_diagnosis_chat_model", lambda: None
    )
    token, user = await _login(async_client, "admin", "admin123")

    response = await async_client.post(
        "/api/staff/diagnosis/investigate",
        json={"question": "最近生意怎么样？", "branch_id": 1},
        headers=_headers(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "llm_unavailable"
    assert body["answer"]
    assert body["budget"]["tool_calls_used"] == 0
    assert body["resolved_window"]["source"] == "default"
    assert body["diagnosis"]["branch_id"] == 1
    assert body["steps"][0]["tool"] == "get_diagnosis"
    assert body["thread_id"].startswith(f"diag-{user['id']}-1-")
