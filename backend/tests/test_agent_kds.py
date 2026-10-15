"""Customer "contact the kitchen" request over the simulated KDS (§3, feature 1).

A customer with an open order sends a free-text message to the kitchen through
``POST /api/agent/order/{id}/request``. The agent classifies it and drives the
order's simulated KDS state. This suite proves the loop end to end:

* a fresh order starts ``queued``;
* a hold parks the task, records the reason, and is idempotent;
* a resume returns it to the queue;
* a status question reports the current state;
* an illegal move (resuming a task that was never held) is spoken back, not
  raised;
* an empty message is 400, an unknown order 404, another customer's order 403,
  and an unauthenticated caller 401;
* the intent classifier is rules-first, so hold/resume never depend on the LLM.
"""

from uuid import uuid4

from sqlalchemy import delete, select

from backend.app.agent.kds import (
    classify_kds_intent,
    compose_kds_reply,
    rule_based_kds_intent,
)
from backend.app.auth.security import create_access_token
from backend.app.database import transactional_session
from backend.app.models.branch import Branch, Table
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.order import Order
from backend.app.models.user import Customer


def _headers(customer_id: int) -> dict:
    token = create_access_token(
        {"sub": str(customer_id), "type": "customer", "email": "x@example.com"}
    )
    return {"Authorization": f"Bearer {token}"}


async def _make_customer() -> int:
    async with transactional_session() as s:
        customer = Customer(
            name="__kds", email=f"__kds_{uuid4().hex}@example.com", password_hash="x"
        )
        s.add(customer)
        await s.flush()
        return customer.id


async def _make_branch(*, stock: int = 10):
    """An isolated branch with one orderable, well-stocked item."""
    async with transactional_session() as s:
        branch = Branch(
            name=f"__kds{uuid4().hex[:8]}",
            address="x",
            phone="x",
            opening_hours="10:00 - 23:59",
            is_active=True,
        )
        s.add(branch)
        await s.flush()
        s.add(
            Table(
                branch_id=branch.id,
                label="T1",
                seating_area="indoor",
                capacity=4,
                is_available=True,
            )
        )
        category = MenuCategory(branch_id=branch.id, name="主食")
        s.add(category)
        await s.flush()
        item = MenuItem(
            category_id=category.id,
            name=f"__面{uuid4().hex[:6]}",
            description="x",
            base_price=10.0,
            is_available=True,
        )
        s.add(item)
        await s.flush()
        s.add(
            MenuItemStock(menu_item_id=item.id, branch_id=branch.id, stock_quantity=stock)
        )
        return branch.id, item.id


async def _place_order(client, customer_id: int, branch_id: int, item_id: int) -> int:
    resp = await client.post(
        "/api/orders",
        headers=_headers(customer_id),
        json={
            "branch_id": branch_id,
            "order_type": "dine_in",
            "items": [{"menu_item_id": item_id, "quantity": 1}],
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _kds_of(order_id: int) -> tuple:
    """The stored ``(kds_status, kds_held_reason)`` for an order."""
    async with transactional_session() as s:
        res = await s.execute(
            select(Order.kds_status, Order.kds_held_reason).where(Order.id == order_id)
        )
        return res.one()


async def _request(client, order_id: int, customer_id: int, message: str):
    return await client.post(
        f"/api/agent/order/{order_id}/request",
        headers=_headers(customer_id),
        json={"message": message},
    )


async def _cleanup(branch_id=None, *customer_ids):
    async with transactional_session() as s:
        if branch_id is not None:
            await s.execute(delete(Branch).where(Branch.id == branch_id))
        for cid in customer_ids:
            await s.execute(delete(Customer).where(Customer.id == cid))


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
async def test_new_order_starts_queued(async_client):
    branch_id, item_id = await _make_branch()
    customer = await _make_customer()
    try:
        order_id = await _place_order(async_client, customer, branch_id, item_id)
        assert await _kds_of(order_id) == ("queued", None)

        orders = (
            await async_client.get("/api/orders/me", headers=_headers(customer))
        ).json()
        row = next(o for o in orders if o["id"] == order_id)
        assert row["kds_status"] == "queued"
        assert row["kds_held_reason"] is None
    finally:
        await _cleanup(branch_id, customer)


async def test_hold_then_resume_round_trip(async_client):
    branch_id, item_id = await _make_branch()
    customer = await _make_customer()
    try:
        order_id = await _place_order(async_client, customer, branch_id, item_id)

        hold = await _request(async_client, order_id, customer, "先别上热菜，等人齐了再上")
        assert hold.status_code == 200
        assert "暂缓" in hold.json()["reply"]
        status, reason = await _kds_of(order_id)
        assert status == "held"
        assert reason == "先别上热菜，等人齐了再上"

        resume = await _request(async_client, order_id, customer, "可以上菜了")
        assert resume.status_code == 200
        assert "恢复" in resume.json()["reply"]
        assert await _kds_of(order_id) == ("queued", None)
    finally:
        await _cleanup(branch_id, customer)


async def test_status_question_reports_progress(async_client):
    branch_id, item_id = await _make_branch()
    customer = await _make_customer()
    try:
        order_id = await _place_order(async_client, customer, branch_id, item_id)
        resp = await _request(async_client, order_id, customer, "我的菜好了吗？")
        assert resp.status_code == 200
        reply = resp.json()["reply"]
        assert "待制作" in reply
        assert f"#{order_id}" in reply
    finally:
        await _cleanup(branch_id, customer)


async def test_hold_is_idempotent(async_client):
    branch_id, item_id = await _make_branch()
    customer = await _make_customer()
    try:
        order_id = await _place_order(async_client, customer, branch_id, item_id)
        await _request(async_client, order_id, customer, "推迟一下，先别上菜")
        second = await _request(async_client, order_id, customer, "推迟一下，先别上菜")
        assert second.status_code == 200
        # Still a single held task -- no flip-flop, no lost reason.
        assert await _kds_of(order_id) == ("held", "推迟一下，先别上菜")
    finally:
        await _cleanup(branch_id, customer)


async def test_resume_without_hold_is_spoken_back(async_client):
    branch_id, item_id = await _make_branch()
    customer = await _make_customer()
    try:
        order_id = await _place_order(async_client, customer, branch_id, item_id)
        resp = await _request(async_client, order_id, customer, "可以上菜了")
        # Not an HTTP error: the endpoint answers with the agent's reply.
        assert resp.status_code == 200
        assert "并未挂起" in resp.json()["reply"]
        assert await _kds_of(order_id) == ("queued", None)
    finally:
        await _cleanup(branch_id, customer)


# --------------------------------------------------------------------------- #
# HTTP error surface
# --------------------------------------------------------------------------- #
async def test_unknown_order_is_404(async_client):
    customer = await _make_customer()
    try:
        resp = await _request(async_client, 999_999_999, customer, "先别上菜")
        assert resp.status_code == 404
    finally:
        await _cleanup(None, customer)


async def test_other_customers_order_is_403(async_client):
    branch_id, item_id = await _make_branch()
    owner = await _make_customer()
    intruder = await _make_customer()
    try:
        order_id = await _place_order(async_client, owner, branch_id, item_id)
        resp = await _request(async_client, order_id, intruder, "先别上菜")
        assert resp.status_code == 403
        # The intruder changed nothing.
        assert await _kds_of(order_id) == ("queued", None)
    finally:
        await _cleanup(branch_id, owner, intruder)


async def test_empty_message_is_400(async_client):
    branch_id, item_id = await _make_branch()
    customer = await _make_customer()
    try:
        order_id = await _place_order(async_client, customer, branch_id, item_id)
        resp = await _request(async_client, order_id, customer, "   ")
        assert resp.status_code == 400
    finally:
        await _cleanup(branch_id, customer)


async def test_request_requires_auth(async_client):
    resp = await async_client.post(
        "/api/agent/order/1/request", json={"message": "先别上菜"}
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Intent classification (rules-first) and the reply
# --------------------------------------------------------------------------- #
def test_rule_based_intent_is_deterministic():
    assert rule_based_kds_intent("先别上热菜，等人齐了再上") == "hold"
    assert rule_based_kds_intent("推迟二十分钟再上菜") == "hold"
    assert rule_based_kds_intent("可以上菜了") == "resume"
    assert rule_based_kds_intent("不用等了，上菜吧") == "resume"
    assert rule_based_kds_intent("我的菜好了吗") == "status"
    assert rule_based_kds_intent("大概还要多久") == "status"
    # Nothing to recognise -> the caller falls back to the LLM / read-only status.
    assert rule_based_kds_intent("谢谢") is None


async def test_clear_messages_never_reach_the_llm(monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise AssertionError("the LLM must not be consulted for a clear message")

    monkeypatch.setattr("backend.app.agent.kds.llm_client.generate", _boom)
    assert await classify_kds_intent("先别上菜") == "hold"
    assert await classify_kds_intent("可以上菜了") == "resume"


async def test_ambiguous_message_uses_the_llm_then_defaults(monkeypatch):
    calls = {"n": 0}

    async def _label(*_args, **_kwargs):
        calls["n"] += 1
        return "hold"

    monkeypatch.setattr("backend.app.agent.kds.llm_client.generate", _label)
    assert await classify_kds_intent("麻烦你处理一下") == "hold"
    assert calls["n"] == 1

    async def _garbage(*_args, **_kwargs):
        return "sorry, I did not understand"

    monkeypatch.setattr("backend.app.agent.kds.llm_client.generate", _garbage)
    # Unparseable -> the read-only default, which changes nothing.
    assert await classify_kds_intent("麻烦你处理一下") == "status"


def test_compose_reply_covers_each_outcome():
    held = compose_kds_reply(
        "hold", {"success": True, "kds_status": "held", "held_reason": "等人"}, order_id=7
    )
    assert "暂缓" in held and "等人" in held

    resumed = compose_kds_reply(
        "resume", {"success": True, "kds_status": "queued"}, order_id=7
    )
    assert "恢复" in resumed

    status = compose_kds_reply(
        "status",
        {"success": True, "kds_status": "held", "held_reason": "等人"},
        order_id=7,
    )
    assert "已挂起" in status and "等人" in status

    failed = compose_kds_reply(
        "hold", {"success": False, "error": "订单已出餐。"}, order_id=7
    )
    assert failed == "订单已出餐。"
