"""Booking upsell offer and gift voucher (AgentFeatures §4, feature 2).

When a requested slot is full, ``GET /api/reservations/availability`` no longer
returns a bare empty list: it embeds the agent's counter-offer -- a nearby free
time plus a surplus dessert / drink -- and ``POST /api/reservations/accept-offer``
books it and issues the gift voucher. This suite proves the loop end to end:

* a full slot yields an ``agent_offer`` (times + gift), a free slot does not;
* accepting books the alternative time, records the ``gifts`` row and takes the
  item's stock;
* a repeat acceptance is idempotent -- one booking, one voucher, one unit of
  stock;
* an out-of-branch gift and an unauthenticated caller are rejected.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete, func, select

from backend.app.agent.offers import compose_offer_message
from backend.app.auth.security import create_access_token
from backend.app.database import transactional_session
from backend.app.models.branch import Branch, Table
from backend.app.models.gift import Gift
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.reservation import Reservation
from backend.app.models.user import Customer
from backend.app.service.gift import GiftService


def _headers(customer_id: int) -> dict:
    token = create_access_token(
        {"sub": str(customer_id), "type": "customer", "email": "x@example.com"}
    )
    return {"Authorization": f"Bearer {token}"}


async def _make_customer() -> int:
    async with transactional_session() as s:
        customer = Customer(
            name="__offer", email=f"__offer_{uuid4().hex}@example.com", password_hash="x"
        )
        s.add(customer)
        await s.flush()
        return customer.id


async def _make_branch(
    *,
    category_name="饮品",
    item_name=None,
    stock=20,
    tables=(("T1", "indoor", 4),),
):
    """An isolated branch with one giftable item and a small set of tables."""
    item_name = item_name or f"__赠品{uuid4().hex[:6]}"
    async with transactional_session() as s:
        branch = Branch(
            name=f"__offer{uuid4().hex[:8]}",
            address="x",
            phone="x",
            opening_hours="10:00 - 23:59",
            is_active=True,
        )
        s.add(branch)
        await s.flush()
        for label, area, capacity in tables:
            s.add(
                Table(
                    branch_id=branch.id,
                    label=label,
                    seating_area=area,
                    capacity=capacity,
                    is_available=True,
                )
            )
        category = MenuCategory(branch_id=branch.id, name=category_name)
        s.add(category)
        await s.flush()
        item = MenuItem(
            category_id=category.id,
            name=item_name,
            description="x",
            base_price=6.0,
            is_available=True,
        )
        s.add(item)
        await s.flush()
        s.add(
            MenuItemStock(menu_item_id=item.id, branch_id=branch.id, stock_quantity=stock)
        )
        return branch.id, item.id, item_name


def _slot(days: int = 1, hour: int = 19, minute: int = 0):
    """A bookable slot as the ``date`` / ``time`` strings (default tomorrow 19:00)."""
    start = (datetime.now(timezone.utc) + timedelta(days=days)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return start.strftime("%Y-%m-%d"), start.strftime("%H:%M")


async def _stock_of(item_id: int, branch_id: int) -> int:
    async with transactional_session() as s:
        res = await s.execute(
            select(MenuItemStock.stock_quantity).where(
                MenuItemStock.menu_item_id == item_id,
                MenuItemStock.branch_id == branch_id,
            )
        )
        return res.scalar_one()


async def _gift_count(branch_id: int) -> int:
    async with transactional_session() as s:
        return (
            await s.execute(
                select(func.count()).select_from(Gift).where(Gift.branch_id == branch_id)
            )
        ).scalar()


async def _reservation_count(customer_id: int) -> int:
    async with transactional_session() as s:
        return (
            await s.execute(
                select(func.count())
                .select_from(Reservation)
                .where(Reservation.customer_id == customer_id)
            )
        ).scalar()


async def _cleanup(branch_id=None, *customer_ids):
    async with transactional_session() as s:
        if branch_id is not None:
            await s.execute(delete(Branch).where(Branch.id == branch_id))
        for cid in customer_ids:
            await s.execute(delete(Customer).where(Customer.id == cid))


# --------------------------------------------------------------------------- #
# Availability carries the offer only when full
# --------------------------------------------------------------------------- #
async def test_availability_offers_when_slot_full(async_client):
    branch_id, item_id, item_name = await _make_branch()
    first = await _make_customer()
    second = await _make_customer()
    date, time = _slot()
    try:
        booked = await async_client.post(
            "/api/reservations",
            headers=_headers(first),
            json={
                "branch_id": branch_id,
                "party_size": 2,
                "requested_date": date,
                "requested_time": time,
            },
        )
        assert booked.status_code == 200

        resp = await async_client.get(
            f"/api/reservations/availability?branch_id={branch_id}"
            f"&date={date}&time={time}&party_size=2",
            headers=_headers(second),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["tables"] == []
        offer = body["agent_offer"]
        assert offer is not None
        assert offer["alternative_times"], "a full slot must suggest another time"
        assert offer["gift_item"]["item_id"] == item_id
        assert item_name in offer["message"]
    finally:
        await _cleanup(branch_id, first, second)


async def test_availability_has_no_offer_when_slot_free(async_client):
    branch_id, _item_id, _name = await _make_branch()
    customer = await _make_customer()
    date, time = _slot()
    try:
        resp = await async_client.get(
            f"/api/reservations/availability?branch_id={branch_id}"
            f"&date={date}&time={time}&party_size=2",
            headers=_headers(customer),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert [t["label"] for t in body["tables"]] == ["T1"]
        assert body["agent_offer"] is None
    finally:
        await _cleanup(branch_id, customer)


# --------------------------------------------------------------------------- #
# Accepting the offer
# --------------------------------------------------------------------------- #
async def test_accept_offer_books_and_issues_gift(async_client):
    branch_id, item_id, item_name = await _make_branch(stock=20)
    first = await _make_customer()
    second = await _make_customer()
    date, time = _slot()
    try:
        await async_client.post(
            "/api/reservations",
            headers=_headers(first),
            json={
                "branch_id": branch_id,
                "party_size": 2,
                "requested_date": date,
                "requested_time": time,
            },
        )
        offer = (
            await async_client.get(
                f"/api/reservations/availability?branch_id={branch_id}"
                f"&date={date}&time={time}&party_size=2",
                headers=_headers(second),
            )
        ).json()["agent_offer"]
        accepted_time = offer["alternative_times"][0]

        resp = await async_client.post(
            "/api/reservations/accept-offer",
            headers=_headers(second),
            json={
                "branch_id": branch_id,
                "party_size": 2,
                "requested_date": date,
                "requested_time": accepted_time,
                "gift_item_id": item_id,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["reservation"]["status"] == "pending"
        assert body["gift"]["status"] == "pending"
        assert body["gift"]["item_id"] == item_id
        assert body["gift"]["item_name"] == item_name
        assert body["gift"]["source_type"] == "reservation"
        assert body["gift"]["source_id"] == body["reservation"]["id"]

        assert await _stock_of(item_id, branch_id) == 19
        assert await _gift_count(branch_id) == 1
    finally:
        await _cleanup(branch_id, first, second)


async def test_accept_offer_is_idempotent(async_client):
    branch_id, item_id, _name = await _make_branch(stock=20)
    first = await _make_customer()
    second = await _make_customer()
    date, time = _slot()
    payload = {
        "branch_id": branch_id,
        "party_size": 2,
        "requested_date": date,
        "requested_time": "20:30",
        "gift_item_id": item_id,
    }
    try:
        await async_client.post(
            "/api/reservations",
            headers=_headers(first),
            json={
                "branch_id": branch_id,
                "party_size": 2,
                "requested_date": date,
                "requested_time": time,
            },
        )
        # "20:30" is a genuinely free slot for the single table (the first party
        # holds 19:00-20:30), so the offer is accepted without needing availability.
        first_accept = await async_client.post(
            "/api/reservations/accept-offer", headers=_headers(second), json=payload
        )
        second_accept = await async_client.post(
            "/api/reservations/accept-offer", headers=_headers(second), json=payload
        )
        assert first_accept.status_code == second_accept.status_code == 200
        assert (
            first_accept.json()["reservation"]["id"]
            == second_accept.json()["reservation"]["id"]
        )
        # One booking, one voucher, one unit of stock -- the retry took nothing.
        assert await _reservation_count(second) == 1
        assert await _gift_count(branch_id) == 1
        assert await _stock_of(item_id, branch_id) == 19
    finally:
        await _cleanup(branch_id, first, second)


async def test_accept_offer_rejects_foreign_gift(async_client):
    branch_id, _item_id, _name = await _make_branch()
    other_branch_id, other_item_id, _other_name = await _make_branch()
    customer = await _make_customer()
    date, time = _slot()
    try:
        resp = await async_client.post(
            "/api/reservations/accept-offer",
            headers=_headers(customer),
            json={
                "branch_id": branch_id,
                "party_size": 2,
                "requested_date": date,
                "requested_time": time,
                "gift_item_id": other_item_id,
            },
        )
        assert resp.status_code == 400
    finally:
        await _cleanup(branch_id, customer)
        await _cleanup(other_branch_id)


async def test_offer_endpoints_require_auth(async_client):
    date, time = _slot()
    assert (
        await async_client.get(
            f"/api/reservations/availability?branch_id=1&date={date}&time={time}"
        )
    ).status_code == 401
    assert (
        await async_client.post("/api/reservations/accept-offer", json={})
    ).status_code == 401


# --------------------------------------------------------------------------- #
# The surplus rule and the message
# --------------------------------------------------------------------------- #
async def test_surplus_inventory_excludes_low_stock_and_other_categories():
    branch_id, drink_id, drink_name = await _make_branch(
        category_name="饮品", item_name="__气泡饮", stock=7
    )
    async with transactional_session() as s:
        dessert = MenuCategory(branch_id=branch_id, name="甜点")
        pizza = MenuCategory(branch_id=branch_id, name="披萨")
        s.add_all([dessert, pizza])
        await s.flush()
        low = MenuItem(category_id=dessert.id, name="__低库存甜点", base_price=5, is_available=True)
        plenty = MenuItem(category_id=pizza.id, name="__披萨", base_price=12, is_available=True)
        s.add_all([low, plenty])
        await s.flush()
        s.add_all(
            [
                MenuItemStock(menu_item_id=low.id, branch_id=branch_id, stock_quantity=3),
                MenuItemStock(menu_item_id=plenty.id, branch_id=branch_id, stock_quantity=99),
            ]
        )
    try:
        items = await GiftService.get_surplus_inventory(branch_id)
        names = {i["name"] for i in items}
        assert drink_name in names  # drink, stock 7 >= threshold
        assert "__低库存甜点" not in names  # below the stock threshold
        assert "__披萨" not in names  # not a dessert / drink category
    finally:
        await _cleanup(branch_id)


def test_compose_offer_message_mentions_times_and_gift():
    message = compose_offer_message(
        alternative_times=["19:30", "20:00"], gift={"name": "手工柠檬水"}, party_size=4
    )
    assert "19:30" in message and "20:00" in message
    assert "手工柠檬水" in message
    assert "4" in message

    plain = compose_offer_message(alternative_times=["19:30"], gift=None, party_size=2)
    assert "19:30" in plain
    assert "赠" not in plain
