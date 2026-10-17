"""Task27/28 tests: same-window peer comparison across visible branches.

The point of the capability is not "compute a percentage". It is that a branch
whose baseline cannot support a comparison is labelled -- and gets no percentage
at all -- so a naive cross-branch read (the ``-68.4%`` from the field notes)
cannot be produced by this service.

These tests work on a shared test database with other tests' branches in it, so
they assert the service's own accounting invariant (every judged branch is
counted once) and the verdict of the branches they created, rather than a fixed
global count.

Task28 adds the HTTP entry point beside the diagnosis rather than inside its
payload, so the last section pins the route's contract: no token, a branch-bound
staffer's explicit ``not_comparable``, scope refusal, and serialization.
"""

from datetime import datetime, time, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import update

from backend.app.auth.security import create_access_token
from backend.app.database import transactional_session
from backend.app.exceptions import ForbiddenError, NotFoundError
from backend.app.models.branch import Branch
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.order import Order
from backend.app.models.user import Customer, StaffUser
from backend.app.service.branch import BranchService
from backend.app.service.branch_peers import (
    MAX_PEER_BRANCHES,
    PEER_INSUFFICIENT_BASELINE,
    PEER_NO_DATA,
    PEER_NOT_COMPARABLE,
    PEER_OK,
    PEER_ZERO_BASELINE,
    BranchPeerService,
)
from backend.app.service.diagnostics import resolve_window
from backend.app.service.order import OrderService


_TODAY = datetime.now(timezone.utc).date()
_END = _TODAY - timedelta(days=1)
_START = _END - timedelta(days=3)  # four-day current window
_BASE_END = _START - timedelta(days=1)
_BASE_START = _BASE_END - timedelta(days=3)  # equal-length baseline

_ADMIN = StaffUser(username="__bp_admin", role="admin", branch_id=None)


# --------------------------------------------------------------------------- #
# Scenario builders
# --------------------------------------------------------------------------- #
async def _make_branch() -> int:
    async with transactional_session() as s:
        branch = Branch(
            name=f"__bp{uuid4().hex[:8]}",
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
            name="__bp", email=f"__bp_{uuid4().hex}@example.com", password_hash="x"
        )
        s.add(customer)
        await s.flush()
        return customer.id


async def _make_item(branch_id: int) -> int:
    async with transactional_session() as s:
        category = MenuCategory(branch_id=branch_id, name=f"__bp{uuid4().hex[:8]}")
        s.add(category)
        await s.flush()
        item = MenuItem(
            category_id=category.id, name="__bp_item", base_price=10.0,
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


async def _place(branch_id: int, customer_id: int, item_id: int, *, day) -> None:
    order = await OrderService.place_order(
        customer_id,
        branch_id,
        "dine_in",
        [{"menu_item_id": item_id, "quantity": 1}],
    )
    moment = datetime.combine(day, time(12, 0), tzinfo=timezone.utc)
    async with transactional_session() as s:
        await s.execute(
            update(Order).where(Order.id == order.id).values(created_at=moment)
        )


async def _seed(branch_id, customer_id, item_id, start, end, *, per_day):
    day = start
    while day <= end:
        for _ in range(per_day):
            await _place(branch_id, customer_id, item_id, day=day)
        day += timedelta(days=1)


async def _branch(customer_id: int, *, current=0, baseline=0, current_days=4,
                  baseline_days=4) -> int:
    """A branch with ``current`` orders/day and ``baseline`` orders/day.

    ``0`` in either direction is a genuine empty window: no orders are placed
    at all, which is exactly the state under test.
    """
    branch_id = await _make_branch()
    if current or baseline:
        item_id = await _make_item(branch_id)
        if current:
            await _seed(
                branch_id, customer_id, item_id, _START,
                _START + timedelta(days=current_days - 1), per_day=current,
            )
        if baseline:
            await _seed(
                branch_id, customer_id, item_id, _BASE_START,
                _BASE_START + timedelta(days=baseline_days - 1), per_day=baseline,
            )
    return branch_id


def _peer(result, branch_id):
    return next(peer for peer in result["peers"] if peer["branch_id"] == branch_id)


async def _pin_visible(monkeypatch, visible_ids):
    """Narrow the visible set to the branches a test built.

    The shared test database accumulates branches from other test files, so the
    branch listing is pinned here. The rows themselves are the real ORM objects
    returned by ``BranchService`` -- only the visible set is controlled, keeping
    the comparison (and the cap) deterministic.
    """
    all_visible = await BranchService.list_branches(_ADMIN)
    wanted = set(visible_ids)
    branches = [branch for branch in all_visible if branch.id in wanted]

    async def _fake_list(_staff, *, session=None):
        return branches

    monkeypatch.setattr(
        "backend.app.service.branch_peers.BranchService.list_branches", _fake_list
    )
    return branches


async def _compare(monkeypatch, target_id, visible_ids, *, staff=_ADMIN):
    await _pin_visible(monkeypatch, visible_ids)
    return await BranchPeerService.compare_branch_peers(
        target_id, _START, _END, staff=staff
    )


async def _make_staff(branch_id=None, role="staff") -> int:
    """A *persisted* staffer -- ``get_current_staff`` looks the token's id up."""
    async with transactional_session() as s:
        staff = StaffUser(
            username=f"__bp_staff{uuid4().hex[:8]}",
            password_hash="x",
            role=role,
            branch_id=branch_id,
        )
        s.add(staff)
        await s.flush()
        return staff.id


def _headers(staff_id, role="staff"):
    token = create_access_token(
        {"sub": str(staff_id), "type": "staff", "username": f"s{staff_id}", "role": role}
    )
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Verdicts
# --------------------------------------------------------------------------- #
async def test_a_thin_baseline_is_labelled_and_gets_no_percentage(monkeypatch):
    """Two active baseline days cannot produce a comparable percentage."""
    customer_id = await _make_customer()
    target = await _branch(customer_id, current=1, baseline=1)
    thin = await _branch(
        customer_id, current=1, baseline=2, baseline_days=2
    )

    result = await _compare(monkeypatch, target, [target, thin])

    peer = _peer(result, thin)
    assert peer["status"] == PEER_INSUFFICIENT_BASELINE
    assert peer["change_pct"] is None
    assert peer["baseline_active_days"] == 2
    assert "2 天" in peer["verdict"]
    # The target itself is comparable, so the comparison is not refused.
    assert result["target"]["status"] == PEER_OK
    assert result["window"]["start_date"] == _START
    assert result["window"]["end_date"] == _END


async def test_zero_baseline_is_not_a_hundred_percent_drop(monkeypatch):
    """No baseline sales is undefined change, not a collapse."""
    customer_id = await _make_customer()
    target = await _branch(customer_id, current=1, baseline=1)
    empty_baseline = await _branch(customer_id, current=1, baseline=0)

    result = await _compare(monkeypatch, target, [target, empty_baseline])

    peer = _peer(result, empty_baseline)
    assert peer["status"] == PEER_ZERO_BASELINE
    assert peer["change_pct"] is None
    assert "上期没有成交订单" in peer["verdict"]


async def test_a_branch_with_no_record_this_window_is_no_data(monkeypatch):
    """A window with nothing in it is no data -- never -100%."""
    customer_id = await _make_customer()
    target = await _branch(customer_id, current=1, baseline=1)
    silent = await _branch(customer_id, current=0, baseline=1)

    result = await _compare(monkeypatch, target, [target, silent])

    peer = _peer(result, silent)
    assert peer["status"] == PEER_NO_DATA
    assert peer["change_pct"] is None
    assert "本期没有任何订单记录" in peer["verdict"]


async def test_a_comparable_peer_reports_its_order_change(monkeypatch):
    """Only a branch that passes the gate gets a percentage."""
    customer_id = await _make_customer()
    target = await _branch(customer_id, current=1, baseline=1)
    halved = await _branch(customer_id, current=1, baseline=2)

    result = await _compare(monkeypatch, target, [target, halved])

    peer = _peer(result, halved)
    assert peer["status"] == PEER_OK
    assert peer["change_pct"] == pytest.approx(-50.0, abs=0.01)
    assert peer["current_active_days"] == 4
    assert peer["baseline_active_days"] == 4


# --------------------------------------------------------------------------- #
# Accounting and scope
# --------------------------------------------------------------------------- #
async def test_the_target_is_not_repeated_and_every_judged_branch_is_counted(
    monkeypatch,
):
    customer_id = await _make_customer()
    target = await _branch(customer_id, current=1, baseline=1)
    peer_branch = await _branch(customer_id, current=1, baseline=2)

    result = await _compare(monkeypatch, target, [target, peer_branch])

    # Identity and verdict only: the target's totals live in the diagnosis
    # payload, not here.
    assert set(result["target"]) == {"branch_id", "branch_name", "status"}
    assert all(peer["branch_id"] != target for peer in result["peers"])
    # Counts cover the target too, and the cap (if any) is stated in a note.
    assert result["comparable_count"] + result["incomparable_count"] == (
        len(result["peers"]) + 1
    )
    statuses = [result["target"]["status"]] + [
        peer["status"] for peer in result["peers"]
    ]
    assert result["comparable_count"] == statuses.count(PEER_OK)
    # No universality judgement is made from a cross-branch count.
    assert "普遍" not in result["summary"].replace("不判断变化是否普遍", "")


async def test_a_branch_bound_staff_cannot_pretend_a_peer_comparison_exists():
    """One visible branch is ``not_comparable``, said out loud."""
    customer_id = await _make_customer()
    own_branch = await _branch(customer_id, current=1, baseline=1)
    staff = StaffUser(username="__bp_scoped", role="staff", branch_id=own_branch)

    result = await BranchPeerService.compare_branch_peers(
        None, _START, _END, staff=staff
    )

    assert result["status"] == PEER_NOT_COMPARABLE
    assert result["peers"] == []
    assert result["target"]["status"] == PEER_NOT_COMPARABLE
    assert result["comparable_count"] == 0
    assert result["incomparable_count"] == 1
    assert "只能看到本店" in result["summary"]


async def test_an_out_of_scope_branch_is_refused_before_any_comparison():
    customer_id = await _make_customer()
    own_branch = await _branch(customer_id, current=1, baseline=1)
    other_branch = await _branch(customer_id, current=1, baseline=1)
    staff = StaffUser(username="__bp_scoped", role="staff", branch_id=own_branch)

    with pytest.raises(ForbiddenError):
        await BranchPeerService.compare_branch_peers(
            other_branch, _START, _END, staff=staff
        )


async def test_a_missing_target_is_a_not_found_not_an_empty_comparison():
    with pytest.raises(NotFoundError):
        await BranchPeerService.compare_branch_peers(
            2_000_000_000, _START, _END, staff=_ADMIN
        )


async def test_the_peer_cover_is_bounded_and_says_so(monkeypatch):
    """The cost bound is explicit: the target survives, the rest is noted."""
    customer_id = await _make_customer()
    # Enough branches that the visible set exceeds the cap.
    extras = [await _make_branch() for _ in range(MAX_PEER_BRANCHES + 2)]
    target = await _branch(customer_id, current=1, baseline=1)

    result = await _compare(monkeypatch, target, [target] + extras)

    assert len(result["peers"]) + 1 == MAX_PEER_BRANCHES
    assert result["comparable_count"] + result["incomparable_count"] == MAX_PEER_BRANCHES
    assert any("未纳入判定与计数" in note for note in result["notes"])
    assert result["target"]["branch_id"] == target


def test_the_window_pair_is_the_diagnosis_window_pair():
    """One derivation, not a copy: the same helper the diagnosis calls."""
    assert resolve_window(_START, _END) == (
        _START, _END, _BASE_START, _BASE_END
    )


# --------------------------------------------------------------------------- #
# Task28: the HTTP entry point
# --------------------------------------------------------------------------- #
async def test_the_peers_route_requires_a_token(async_client):
    resp = await async_client.get("/api/staff/diagnosis/peers?branch_id=1")
    assert resp.status_code == 401


async def test_the_peers_route_says_not_comparable_for_a_branch_bound_staff(
    async_client,
):
    """The route must not turn "one visible branch" into an empty success."""
    customer_id = await _make_customer()
    own_branch = await _branch(customer_id, current=1, baseline=1)
    staff_id = await _make_staff(branch_id=own_branch)

    resp = await async_client.get(
        f"/api/staff/diagnosis/peers?start_date={_START.isoformat()}"
        f"&end_date={_END.isoformat()}",
        headers=_headers(staff_id),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == PEER_NOT_COMPARABLE
    assert body["target"]["branch_id"] == own_branch
    assert body["target"]["status"] == PEER_NOT_COMPARABLE
    assert body["peers"] == []
    assert body["comparable_count"] == 0
    assert body["incomparable_count"] == 1
    assert "只能看到本店" in body["summary"]


async def test_the_peers_route_refuses_another_branch(async_client):
    branch_id = await _make_branch()
    other_branch = await _make_branch()
    staff_id = await _make_staff(branch_id=branch_id)

    resp = await async_client.get(
        f"/api/staff/diagnosis/peers?branch_id={other_branch}",
        headers=_headers(staff_id),
    )

    assert resp.status_code == 403
    assert "权限" in resp.json()["detail"]


async def test_the_peers_route_serializes_the_judged_window_and_peers(
    async_client, staff_auth_headers, monkeypatch
):
    """An admin gets the shape: ISO dates, one entry per non-target branch."""
    customer_id = await _make_customer()
    target = await _branch(customer_id, current=1, baseline=1)
    halved = await _branch(customer_id, current=1, baseline=2)
    await _pin_visible(monkeypatch, [target, halved])

    resp = await async_client.get(
        f"/api/staff/diagnosis/peers?branch_id={target}"
        f"&start_date={_START.isoformat()}&end_date={_END.isoformat()}",
        headers=staff_auth_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == PEER_OK
    assert body["window"] == {
        "start_date": _START.isoformat(),
        "end_date": _END.isoformat(),
        "baseline_start_date": _BASE_START.isoformat(),
        "baseline_end_date": _BASE_END.isoformat(),
    }
    peer = _peer(body, halved)
    assert peer["status"] == PEER_OK
    assert peer["change_pct"] == pytest.approx(-50.0, abs=0.01)
    # The target's numbers are not duplicated here, and the counts still cover
    # every judged branch including it.
    assert set(body["target"]) == {"branch_id", "branch_name", "status"}
    assert body["comparable_count"] + body["incomparable_count"] == (
        len(body["peers"]) + 1
    )
