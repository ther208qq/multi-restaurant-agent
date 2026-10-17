"""Task12 tests: customer activity queries served through the Services.

The ``/orders/me`` and ``/reservations/me`` routes read the customer's history
through OrderService / ReservationService. The legacy Agent ``status_node``
tests were removed with the unwired chat graph; the same Service paths remain
covered here through the HTTP routes.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete

from backend.app.auth.security import create_access_token
from backend.app.database import transactional_session
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.order import Order
from backend.app.models.reservation import Reservation
from backend.app.models.user import Customer
from backend.app.service.auth import AuthService
from backend.app.service.order import OrderService
from backend.app.service.reservation import ReservationService

def _email() -> str:
    return f"__ca_{uuid4().hex}@example.com"


def _headers(customer_id: int) -> dict:
    token = create_access_token(
        {"sub": str(customer_id), "type": "customer", "email": "x@example.com"}
    )
    return {"Authorization": f"Bearer {token}"}


def _slot(days: int, hour: int) -> datetime:
    # ``days`` is a per-test slot discriminator; fold it into the bookable
    # horizon (1..25 days) so the slot stays inside the 30-day window.
    offset = days % 25 + 1
    return (datetime.now(timezone.utc) + timedelta(days=offset)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


async def _make_item(branch_id: int = 1, stock: int = 10, base_price: float = 2.0):
    async with transactional_session() as s:
        cat = MenuCategory(branch_id=branch_id, name="__ca_cat")
        s.add(cat)
        await s.flush()
        item = MenuItem(
            category_id=cat.id, name="__ca_item", base_price=base_price, is_available=True
        )
        s.add(item)
        await s.flush()
        s.add(MenuItemStock(menu_item_id=item.id, branch_id=branch_id, stock_quantity=stock))
        await s.flush()
        return item.id, cat.id


async def _make_customer() -> int:
    customer = await AuthService.register_customer(
        name="__ca", email=_email(), password="x"
    )
    return customer.id


async def _cleanup(customer_id=None, cat_id=None):
    async with transactional_session() as s:
        if customer_id is not None:
            await s.execute(delete(Order).where(Order.customer_id == customer_id))
            await s.execute(
                delete(Reservation).where(Reservation.customer_id == customer_id)
            )
        if cat_id is not None:
            await s.execute(delete(MenuCategory).where(MenuCategory.id == cat_id))
        if customer_id is not None:
            await s.execute(delete(Customer).where(Customer.id == customer_id))


async def test_my_orders_lists_customer_orders(async_client):
    item_id, cat_id = await _make_item()
    customer_id = await _make_customer()
    try:
        order = await OrderService.place_order(
            customer_id, 1, "pickup", [{"menu_item_id": item_id, "quantity": 1}]
        )
        resp = await async_client.get("/api/orders/me", headers=_headers(customer_id))
        assert resp.status_code == 200
        body = resp.json()
        match = next((o for o in body if o["id"] == order.id), None)
        assert match is not None
        assert match["customer_id"] == customer_id
        assert len(match["items"]) == 1
    finally:
        await _cleanup(customer_id=customer_id, cat_id=cat_id)


async def test_my_orders_scoped_to_current_customer(async_client):
    item_id, cat_id = await _make_item()
    owner_id = await _make_customer()
    other_id = await _make_customer()
    try:
        order = await OrderService.place_order(
            owner_id, 1, "pickup", [{"menu_item_id": item_id, "quantity": 1}]
        )
        resp = await async_client.get("/api/orders/me", headers=_headers(other_id))
        assert resp.status_code == 200
        assert all(o["id"] != order.id for o in resp.json())
    finally:
        await _cleanup(customer_id=owner_id, cat_id=cat_id)
        await _cleanup(customer_id=other_id)


async def test_my_reservations_newest_first(async_client):
    customer_id = await _make_customer()
    try:
        later = await ReservationService.book_table(
            customer_id=customer_id, branch_id=1, start_time=_slot(700, 20), party_size=2
        )
        earlier = await ReservationService.book_table(
            customer_id=customer_id, branch_id=1, start_time=_slot(700, 18), party_size=2
        )
        resp = await async_client.get(
            "/api/reservations/me", headers=_headers(customer_id)
        )
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.json()]
        assert later.id in ids
        assert earlier.id in ids
        assert ids.index(later.id) < ids.index(earlier.id)
    finally:
        await _cleanup(customer_id=customer_id)


async def test_my_orders_requires_auth(async_client):
    assert (await async_client.get("/api/orders/me")).status_code == 401
    assert (await async_client.get("/api/reservations/me")).status_code == 401

