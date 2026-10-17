"""Task48: four golden investigation scenarios with a scripted model.

The deterministic diagnosis is the ground truth; the scripted model supplies the
tool choices and the final JSON, exactly as a provider would. Each scenario
therefore exercises the whole path -- request -> LangGraph ReAct loop -> read-only
tools -> guardrails -> response -- without a network call:

1. **Dish collapse** (``get_diagnosis`` -> ``get_dish_daily``): the answer may
   cite the deterministic ``fN.eM`` ref and the tool's ``dishN.<id>.eM`` ref, and
   must not keep anything else.
2. **Cancellation rise** (``get_diagnosis`` -> ``get_operational_changes``): the
   cancellation split is evidence; a claim citing it survives.
3. **Own store down, peers flat** (``get_diagnosis`` -> ``compare_peers``): the
   peer summary ref is citable and the claim is kept.
4. **Insufficient data**: the response says so and stays rule-based.

A fifth boundary is asserted throughout: every kept ref resolves to an evidence
item, every dropped claim stays dropped, and no prose survives with an invented
number or an unobserved cause.
"""

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, List
from uuid import uuid4

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver
from pydantic import Field
from sqlalchemy import update

from backend.app.agent import diagnosis_investigation
from backend.app.database import transactional_session
from backend.app.main import app
from backend.app.models.audit import AuditLog
from backend.app.models.branch import Branch
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.order import Order
from backend.app.models.user import Customer, StaffUser
from backend.app.repo.reservation import ReservationRepo
from backend.app.service.diagnostics import DiagnosisService, resolve_window
from backend.app.service.order import OrderService

_END = datetime.now(timezone.utc).date() - timedelta(days=1)
_START = _END - timedelta(days=3)
_BASE_START, _BASE_END = resolve_window(_START, _END)[2:]
# The endpoint injects a real staff identity; the pre-computation below needs
# one too, otherwise the cancellation split is reported as "unknown caller" and
# carries no counts. A detached admin is enough: refs do not depend on the id.
_ADMIN = StaffUser(username="__golden_admin", role="admin", branch_id=None)


class ScenarioModel(BaseChatModel):
    """Emit the queued AIMessages in order; each call gets a fresh message id."""

    responses: List[AIMessage]
    index: int = 0
    seen: List[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-golden-scenario"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        position = min(self.index, len(self.responses) - 1)
        self.index += 1
        template = self.responses[position]
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=template.content,
                        tool_calls=[
                            {**call, "id": f"{call.get('id', 'call')}-{self.index}"}
                            for call in template.tool_calls
                        ],
                    )
                )
            ]
        )


def _call(name: str, call_id: str, **args) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def _final(**sections) -> AIMessage:
    """A provider-style final JSON answer, with safe defaults for every bucket."""
    payload = {
        "answer": sections.pop("answer", "已按证据给出结论。"),
        "facts": sections.pop("facts", []),
        "inferences": sections.pop("inferences", []),
        "hypotheses": sections.pop("hypotheses", []),
        "checks": sections.pop("checks", []),
    }
    return AIMessage(content=json.dumps(payload, ensure_ascii=False))


async def _login(client):
    response = await client.post(
        "/api/staff/login", json={"username": "admin", "password": "admin123"}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


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


async def _make_branch() -> int:
    async with transactional_session() as s:
        branch = Branch(
            name=f"__golden{uuid4().hex[:8]}",
            address="x",
            phone="x",
            opening_hours="10:00 - 23:59",
            is_active=True,
        )
        s.add(branch)
        await s.flush()
        return branch.id


async def _make_customer() -> int:
    async with transactional_session() as s:
        customer = Customer(
            name="__golden",
            email=f"__golden_{uuid4().hex}@example.com",
            password_hash="x",
        )
        s.add(customer)
        await s.flush()
        return customer.id


async def _make_item(branch_id: int, name: str = "__golden_item") -> int:
    async with transactional_session() as s:
        category = MenuCategory(branch_id=branch_id, name=f"__golden{uuid4().hex[:8]}")
        s.add(category)
        await s.flush()
        item = MenuItem(
            category_id=category.id,
            name=name,
            base_price=10.0,
            is_available=True,
        )
        s.add(item)
        await s.flush()
        s.add(
            MenuItemStock(
                menu_item_id=item.id, branch_id=branch_id, stock_quantity=10000
            )
        )
        await s.flush()
        return item.id


async def _place(branch_id, customer_id, item_id, *, day: date) -> int:
    order = await OrderService.place_order(
        customer_id,
        branch_id,
        "dine_in",
        [{"menu_item_id": item_id, "quantity": 1}],
    )
    async with transactional_session() as s:
        await s.execute(
            update(Order)
            .where(Order.id == order.id)
            .values(created_at=datetime.combine(day, time(12, 0), tzinfo=timezone.utc))
        )
    return order.id


async def _seed(branch_id, customer_id, item_id, start, end, *, per_day):
    day = start
    while day <= end:
        for _ in range(per_day):
            await _place(branch_id, customer_id, item_id, day=day)
        day += timedelta(days=1)


async def _book(branch_id, customer_id, *, start_time, status="confirmed"):
    """Insert a historical booking directly; service rules reject past slots."""
    async with transactional_session() as s:
        reservation = await ReservationRepo.create_reservation(
            s,
            customer_id=customer_id,
            branch_id=branch_id,
            table_id=None,
            party_size=2,
            start_time=start_time,
            end_time=start_time + timedelta(minutes=90),
            duration_minutes=90,
            seating_area_preference="indoor",
            status=status,
            notes=None,
        )
        return reservation.id


def _window_payload(branch_id: int, start: date = _START, end: date = _END) -> dict:
    return {
        "question": "请调查这个区间发生了什么",
        "branch_id": branch_id,
        "start_date": str(start),
        "end_date": str(end),
    }


async def _investigate(client, token, model, monkeypatch, payload):
    monkeypatch.setattr(
        diagnosis_investigation, "build_diagnosis_chat_model", lambda: model
    )
    return await client.post(
        "/api/staff/diagnosis/investigate", json=payload, headers=_headers(token)
    )


def _assert_claims_are_grounded(report: dict, evidence: list) -> None:
    refs = {item["ref"] for item in evidence}
    for section in ("facts", "inferences", "checks"):
        for claim in report[section]:
            assert claim["evidence_refs"], claim
            assert set(claim["evidence_refs"]) <= refs, claim


def _finding(diagnosis: dict, metric: str, menu_item_id=None) -> dict:
    for finding in diagnosis["findings"]:
        if finding["metric"] == metric and finding.get("menu_item_id") == menu_item_id:
            return finding
    raise AssertionError(f"missing {metric} finding: {diagnosis['findings']}")


# --------------------------------------------------------------------------- #
# 1. Dish collapse
# --------------------------------------------------------------------------- #
async def test_dish_collapse_cites_both_diagnosis_and_dish_tool_evidence(
    async_client, monkeypatch, checkpointer
):
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    flagship = await _make_item(branch_id, "招牌菜")
    ordinary = await _make_item(branch_id, "常规菜")

    await _seed(branch_id, customer_id, flagship, _BASE_START, _BASE_END, per_day=2)
    await _seed(branch_id, customer_id, ordinary, _BASE_START, _BASE_END, per_day=2)
    await _seed(branch_id, customer_id, flagship, _START, _END, per_day=1)
    await _seed(branch_id, customer_id, ordinary, _START, _END, per_day=2)

    diagnosis = await DiagnosisService.diagnose(branch_id, _START, _END)
    dish = _finding(diagnosis, "dish_sales", flagship)
    diagnosis_ref = dish["evidence"][0]["ref"]
    dish_tool_ref = f"dish1.{flagship}.e3"

    model = ScenarioModel(
        responses=[
            _call(
                "get_diagnosis",
                "c1",
                start_date=str(_START),
                end_date=str(_END),
            ),
            _call(
                "get_dish_daily",
                "c2",
                menu_item_ids=[flagship],
                start_date=str(_START),
                end_date=str(_END),
            ),
            _final(
                answer="招牌菜销量大幅下降，是本次营业额回落的主要原因。",
                facts=[
                    {
                        "text": "招牌菜本期销量明显下降",
                        "evidence_refs": [diagnosis_ref],
                    }
                ],
                inferences=[
                    {
                        "text": "变化集中在招牌菜，其他菜品销量保持稳定",
                        "evidence_refs": [dish_tool_ref],
                    }
                ],
                checks=[
                    {
                        "text": "核对招牌菜在本期每日的实际出单情况",
                        "evidence_refs": [dish_tool_ref],
                    }
                ],
            ),
        ]
    )
    token = await _login(async_client)

    response = await _investigate(
        async_client, token, model, monkeypatch, _window_payload(branch_id)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [step["tool"] for step in body["steps"]] == [
        "get_diagnosis",
        "get_dish_daily",
    ]
    assert body["budget"]["tool_calls_used"] == 2
    assert body["report"]["dropped"] == 0
    assert body["report"]["source"] == "mixed"
    assert body["hypotheses"] == []
    _assert_claims_are_grounded(body["report"], body["evidence"])
    assert any(item["ref"].startswith("dish") for item in body["evidence"])
    assert any(
        dish_tool_ref in claim["evidence_refs"]
        for claim in body["report"]["inferences"]
    )


# --------------------------------------------------------------------------- #
# 2. Cancellation rise
# --------------------------------------------------------------------------- #
async def test_cancellation_rise_is_answered_from_the_cancellation_split(
    async_client, monkeypatch, checkpointer
):
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id)

    await _seed(branch_id, customer_id, item_id, _BASE_START, _BASE_END, per_day=2)
    current_orders = []
    day = _START
    while day <= _END:
        current_orders.append(await _place(branch_id, customer_id, item_id, day=day))
        current_orders.append(await _place(branch_id, customer_id, item_id, day=day))
        current_orders.append(await _place(branch_id, customer_id, item_id, day=day))
        day += timedelta(days=1)
    for order_id in current_orders[:4]:
        await OrderService.cancel_order(order_id, customer_id)
    # Cancellation attribution is keyed on when the audit entry was written, not
    # when the order was placed. The cancellations just happened "now", outside a
    # window ending yesterday, so move those entries into the judged window.
    async with transactional_session() as s:
        await s.execute(
            update(AuditLog)
            .where(
                AuditLog.action == "order.status_changed",
                AuditLog.entity_id.in_(current_orders[:4]),
            )
            .values(
                created_at=datetime.combine(
                    _END, time(12, 30), tzinfo=timezone.utc
                )
            )
        )

    diagnosis = await DiagnosisService.diagnose(
        branch_id, _START, _END, staff=_ADMIN
    )
    cancellation = _finding(diagnosis, "cancelled_rate")
    split_ref = next(
        item["ref"] for item in cancellation["evidence"] if isinstance(item["value"], dict)
    )

    model = ScenarioModel(
        responses=[
            _call(
                "get_diagnosis",
                "c1",
                start_date=str(_START),
                end_date=str(_END),
            ),
            _call(
                "get_operational_changes",
                "c2",
                start_date=str(_START),
                end_date=str(_END),
            ),
            _final(
                answer="取消率有所上升，需要区分顾客取消与运营原因。",
                facts=[
                    {"text": "本期取消率上升", "evidence_refs": [cancellation["evidence"][0]["ref"]]}
                ],
                inferences=[
                    {
                        "text": "取消中包含顾客主动发起的部分，需要区分需求侧与运营侧",
                        "evidence_refs": [split_ref],
                    }
                ],
                checks=[
                    {
                        "text": "核对窗口内是否有门店侧运营调整",
                        "evidence_refs": [split_ref],
                    }
                ],
            ),
        ]
    )
    token = await _login(async_client)

    response = await _investigate(
        async_client, token, model, monkeypatch, _window_payload(branch_id)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [step["tool"] for step in body["steps"]] == [
        "get_diagnosis",
        "get_operational_changes",
    ]
    assert body["report"]["dropped"] == 0
    assert body["report"]["inferences"][0]["evidence_refs"] == [split_ref]
    _assert_claims_are_grounded(body["report"], body["evidence"])
    # Rule causes add the cancellation side split alongside the model's claim.
    assert body["report"]["inferences"]


# --------------------------------------------------------------------------- #
# 3. Own store down, peers flat
# --------------------------------------------------------------------------- #
async def test_own_store_drop_with_flat_peers_uses_the_comparison_evidence(
    async_client, monkeypatch, checkpointer
):
    customer_id = await _make_customer()

    # An isolated window in the past: every other branch created by the suite has
    # data only around the shared _START/_END, so here the target and the two
    # seeded peers are the only comparable stores -- "peers flat" is true by
    # construction rather than an accident of test order.
    peer_end = date(2025, 6, 4)
    peer_start = peer_end - timedelta(days=3)
    peer_base_start, peer_base_end = resolve_window(peer_start, peer_end)[2:]

    target = await _make_branch()
    target_item = await _make_item(target)
    await _seed(target, customer_id, target_item, peer_base_start, peer_base_end, per_day=3)
    await _seed(target, customer_id, target_item, peer_start, peer_end, per_day=1)

    peer_ids = []
    for _ in range(2):
        peer = await _make_branch()
        peer_ids.append(peer)
        peer_item = await _make_item(peer)
        await _seed(peer, customer_id, peer_item, peer_base_start, peer_base_end, per_day=2)
        await _seed(peer, customer_id, peer_item, peer_start, peer_end, per_day=2)

    # Pin the premise before asserting the conclusion: the target really is down
    # and the seeded peers really are flat in this window. Check each peer
    # directly instead of through ``compare_branch_peers``: that service caps its
    # result at MAX_PEER_BRANCHES, and in a full-suite run the earlier tests
    # already own the slots, so the tool's selection is not the premise we mean
    # to assert here.
    target_diagnosis = await DiagnosisService.diagnose(target, peer_start, peer_end)
    assert any(
        finding["metric"] == "revenue" and finding["direction"] == "down"
        for finding in target_diagnosis["findings"]
    )
    for peer in peer_ids:
        peer_diagnosis = await DiagnosisService.diagnose(peer, peer_start, peer_end)
        assert peer_diagnosis["status"] == "ok"
        assert not any(
            finding["metric"] == "revenue" and finding["direction"] == "down"
            for finding in peer_diagnosis["findings"]
        )

    model = ScenarioModel(
        responses=[
            _call(
                "get_diagnosis",
                "c1",
                start_date=str(peer_start),
                end_date=str(peer_end),
            ),
            _call(
                "compare_peers",
                "c2",
                start_date=str(peer_start),
                end_date=str(peer_end),
            ),
            _final(
                answer="其他可比门店没有同步下降，本店变化更可能是店内问题。",
                inferences=[
                    {
                        "text": "其他可比门店没有同步下降，本店下降不是全店共性问题",
                        "evidence_refs": ["peers1.s1"],
                    }
                ],
                checks=[
                    {
                        "text": "核对本店窗口内的运营调整与菜品可售情况",
                        "evidence_refs": ["peers1.s1"],
                    }
                ],
            ),
        ]
    )
    token = await _login(async_client)

    response = await _investigate(
        async_client,
        token,
        model,
        monkeypatch,
        _window_payload(target, peer_start, peer_end),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [step["tool"] for step in body["steps"]] == [
        "get_diagnosis",
        "compare_peers",
    ]
    assert body["report"]["dropped"] == 0
    peer_evidence = [item for item in body["evidence"] if item["ref"].startswith("peers")]
    assert peer_evidence
    _assert_claims_are_grounded(body["report"], body["evidence"])
    assert body["report"]["inferences"][0]["evidence_refs"] == ["peers1.s1"]
    assert "普遍" not in body["answer"]


# --------------------------------------------------------------------------- #
# 4. Insufficient data
# --------------------------------------------------------------------------- #
async def test_insufficient_data_stays_rule_based_and_says_so(
    async_client, monkeypatch, checkpointer
):
    branch_id = await _make_branch()

    model = ScenarioModel(
        responses=[
            _call(
                "get_diagnosis",
                "c1",
                start_date=str(_START),
                end_date=str(_END),
            ),
            _final(
                answer="该区间没有足够数据，暂不判断。",
                inferences=[
                    {"text": "因为数据不足所以无法判断", "evidence_refs": ["f9.e9"]}
                ],
            ),
        ]
    )
    token = await _login(async_client)

    response = await _investigate(
        async_client, token, model, monkeypatch, _window_payload(branch_id)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "insufficient_data"
    assert body["report"]["status"] == "insufficient_data"
    assert body["report"]["source"] == "rule"
    assert body["report"]["dropped"] == 1
    assert body["hypotheses"] == []
    assert body["diagnosis"]["status"] == "insufficient_data"
    assert body["diagnosis"]["notes"]
    # No invented ref survives anywhere in the report.
    rendered = json.dumps(body["report"], ensure_ascii=False)
    assert "f9.e9" not in rendered
    assert [step["tool"] for step in body["steps"]] == ["get_diagnosis"]


# --------------------------------------------------------------------------- #
# 5. Business profile (Task61)
# --------------------------------------------------------------------------- #
async def test_business_profile_scenario_cites_reservation_and_baseline_evidence(
    async_client, monkeypatch, checkpointer
):
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id)

    # Current window: two orders a day; the same weekdays one week earlier:
    # three a day. That makes "last week same weekday" comparable and higher.
    await _seed(branch_id, customer_id, item_id, _START, _END, per_day=2)
    await _seed(
        branch_id,
        customer_id,
        item_id,
        _START - timedelta(days=7),
        _END - timedelta(days=7),
        per_day=3,
    )
    # A slot that came and went while still confirmed -> suspected no-show.
    await _book(
        branch_id,
        customer_id,
        start_time=datetime.combine(
            _START, time(18, 0), tzinfo=timezone.utc
        ),
        status="confirmed",
    )
    await _book(
        branch_id,
        customer_id,
        start_time=datetime.combine(
            _END, time(19, 0), tzinfo=timezone.utc
        ),
        status="cancelled",
    )

    model = ScenarioModel(
        responses=[
            _call(
                "get_business_profile",
                "c1",
                start_date=str(_START),
                end_date=str(_END),
            ),
            _final(
                answer="预订取消与疑似未到店值得关注，订单结构与上周同日相比没有明显变化。",
                facts=[
                    {
                        "text": "窗口内存在已取消和疑似未到店的预订",
                        "evidence_refs": ["profile1.r1"],
                    }
                ],
                inferences=[
                    {
                        "text": "上周同星期的订单量高于本期",
                        "evidence_refs": ["profile1.b1"],
                    }
                ],
                checks=[
                    {
                        "text": "核对堂食/外带结构、午餐时段与星期分布的现场记录",
                        "evidence_refs": ["profile1.t1", "profile1.w1"],
                    }
                ],
            ),
        ]
    )
    token = await _login(async_client)

    response = await _investigate(
        async_client, token, model, monkeypatch, _window_payload(branch_id)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [step["tool"] for step in body["steps"]] == ["get_business_profile"]
    assert body["budget"]["tool_calls_used"] == 1
    assert body["report"]["dropped"] == 0
    _assert_claims_are_grounded(body["report"], body["evidence"])
    profile_refs = {
        item["ref"]
        for item in body["evidence"]
        if item["ref"].startswith("profile1.")
    }
    assert profile_refs == {
        "profile1.t1",
        "profile1.w1",
        "profile1.h1",
        "profile1.r1",
        "profile1.b1",
        "profile1.b2",
    }
    reservation_evidence = next(
        item for item in body["evidence"] if item["ref"] == "profile1.r1"
    )
    assert reservation_evidence["value"]["no_show_count"] == 1
    assert reservation_evidence["value"]["cancelled_count"] == 1
    assert body["report"]["inferences"][0]["evidence_refs"] == ["profile1.b1"]
