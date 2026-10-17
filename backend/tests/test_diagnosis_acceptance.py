"""Task29: cross-cutting acceptance checks.

Every other diagnosis test module owns one capability. This one sits at the
seams between them and turns the acceptance checklist into executable
assertions, so a future refactor cannot satisfy the feature tests while quietly
breaking the baseline they were accepted against:

* the **golden payload** of a deterministic fall -- structure, threshold values,
  finding list and their numbers, and evidence refs that all resolve;
* the **§D four-branch invariant** from the field record -- of four visible
  branches only one is comparable, the other three carry no percentage at all,
  and the summary never claims a universal trend;
* the **cost bounds** (``MAX_DISH_DAILY_ITEMS`` / ``MAX_PEER_BRANCHES``) are
  positive and the tests that exercise them live beside their features. The
  remaining checklist items -- architecture boundaries, no model call for a
  quiet period, rule fallback, bidirectional dish movements -- are verified by
  running the modules listed in §P of the acceptance record.
"""

from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4

from sqlalchemy import update

from backend.app.database import transactional_session
from backend.app.models.branch import Branch
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.order import Order
from backend.app.models.user import Customer, StaffUser
from backend.app.service.branch import BranchService
from backend.app.service.branch_peers import (
    PEER_INSUFFICIENT_BASELINE,
    PEER_NO_DATA,
    PEER_OK,
    PEER_ZERO_BASELINE,
    BranchPeerService,
)
from backend.app.service.diagnostics import DiagnosisService, resolve_window
from backend.app.service.order import OrderService


_END = datetime.now(timezone.utc).date() - timedelta(days=1)
_START = _END - timedelta(days=3)  # four-day current window
_BASE_START, _BASE_END = resolve_window(_START, _END)[2:]

_ADMIN = StaffUser(username="__accept_admin", role="admin", branch_id=None)


# --------------------------------------------------------------------------- #
# Scenario builders (shared fixture data, real orders)
# --------------------------------------------------------------------------- #
async def _make_branch() -> int:
    async with transactional_session() as s:
        branch = Branch(
            name=f"__accept{uuid4().hex[:8]}",
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
            name="__accept", email=f"__accept_{uuid4().hex}@example.com",
            password_hash="x",
        )
        s.add(customer)
        await s.flush()
        return customer.id


async def _make_item(branch_id: int) -> int:
    async with transactional_session() as s:
        category = MenuCategory(branch_id=branch_id, name=f"__accept{uuid4().hex[:8]}")
        s.add(category)
        await s.flush()
        item = MenuItem(
            category_id=category.id, name="__accept_item", base_price=10.0,
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


async def _place(branch_id: int, customer_id: int, item_id: int, *, day: date) -> None:
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


async def _seed(branch_id, customer_id, item_id, start, end, *, per_day):
    day = start
    while day <= end:
        for _ in range(per_day):
            await _place(branch_id, customer_id, item_id, day=day)
        day += timedelta(days=1)


# --------------------------------------------------------------------------- #
# Clause 1: the golden payload
# --------------------------------------------------------------------------- #
async def test_the_golden_payload_is_structurally_stable():
    """The deterministic fall the baseline was accepted against, pinned whole.

    Twelve orders across four baseline days become four across four current
    days; one dish carries the same fall. The assertion is deliberately exact on
    the parts the system promises (structure, thresholds, numbers, refs) and
    silent on model-generated text, which the baseline never locked.
    """
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id)
    await _seed(branch_id, customer_id, item_id, _BASE_START, _BASE_END, per_day=3)
    await _seed(branch_id, customer_id, item_id, _START, _END, per_day=1)

    body = await DiagnosisService.diagnose(branch_id, _START, _END)

    # 1. Top-level contract: no field may appear or vanish unnoticed.
    assert set(body) == {
        "branch_id",
        "branch_name",
        "status",
        "notes",
        "current_period",
        "baseline_period",
        "thresholds",
        "findings",
    }
    assert body["status"] == "ok"
    assert body["current_period"] == {
        "start_date": _START,
        "end_date": _END,
        "order_count": 4,
        "cancelled_count": 0,
        "revenue": 40.0,
        "average_order_value": 10.0,
        "units_sold": 4,
        "active_days": 4,
    }
    assert body["baseline_period"] == {
        "start_date": _BASE_START,
        "end_date": _BASE_END,
        "order_count": 12,
        "cancelled_count": 0,
        "revenue": 120.0,
        "average_order_value": 10.0,
        "units_sold": 12,
        "active_days": 4,
    }
    assert body["thresholds"] == {
        "change_threshold_pct": 30.0,
        "critical_change_pct": 60.0,
        "cancellation_rate_delta_pp": 5.0,
        "critical_cancellation_rate_pp": 15.0,
        "min_baseline_days": 3,
    }
    assert body["notes"] == []

    # 2. The same move on volume, revenue and the dish; AOV is unchanged and the
    #    cancellation rate did not move, so neither may be reported.
    assert [
        (f["metric"], f["direction"], f["severity"])
        for f in body["findings"]
    ] == [
        ("order_count", "down", "critical"),
        ("revenue", "down", "critical"),
        ("dish_sales", "down", "critical"),
    ]
    assert [round(f["change_pct"], 2) for f in body["findings"]] == [
        -66.67,
        -66.67,
        -66.67,
    ]
    assert [f["current"] for f in body["findings"]] == [4, 40.0, 4]
    assert [f["previous"] for f in body["findings"]] == [12, 120.0, 12]

    # 3. Every evidence ref resolves, is unique, and follows the fN.eM scheme
    #    the Agent's citation guard depends on.
    refs = [e["ref"] for f in body["findings"] for e in f["evidence"]]
    assert len(refs) == len(set(refs))
    for index, finding in enumerate(body["findings"]):
        for offset, evidence in enumerate(finding["evidence"]):
            assert evidence["ref"] == f"f{index}.e{offset}"

    # 4. The dish finding carries the Task24/25 daily series and its shape.
    dish = body["findings"][2]
    assert any(e["label"].endswith("逐日走势") for e in dish["evidence"])
    series = next(e["value"] for e in dish["evidence"] if isinstance(e["value"], list))
    assert series == [1, 1, 1, 1]


# --------------------------------------------------------------------------- #
# Clause 2: the §D four-branch invariant
# --------------------------------------------------------------------------- #
async def _branch(customer_id: int, *, current=0, baseline=0, current_days=4,
                  baseline_days=4) -> int:
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


async def test_the_field_four_branch_scenario_yields_exactly_one_comparable(
    monkeypatch,
):
    """§D restated: one target plus three peers with an unusable baseline.

    The field record's warning was that a naive cross-branch read shows a
    ``-68.4%`` from a branch with two baseline days. Here three branches are
    deliberately unable to support a percentage for three different reasons --
    thin baseline, no record, zero baseline -- and only the target is comparable.
    """
    customer_id = await _make_customer()
    target = await _branch(customer_id, current=1, baseline=1)
    thin = await _branch(customer_id, current=1, baseline=2, baseline_days=2)
    silent = await _branch(customer_id, current=0, baseline=1)
    zero = await _branch(customer_id, current=1, baseline=0)

    all_visible = await BranchService.list_branches(_ADMIN)
    wanted = {target, thin, silent, zero}
    branches = [branch for branch in all_visible if branch.id in wanted]

    async def _fake_list(_staff, *, session=None):
        return branches

    monkeypatch.setattr(
        "backend.app.service.branch_peers.BranchService.list_branches", _fake_list
    )
    result = await BranchPeerService.compare_branch_peers(
        target, _START, _END, staff=_ADMIN
    )

    assert result["target"]["status"] == PEER_OK
    assert result["comparable_count"] == 1
    assert result["incomparable_count"] == 3
    assert {peer["branch_id"]: peer["status"] for peer in result["peers"]} == {
        thin: PEER_INSUFFICIENT_BASELINE,
        silent: PEER_NO_DATA,
        zero: PEER_ZERO_BASELINE,
    }
    # The whole point: no peer carries a number that could be read as a drop.
    assert all(peer["change_pct"] is None for peer in result["peers"])
    assert "%" not in result["summary"]
    assert "普遍" not in result["summary"].replace("不判断变化是否普遍", "")
