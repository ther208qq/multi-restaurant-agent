"""Customer self-service table booking over HTTP (OpenItems section 10).

``POST /api/reservations`` and ``GET /api/reservations/availability`` let a
customer pick an area and book without the chat Agent; the system chooses the
table. This suite proves the wiring and the guarantees:

* a booking lands as ``pending`` (staff confirm it later), holds a table and
  writes the ``reservation.created`` audit row for the authenticated customer;
* a repeat submission returns the booking already made (``deduplicate=True``) --
  one row, not two tables for the same party;
* a slot with no free table, a malformed date/time and an unknown branch are
  rejected with 409 / 400 / 404;
* the customer id always comes from the JWT.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete, func, select

from backend.app.auth.security import create_access_token
from backend.app.database import transactional_session
from backend.app.models.branch import Branch, Table
from backend.app.models.reservation import Reservation
from backend.app.models.user import Customer, StaffUser
from backend.app.repo.user import UserRepo
from backend.app.service.audit import AuditService


def _headers(customer_id: int) -> dict:
    token = create_access_token(
        {"sub": str(customer_id), "type": "customer", "email": "x@example.com"}
    )
    return {"Authorization": f"Bearer {token}"}


async def _make_customer() -> int:
    async with transactional_session() as s:
        customer = Customer(
            name="__cb", email=f"__cb_{uuid4().hex}@example.com", password_hash="x"
        )
        s.add(customer)
        await s.flush()
        return customer.id


async def _make_branch(*specs):
    """Create an isolated branch; specs = [(label, area, capacity)]."""
    async with transactional_session() as s:
        branch = Branch(
            name=f"__cb{uuid4().hex[:8]}", address="x", phone="x",
            opening_hours="10:00 - 23:59", is_active=True,
        )
        s.add(branch)
        await s.flush()
        table_ids = []
        for label, area, capacity in specs:
            table = Table(
                branch_id=branch.id,
                label=label,
                seating_area=area,
                capacity=capacity,
                is_available=True,
            )
            s.add(table)
            await s.flush()
            table_ids.append(table.id)
        return branch.id, table_ids


def _slot_strings(days: int = 600, hour: int = 19, minute: int = 0):
    """A bookable (in-window) slot as the ``requested_date`` / ``requested_time``
    strings. ``days`` is a slot discriminator folded into the 1..25 day horizon."""
    offset = days % 25 + 1
    start = (datetime.now(timezone.utc) + timedelta(days=offset)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return start.strftime("%Y-%m-%d"), start.strftime("%H:%M")


async def _reservation_count(customer_id: int) -> int:
    async with transactional_session() as s:
        return (
            await s.execute(
                select(func.count())
                .select_from(Reservation)
                .where(Reservation.customer_id == customer_id)
            )
        ).scalar()


async def _logs(action=None, entity_id=None):
    """Read the audit trail through the Service, as an unrestricted admin."""
    async with transactional_session() as s:
        admin = StaffUser(
            username=f"__cb{uuid4().hex[:8]}", password_hash="x", role="admin"
        )
        s.add(admin)
        await s.flush()
        admin_id = admin.id
    async with transactional_session() as s:
        admin = await UserRepo.get_staff_by_id(s, admin_id)
    return await AuditService.list_logs(
        admin, action=action, entity_id=entity_id, limit=200
    )


async def _cleanup(customer_id=None, branch_id=None):
    async with transactional_session() as s:
        if customer_id is not None:
            await s.execute(delete(Customer).where(Customer.id == customer_id))
        if branch_id is not None:
            await s.execute(delete(Branch).where(Branch.id == branch_id))


# --------------------------------------------------------------------------- #
# Booking
# --------------------------------------------------------------------------- #
async def test_book_table_happy_path(async_client):
    branch_id, _ = await _make_branch(("T1", "indoor", 4))
    customer_id = await _make_customer()
    date, time = _slot_strings()
    payload = {
        "branch_id": branch_id,
        "party_size": 2,
        "requested_date": date,
        "requested_time": time,
    }
    try:
        resp = await async_client.post(
            "/api/reservations", headers=_headers(customer_id), json=payload
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "pending"  # waits for staff confirmation
        assert body["table_id"]  # the system picked a table
        assert body["table_label"] == "T1"
        assert body["branch_name"]
        assert body["customer_id"] == customer_id

        rows = await _logs(action="reservation.created", entity_id=body["id"])
        assert len(rows) == 1
        assert rows[0].actor_type == "customer"
        assert rows[0].actor_id == customer_id
    finally:
        await _cleanup(customer_id=customer_id, branch_id=branch_id)


async def test_duplicate_booking_returns_same_reservation(async_client):
    branch_id, _ = await _make_branch(("T1", "indoor", 4))
    customer_id = await _make_customer()
    date, time = _slot_strings()
    payload = {
        "branch_id": branch_id,
        "party_size": 2,
        "requested_date": date,
        "requested_time": time,
    }
    try:
        first = await async_client.post(
            "/api/reservations", headers=_headers(customer_id), json=payload
        )
        second = await async_client.post(
            "/api/reservations", headers=_headers(customer_id), json=payload
        )
        assert first.status_code == second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        assert await _reservation_count(customer_id) == 1  # one table, not two
    finally:
        await _cleanup(customer_id=customer_id, branch_id=branch_id)


async def test_booking_conflicts_when_table_taken(async_client):
    """A second party at the same slot finds the only table taken (409)."""
    branch_id, _ = await _make_branch(("T1", "indoor", 2))
    first_id = await _make_customer()
    second_id = await _make_customer()
    date, time = _slot_strings()
    payload = {
        "branch_id": branch_id,
        "party_size": 2,
        "requested_date": date,
        "requested_time": time,
    }
    try:
        ok = await async_client.post(
            "/api/reservations", headers=_headers(first_id), json=payload
        )
        assert ok.status_code == 200
        clash = await async_client.post(
            "/api/reservations", headers=_headers(second_id), json=payload
        )
        assert clash.status_code == 409
        assert await _reservation_count(second_id) == 0
    finally:
        await _cleanup(customer_id=first_id, branch_id=branch_id)
        await _cleanup(customer_id=second_id)


async def test_booking_rejects_bad_slot_format(async_client):
    branch_id, _ = await _make_branch(("T1", "indoor", 4))
    customer_id = await _make_customer()
    payload = {
        "branch_id": branch_id,
        "party_size": 2,
        "requested_date": "not-a-date",
        "requested_time": "19:00",
    }
    try:
        resp = await async_client.post(
            "/api/reservations", headers=_headers(customer_id), json=payload
        )
        assert resp.status_code == 400
    finally:
        await _cleanup(customer_id=customer_id, branch_id=branch_id)


async def test_booking_unknown_branch_is_404(async_client):
    customer_id = await _make_customer()
    date, time = _slot_strings()
    payload = {
        "branch_id": 999999,
        "party_size": 2,
        "requested_date": date,
        "requested_time": time,
    }
    try:
        resp = await async_client.post(
            "/api/reservations", headers=_headers(customer_id), json=payload
        )
        assert resp.status_code == 404
    finally:
        await _cleanup(customer_id=customer_id)


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #
async def test_availability_lists_free_tables(async_client):
    branch_id, _ = await _make_branch(("T1", "indoor", 4))
    customer_id = await _make_customer()
    date, time = _slot_strings()
    query = (
        f"/api/reservations/availability?branch_id={branch_id}"
        f"&date={date}&time={time}&party_size=2"
    )
    try:
        resp = await async_client.get(query, headers=_headers(customer_id))
        assert resp.status_code == 200
        labels = [t["label"] for t in resp.json()]
        assert "T1" in labels
    finally:
        await _cleanup(customer_id=customer_id, branch_id=branch_id)


async def test_booking_requires_auth(async_client):
    assert (
        await async_client.post("/api/reservations", json={})
    ).status_code == 401
    assert (
        await async_client.get(
            "/api/reservations/availability?branch_id=1&date=2027-01-01&time=19:00"
        )
    ).status_code == 401