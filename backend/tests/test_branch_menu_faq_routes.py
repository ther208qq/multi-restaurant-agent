"""Task3 route tests: Branch / Menu / FAQ HTTP endpoints served via Services.

These verify the migrated API layer still behaves as before: same shapes, same
status codes, with domain exceptions mapped to HTTP by the global handlers.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func

from backend.app.auth.security import create_access_token
from backend.app.database import transactional_session
from backend.app.models.branch import Table
from backend.app.models.menu import MenuCategory
from backend.app.models.reservation import Reservation
from backend.app.models.user import StaffUser
from backend.app.service.menu import MenuService

_ADMIN = StaffUser(username="t", role="admin", branch_id=None)


def _staff_headers(staff_id: int, role: str = "staff") -> dict:
    token = create_access_token(
        {"sub": str(staff_id), "type": "staff", "username": f"s{staff_id}", "role": role}
    )
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Public branch / menu / faq
# --------------------------------------------------------------------------- #


async def test_public_list_branches(async_client):
    resp = await async_client.get("/api/branches")
    assert resp.status_code == 200
    data = resp.json()
    assert data
    assert all("id" in b and "tables" in b for b in data)


async def test_public_branch_menu(async_client):
    resp = await async_client.get("/api/branches/1/menu")
    assert resp.status_code == 200
    data = resp.json()
    assert data["branch_id"] == 1
    assert data["categories"]
    item = data["categories"][0]["items"][0]
    assert set(item) >= {"id", "effective_available", "variants"}


async def test_public_branch_menu_not_found(async_client):
    resp = await async_client.get("/api/branches/999999/menu")
    assert resp.status_code == 404
    assert resp.json()["detail"]


async def test_public_branch_faq(async_client):
    resp = await async_client.get("/api/branches/1/faq")
    assert resp.status_code == 200
    assert resp.json()["policies"]


async def test_public_branch_faq_not_found(async_client):
    resp = await async_client.get("/api/branches/999999/faq")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Staff branches / tables
# --------------------------------------------------------------------------- #


async def test_staff_list_branches_requires_auth(async_client):
    resp = await async_client.get("/api/staff/branches")
    assert resp.status_code == 401


async def test_staff_list_branches_admin(async_client, staff_auth_headers):
    resp = await async_client.get("/api/staff/branches", headers=staff_auth_headers)
    assert resp.status_code == 200
    assert resp.json()


async def test_staff_list_tables(async_client, staff_auth_headers):
    resp = await async_client.get("/api/staff/branches/tables", headers=staff_auth_headers)
    assert resp.status_code == 200
    assert resp.json()


async def test_staff_update_branch_not_found(async_client):
    resp = await async_client.patch(
        "/api/staff/branches/999999",
        json={"name": "x"},
        headers=_staff_headers(1, role="admin"),
    )
    assert resp.status_code == 404


async def test_staff_create_table_success(async_client, staff_auth_headers):
    resp = await async_client.post(
        "/api/staff/branches/tables",
        json={"branch_id": 1, "label": "__route_t", "seating_area": "indoor", "capacity": 2},
        headers=staff_auth_headers,
    )
    assert resp.status_code == 200
    table_id = resp.json()["id"]
    async with transactional_session() as session:
        await session.execute(delete(Table).where(Table.id == table_id))



async def test_staff_create_table_forbidden_for_other_branch(async_client):
    # staff id 2 is branch-scoped to branch 1; branch 2 must be forbidden.
    resp = await async_client.post(
        "/api/staff/branches/tables",
        json={"branch_id": 2, "label": "X", "seating_area": "indoor", "capacity": 2},
        headers=_staff_headers(2, role="staff"),
    )
    assert resp.status_code == 403


async def test_staff_update_table_success(async_client, staff_auth_headers):
    created = await async_client.post(
        "/api/staff/branches/tables",
        json={"branch_id": 1, "label": "__route_upd", "seating_area": "indoor", "capacity": 2},
        headers=staff_auth_headers,
    )
    assert created.status_code == 200
    table_id = created.json()["id"]

    patched = await async_client.patch(
        f"/api/staff/branches/tables/{table_id}",
        json={"label": "__route_upd2", "capacity": 6, "seating_area": "rooftop"},
        headers=staff_auth_headers,
    )
    assert patched.status_code == 200
    assert patched.json()["label"] == "__route_upd2"
    assert patched.json()["capacity"] == 6
    assert patched.json()["seating_area"] == "rooftop"

    listed = await async_client.get(
        "/api/staff/branches/tables", headers=staff_auth_headers
    )
    row = next(t for t in listed.json() if t["id"] == table_id)
    assert row["label"] == "__route_upd2"
    assert row["capacity"] == 6

    async with transactional_session() as session:
        await session.execute(delete(Table).where(Table.id == table_id))


async def test_staff_create_table_duplicate_label_is_refused(async_client, staff_auth_headers):
    """Two tables sharing a name in one branch are indistinguishable to staff."""
    created = await async_client.post(
        "/api/staff/branches/tables",
        json={"branch_id": 1, "label": "__route_dup", "seating_area": "indoor", "capacity": 4},
        headers=staff_auth_headers,
    )
    assert created.status_code == 200
    table_id = created.json()["id"]

    second = await async_client.post(
        "/api/staff/branches/tables",
        json={"branch_id": 1, "label": "__route_dup", "seating_area": "outdoor", "capacity": 6},
        headers=staff_auth_headers,
    )
    assert second.status_code == 409
    assert "__route_dup" in second.json()["detail"]

    async with transactional_session() as session:
        await session.execute(delete(Table).where(Table.id == table_id))


async def test_staff_create_table_same_label_other_branch_allowed(async_client):
    """The name only has to be unique inside its branch -- the seed reuses R1."""
    ids = []
    for branch_id in (1, 2):
        resp = await async_client.post(
            "/api/staff/branches/tables",
            json={
                "branch_id": branch_id,
                "label": "__route_dup_x",
                "seating_area": "indoor",
                "capacity": 2,
            },
            headers=_staff_headers(1, role="admin"),
        )
        assert resp.status_code == 200, resp.text
        ids.append(resp.json()["id"])

    async with transactional_session() as session:
        await session.execute(delete(Table).where(Table.id.in_(ids)))


async def test_staff_update_table_duplicate_label_is_refused(async_client, staff_auth_headers):
    """Renaming onto a taken label is a 409; re-sending one's own label is not."""
    first = await async_client.post(
        "/api/staff/branches/tables",
        json={"branch_id": 1, "label": "__route_dup_a", "seating_area": "indoor", "capacity": 2},
        headers=staff_auth_headers,
    )
    second = await async_client.post(
        "/api/staff/branches/tables",
        json={"branch_id": 1, "label": "__route_dup_b", "seating_area": "indoor", "capacity": 2},
        headers=staff_auth_headers,
    )
    assert first.status_code == 200 and second.status_code == 200
    a_id, b_id = first.json()["id"], second.json()["id"]

    clash = await async_client.patch(
        f"/api/staff/branches/tables/{b_id}",
        json={"label": "__route_dup_a"},
        headers=staff_auth_headers,
    )
    assert clash.status_code == 409
    assert "__route_dup_a" in clash.json()["detail"]

    # B keeps its own name, and editing another field while echoing that name
    # back (which is what the admin page does) still goes through -- otherwise a
    # branch that already held duplicates would be frozen.
    kept = await async_client.patch(
        f"/api/staff/branches/tables/{b_id}",
        json={"label": "__route_dup_b", "capacity": 8},
        headers=staff_auth_headers,
    )
    assert kept.status_code == 200
    assert kept.json()["label"] == "__route_dup_b"
    assert kept.json()["capacity"] == 8

    async with transactional_session() as session:
        await session.execute(delete(Table).where(Table.id.in_([a_id, b_id])))


async def test_staff_delete_table_success(async_client, staff_auth_headers):
    """A table nothing ever booked is removed outright."""
    created = await async_client.post(
        "/api/staff/branches/tables",
        json={"branch_id": 1, "label": "__route_del", "seating_area": "indoor", "capacity": 4},
        headers=staff_auth_headers,
    )
    assert created.status_code == 200
    table_id = created.json()["id"]

    deleted = await async_client.delete(
        f"/api/staff/branches/tables/{table_id}", headers=staff_auth_headers
    )
    assert deleted.status_code == 204

    listed = await async_client.get(
        "/api/staff/branches/tables", headers=staff_auth_headers
    )
    assert all(t["id"] != table_id for t in listed.json())

    # Gone for good: a second attempt is a 404, not another 204.
    again = await async_client.delete(
        f"/api/staff/branches/tables/{table_id}", headers=staff_auth_headers
    )
    assert again.status_code == 404


async def test_staff_delete_table_with_bookings_is_refused(async_client, staff_auth_headers):
    """A booked table is protected -- the FK cascades, so deleting erases history."""
    created = await async_client.post(
        "/api/staff/branches/tables",
        json={"branch_id": 1, "label": "__route_del_booked", "seating_area": "indoor", "capacity": 2},
        headers=staff_auth_headers,
    )
    assert created.status_code == 200
    table_id = created.json()["id"]

    start = datetime.now(timezone.utc) + timedelta(days=30)
    end = start + timedelta(minutes=90)
    async with transactional_session() as session:
        session.add(
            Reservation(
                customer_id=1,
                branch_id=1,
                table_id=table_id,
                party_size=2,
                start_time=start,
                end_time=end,
                duration_minutes=90,
                status="confirmed",
                booking_window=func.tstzrange(start, end, "[)"),
            )
        )

    refused = await async_client.delete(
        f"/api/staff/branches/tables/{table_id}", headers=staff_auth_headers
    )
    assert refused.status_code == 409
    assert "停用" in refused.json()["detail"]

    listed = await async_client.get(
        "/api/staff/branches/tables", headers=staff_auth_headers
    )
    assert any(t["id"] == table_id for t in listed.json())

    # Cleanup: drop the booking, then the table (the API refuses while it exists).
    async with transactional_session() as session:
        await session.execute(delete(Reservation).where(Reservation.table_id == table_id))
        await session.execute(delete(Table).where(Table.id == table_id))


async def test_staff_delete_table_not_found(async_client, staff_auth_headers):
    resp = await async_client.delete(
        "/api/staff/branches/tables/999999", headers=staff_auth_headers
    )
    assert resp.status_code == 404


async def test_staff_delete_table_forbidden_for_other_branch(async_client):
    """A branch-bound staff member may not delete another branch's table."""
    listed = await async_client.get(
        "/api/staff/branches/tables?branch_id=2", headers=_staff_headers(1, role="admin")
    )
    other_branch_table = listed.json()[0]["id"]

    resp = await async_client.delete(
        f"/api/staff/branches/tables/{other_branch_table}",
        headers=_staff_headers(2, role="staff"),  # scoped to branch 1
    )
    assert resp.status_code == 403


async def test_staff_delete_table_requires_auth(async_client):
    resp = await async_client.delete("/api/staff/branches/tables/1")
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Staff menu / inventory
# --------------------------------------------------------------------------- #


async def test_staff_menu_categories(async_client, staff_auth_headers):
    resp = await async_client.get("/api/staff/menu/categories", headers=staff_auth_headers)
    assert resp.status_code == 200
    assert resp.json()


async def test_staff_menu_create_item_missing_category(async_client, staff_auth_headers):
    resp = await async_client.post(
        "/api/staff/menu/items",
        json={"category_id": 999999, "name": "x", "base_price": 1.0},
        headers=staff_auth_headers,
    )
    assert resp.status_code == 404


async def test_staff_menu_update_item_not_found(async_client, staff_auth_headers):
    resp = await async_client.patch(
        "/api/staff/menu/items/999999", json={"name": "x"}, headers=staff_auth_headers
    )
    assert resp.status_code == 404


async def test_staff_menu_stock_negative(async_client, staff_auth_headers):
    resp = await async_client.put(
        "/api/staff/menu/stock",
        json={"menu_item_id": 1, "branch_id": 1, "stock_quantity": -1},
        headers=staff_auth_headers,
    )
    assert resp.status_code == 400


async def test_staff_menu_stock_forbidden_for_other_branch(async_client):
    resp = await async_client.put(
        "/api/staff/menu/stock",
        json={"menu_item_id": 1, "branch_id": 2, "stock_quantity": 5},
        headers=_staff_headers(2, role="staff"),
    )
    assert resp.status_code == 403


async def test_staff_menu_create_item_and_update(async_client, staff_auth_headers):
    # The category is created through the Service (the POST /categories endpoint
    # hits the same pre-existing lazy-load bug as POST /branches, so it is out of
    # this task's scope); the item create/update endpoints are exercised directly.
    category = await MenuService.create_category(_ADMIN, 1, "__route_test_cat")
    try:
        item_resp = await async_client.post(
            "/api/staff/menu/items",
            json={
                "category_id": category.id,
                "name": "__route_test_item",
                "base_price": 2.0,
                "is_available": True,
                "initial_stock": 7,
                "variants": [
                    {"variant_type": "size", "variant_name": "L", "price_delta": 1.0}
                ],
            },
            headers=staff_auth_headers,
        )
        assert item_resp.status_code == 200
        body = item_resp.json()
        assert body["stock_quantity"] == 7
        assert body["effective_available"] is True

        patch = await async_client.patch(
            f"/api/staff/menu/items/{body['id']}",
            json={"is_available": False},
            headers=staff_auth_headers,
        )
        assert patch.status_code == 200
        assert patch.json()["effective_available"] is False
    finally:
        async with transactional_session() as session:
            await session.execute(
                delete(MenuCategory).where(MenuCategory.id == category.id)
            )
