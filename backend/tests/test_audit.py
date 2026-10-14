"""Task18 tests: permission enforcement and the audit trail (spec §9).

Spec §9 "权限与审计" asks for two things and this module covers both:

* **权限**: the backend validates customer / staff / branch scope. A branch-bound
  staff member cannot read or act on another branch's data.
* **审计**: key operations -- order placement and status moves (including
  cancellation), stock adjustments, reservations (create / status / reschedule /
  table reassignment), branch and table edits -- are recorded together with the
  actor who performed them. The write happens inside the audited operation's own
  transaction, so a rejected operation leaves no audit row behind.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from backend.app.auth.security import create_access_token
from backend.app.database import transactional_session
from backend.app.exceptions import (
    ConflictError,
    ForbiddenError,
    ValidationError,
)
from backend.app.models.branch import Branch, Table
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.user import Customer, StaffUser
from backend.app.repo.reservation import ReservationRepo
from backend.app.repo.user import UserRepo
from backend.app.service.audit import AuditService
from backend.app.service.branch import BranchService
from backend.app.service.menu import MenuService
from backend.app.service.order import OrderService
from backend.app.service.reservation import ReservationService

SYSTEM_BRANCH = 1  # seeded branch, used for menu/order tests


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #
async def _make_branch(*specs, label="__au"):
    """Create an isolated branch; specs = [(label, area, capacity)]."""
    async with transactional_session() as s:
        branch = Branch(
            name=label, address="x", phone="x",
            opening_hours="10:00 - 23:59", is_active=True,
        )
        s.add(branch)
        await s.flush()
        table_ids = []
        for t_label, area, capacity in specs:
            table = Table(
                branch_id=branch.id,
                label=t_label,
                seating_area=area,
                capacity=capacity,
                is_available=True,
            )
            s.add(table)
            await s.flush()
            table_ids.append(table.id)
        return branch.id, table_ids


async def _make_customer():
    async with transactional_session() as s:
        customer = Customer(
            name="__au", email=f"{uuid4().hex}@x.com", password_hash="x"
        )
        s.add(customer)
        await s.flush()
        return customer.id


async def _make_staff(branch_id=None, role="staff"):
    async with transactional_session() as s:
        staff = StaffUser(
            username=f"__au{uuid4().hex[:8]}",
            password_hash="x",
            role=role,
            branch_id=branch_id,
        )
        s.add(staff)
        await s.flush()
        return staff.id


async def _load_staff(staff_id: int):
    async with transactional_session() as s:
        return await UserRepo.get_staff_by_id(s, staff_id)


async def _make_item(branch_id: int = SYSTEM_BRANCH, stock: int = 10, price: float = 2.0):
    async with transactional_session() as s:
        category = MenuCategory(branch_id=branch_id, name=f"__au{uuid4().hex[:8]}")
        s.add(category)
        await s.flush()
        item = MenuItem(
            category_id=category.id,
            name="__au_item",
            base_price=price,
            is_available=True,
        )
        s.add(item)
        await s.flush()
        s.add(
            MenuItemStock(
                menu_item_id=item.id, branch_id=branch_id, stock_quantity=stock
            )
        )
        await s.flush()
        return item.id


async def _logs(action=None, entity_id=None, branch_id=None, limit=200):
    """Read the trail through the Service, as an unrestricted admin would."""
    admin = await _load_staff(await _make_staff(role="admin"))
    return await AuditService.list_logs(
        admin, action=action, entity_id=entity_id, branch_id=branch_id, limit=limit
    )


def _slot(days=600, hour=19, minute=0):
    # ``days`` is a per-test slot discriminator; fold it into the bookable
    # horizon (1..25 days) so the slot stays inside the 30-day window.
    offset = days % 25 + 1
    return (datetime.now(timezone.utc) + timedelta(days=offset)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )


def _headers(staff_id: int, role: str = "staff") -> dict:
    token = create_access_token(
        {"sub": str(staff_id), "type": "staff", "username": f"__au{staff_id}", "role": role}
    )
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Order auditing
# --------------------------------------------------------------------------- #
async def test_order_placement_records_customer_actor():
    item_id = await _make_item()
    order = await OrderService.place_order(
        1, SYSTEM_BRANCH, "dine_in", [{"menu_item_id": item_id, "quantity": 2}]
    )

    rows = await _logs(action="order.created", entity_id=order.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.entity_type == "order"
    assert row.branch_id == SYSTEM_BRANCH
    assert row.actor_type == "customer"
    assert row.actor_id == 1  # seeded customer
    assert row.details["status"] == "placed"
    assert row.details["order_type"] == "dine_in"
    assert row.details["line_count"] == 1


async def test_order_status_change_records_staff_actor_and_cancellation():
    item_id = await _make_item()
    order = await OrderService.place_order(
        1, SYSTEM_BRANCH, "dine_in", [{"menu_item_id": item_id, "quantity": 1}]
    )
    staff_id = await _make_staff(branch_id=SYSTEM_BRANCH)
    staff = await _load_staff(staff_id)

    await OrderService.update_status(order.id, "confirmed", staff=staff)
    await OrderService.update_status(order.id, "cancelled", staff=staff)

    rows = await _logs(action="order.status_changed", entity_id=order.id)
    # Newest first.
    assert [(r.details["from"], r.details["to"]) for r in rows] == [
        ("confirmed", "cancelled"),
        ("placed", "confirmed"),
    ]
    assert all(r.actor_type == "staff" for r in rows)
    assert all(r.actor_id == staff_id for r in rows)
    assert all(r.actor_name == staff.username for r in rows)


async def test_illegal_transition_leaves_no_audit_row():
    item_id = await _make_item()
    order = await OrderService.place_order(
        1, SYSTEM_BRANCH, "dine_in", [{"menu_item_id": item_id, "quantity": 1}]
    )

    with pytest.raises(ValidationError):
        await OrderService.update_status(order.id, "delivered", staff=None)

    assert await _logs(action="order.status_changed", entity_id=order.id) == []


async def test_rejected_order_leaves_no_audit_row():
    """Insufficient stock aborts the placement -- and no order.created row."""
    item_id = await _make_item(stock=1)
    before = len(await _logs(action="order.created"))

    with pytest.raises(ConflictError):
        await OrderService.place_order(
            1, SYSTEM_BRANCH, "dine_in", [{"menu_item_id": item_id, "quantity": 5}]
        )

    assert len(await _logs(action="order.created")) == before


# --------------------------------------------------------------------------- #
# Menu / stock auditing
# --------------------------------------------------------------------------- #
async def test_stock_update_records_from_to_and_actor():
    item_id = await _make_item(stock=10)
    staff_id = await _make_staff(branch_id=SYSTEM_BRANCH)
    staff = await _load_staff(staff_id)

    await MenuService.update_stock(staff, item_id, SYSTEM_BRANCH, 7)

    rows = await _logs(action="stock.updated", entity_id=item_id)
    assert len(rows) == 1
    assert rows[0].details == {"from": 10, "to": 7}
    assert rows[0].actor_type == "staff"
    assert rows[0].actor_id == staff_id
    assert rows[0].branch_id == SYSTEM_BRANCH


async def test_menu_item_update_records_field_changes():
    item_id = await _make_item(price=2.0)
    staff_id = await _make_staff(branch_id=SYSTEM_BRANCH)
    staff = await _load_staff(staff_id)

    await MenuService.update_item(staff, item_id, {"base_price": 9.5, "name": "__au_new"})

    rows = await _logs(action="menu_item.updated", entity_id=item_id)
    assert len(rows) == 1
    assert rows[0].details["changes"]["base_price"] == [2.0, 9.5]
    assert rows[0].details["changes"]["name"] == ["__au_item", "__au_new"]


# --------------------------------------------------------------------------- #
# Reservation auditing
# --------------------------------------------------------------------------- #
async def test_reservation_creation_records_customer_actor():
    branch_id, (table_id,) = await _make_branch(("T1", "indoor", 4))
    customer_id = await _make_customer()

    reservation = await ReservationService.book_table(
        customer_id, branch_id, _slot(), party_size=2
    )

    rows = await _logs(action="reservation.created", entity_id=reservation.id)
    assert len(rows) == 1
    assert rows[0].actor_type == "customer"
    assert rows[0].actor_id == customer_id
    assert rows[0].branch_id == branch_id
    assert rows[0].details["status"] == "pending"
    assert rows[0].details["table_id"] == table_id


async def test_reservation_status_change_records_staff_actor():
    branch_id, _ = await _make_branch(("T1", "indoor", 4))
    customer_id = await _make_customer()
    reservation = await ReservationService.book_table(
        customer_id, branch_id, _slot(hour=18), party_size=2
    )
    staff_id = await _make_staff(branch_id=branch_id)
    staff = await _load_staff(staff_id)

    await ReservationService.update_reservation(
        reservation.id, {"status": "confirmed"}, staff=staff
    )

    rows = await _logs(action="reservation.status_changed", entity_id=reservation.id)
    assert len(rows) == 1
    assert rows[0].details == {"from": "pending", "to": "confirmed"}
    assert rows[0].actor_id == staff_id


async def test_reschedule_and_table_reassign_are_recorded_separately():
    # Different capacities make the best-fit pick (T1) and the manual move (T2)
    # deterministic, and raising the party size past T1's capacity forces the move.
    branch_id, (first_table, second_table) = await _make_branch(
        ("T1", "indoor", 2), ("T2", "indoor", 4)
    )
    customer_id = await _make_customer()
    reservation = await ReservationService.book_table(
        customer_id, branch_id, _slot(hour=12), party_size=2
    )
    staff_id = await _make_staff(branch_id=branch_id)
    staff = await _load_staff(staff_id)

    new_start = _slot(hour=17)
    await ReservationService.update_reservation(
        reservation.id,
        {"start_time": new_start, "party_size": 3, "table_id": second_table},
        staff=staff,
    )

    rescheduled = await _logs(action="reservation.rescheduled", entity_id=reservation.id)
    assert len(rescheduled) == 1
    assert rescheduled[0].details["party_size"] == [2, 3]
    assert rescheduled[0].details["start_time"][1] == new_start.isoformat()

    reassigned = await _logs(
        action="reservation.table_reassigned", entity_id=reservation.id
    )
    assert len(reassigned) == 1
    assert reassigned[0].details["from_table_id"] == first_table
    assert reassigned[0].details["to_table_id"] == second_table
    assert reassigned[0].details["from_table_label"] == "T1"
    assert reassigned[0].details["to_table_label"] == "T2"
    assert reassigned[0].actor_id == staff_id


async def test_seating_a_tableless_booking_records_reassignment():
    """Auto-assignment on seat is an auditable table move too."""
    branch_id, (table_id,) = await _make_branch(("T1", "indoor", 4))
    customer_id = await _make_customer()
    staff_id = await _make_staff(branch_id=branch_id)
    staff = await _load_staff(staff_id)

    async with transactional_session() as s:
        start = _slot(hour=20)
        reservation = await ReservationRepo.create_reservation(
            s,
            customer_id=customer_id,
            branch_id=branch_id,
            table_id=None,
            party_size=2,
            start_time=start,
            end_time=start + timedelta(minutes=90),
            duration_minutes=90,
            seating_area_preference="indoor",
            status="confirmed",
            notes=None,
        )
        reservation_id = reservation.id

    await ReservationService.update_reservation(
        reservation_id, {"status": "seated"}, staff=staff
    )

    reassigned = await _logs(
        action="reservation.table_reassigned", entity_id=reservation_id
    )
    assert len(reassigned) == 1
    assert reassigned[0].details["from_table_id"] is None
    assert reassigned[0].details["to_table_id"] == table_id


# --------------------------------------------------------------------------- #
# Branch / table auditing
# --------------------------------------------------------------------------- #
async def test_branch_and_table_updates_record_admin_actor():
    branch_id, (table_id,) = await _make_branch(("T1", "indoor", 4))
    admin_id = await _make_staff(role="admin")
    admin = await _load_staff(admin_id)

    await BranchService.update_branch(branch_id, {"phone": "010-1234"}, actor=admin)
    await BranchService.update_table(admin, table_id, {"capacity": 8})

    branch_rows = await _logs(action="branch.updated", entity_id=branch_id)
    assert len(branch_rows) == 1
    assert branch_rows[0].details["changes"]["phone"] == ["x", "010-1234"]
    assert branch_rows[0].actor_type == "staff"
    assert branch_rows[0].actor_role == "admin"
    assert branch_rows[0].actor_id == admin_id

    table_rows = await _logs(action="table.updated", entity_id=table_id)
    assert len(table_rows) == 1
    assert table_rows[0].details["changes"]["capacity"] == [4, 8]
    assert table_rows[0].branch_id == branch_id


# --------------------------------------------------------------------------- #
# Permission enforcement
# --------------------------------------------------------------------------- #
async def test_audit_log_listing_is_branch_scoped():
    branch_a, (table_a,) = await _make_branch(("A1", "indoor", 4))
    branch_b, (table_b,) = await _make_branch(("B1", "indoor", 4))
    staff_a = await _load_staff(await _make_staff(branch_id=branch_a))
    staff_b = await _load_staff(await _make_staff(branch_id=branch_b))

    await BranchService.update_table(staff_a, table_a, {"capacity": 3})
    await BranchService.update_table(staff_b, table_b, {"capacity": 5})

    visible = await AuditService.list_logs(staff_a, action="table.updated")
    assert {row.branch_id for row in visible} == {branch_a}
    assert any(row.details["changes"]["capacity"] == [4, 3] for row in visible)
    assert not any(row.details["changes"]["capacity"] == [4, 5] for row in visible)

    # A branch-bound staff member cannot widen the scope via ?branch_id.
    widened = await AuditService.list_logs(staff_a, branch_id=branch_b)
    assert {row.branch_id for row in widened} == {branch_a}


async def test_audit_log_limit_is_validated():
    staff = await _load_staff(await _make_staff(role="admin"))
    with pytest.raises(ValidationError):
        await AuditService.list_logs(staff, limit=0)


async def test_availability_query_is_branch_scoped():
    branch_a, _ = await _make_branch(("A1", "indoor", 4))
    staff_a = await _load_staff(await _make_staff(branch_id=branch_a))
    admin = await _load_staff(await _make_staff(role="admin"))

    with pytest.raises(ForbiddenError):
        await ReservationService.list_available_tables(
            SYSTEM_BRANCH, _slot(), party_size=2, staff=staff_a
        )

    # Own branch, and an unrestricted admin, both get an answer.
    assert await ReservationService.list_available_tables(
        branch_a, _slot(), party_size=2, staff=staff_a
    )
    assert await ReservationService.list_available_tables(
        SYSTEM_BRANCH, _slot(), party_size=2, staff=admin
    )


async def test_audit_log_route_requires_staff_token(async_client):
    assert (await async_client.get("/api/staff/audit-logs")).status_code == 401
    customer_token = create_access_token({"sub": "1", "type": "customer"})
    resp = await async_client.get(
        "/api/staff/audit-logs",
        headers={"Authorization": f"Bearer {customer_token}"},
    )
    assert resp.status_code == 401


async def test_audit_log_route_returns_labelled_rows(async_client):
    item_id = await _make_item(stock=20)
    staff_id = await _make_staff(branch_id=SYSTEM_BRANCH)
    staff = await _load_staff(staff_id)
    await MenuService.update_stock(staff, item_id, SYSTEM_BRANCH, 13)

    resp = await async_client.get(
        "/api/staff/audit-logs",
        params={"action": "stock.updated", "entity_id": item_id},
        headers=_headers(staff_id),
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["action"] == "stock.updated"
    assert rows[0]["action_label"] == "库存调整"
    assert rows[0]["entity_label"] == "菜品"
    assert rows[0]["actor_name"] == staff.username
    assert rows[0]["details"] == {"from": 20, "to": 13}


async def test_audit_log_route_is_branch_scoped(async_client):
    branch_a, (table_a,) = await _make_branch(("A1", "indoor", 4))
    branch_b, (table_b,) = await _make_branch(("B1", "indoor", 4))
    staff_a_id = await _make_staff(branch_id=branch_a)
    staff_b_id = await _make_staff(branch_id=branch_b)
    await BranchService.update_table(await _load_staff(staff_a_id), table_a, {"capacity": 3})
    await BranchService.update_table(await _load_staff(staff_b_id), table_b, {"capacity": 5})

    resp = await async_client.get(
        "/api/staff/audit-logs",
        params={"action": "table.updated", "branch_id": branch_b},
        headers=_headers(staff_a_id),
    )
    assert resp.status_code == 200
    rows = resp.json()
    # The foreign branch_id is ignored: only branch A's own trail is visible.
    assert rows and {row["branch_id"] for row in rows} == {branch_a}
