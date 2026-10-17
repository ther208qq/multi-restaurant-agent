"""T1 tests: deterministic and, above all, *honest* anomaly detection.

``sdd/经营异常诊断Agent审计与Tasks.md`` §T1. The value of this layer is not that
it can compute a percentage change -- that is arithmetic. It is that it refuses
to report one when the data cannot support it. These tests pin down both halves:

* a real, sustained fall is reported, with the window it was measured over;
* an empty baseline, a baseline too thin to be a trend, and a dish that merely
  fell out of a capped ranking are all reported as **unjudged**, never as a 100%
  collapse.

The scenario builder backdates ``orders.created_at`` the same way
``test_stats.py`` does (``UPDATE orders SET created_at = ...``), because the seed
ships no orders at all -- everything here has to be constructed.
"""

import json
from datetime import datetime, time, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import update

from backend.app.agent.diagnosis import (
    ANALYSIS_INSUFFICIENT,
    ANALYSIS_NO_FINDINGS,
    ANALYSIS_OK,
    FORBIDDEN_CAUSE_KEYWORDS,
    HYPOTHESIS_PREFIX,
    SOURCE_LLM,
    SOURCE_RULE,
    analyze_diagnosis,
)
from backend.app.auth.security import create_access_token
from backend.app.database import transactional_session
from backend.app.exceptions import ForbiddenError
from backend.app.models.audit import AuditLog
from backend.app.models.branch import Branch
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.models.order import Order
from backend.app.models.user import Customer, StaffUser
from backend.app.service.diagnostics import (
    MAX_DISH_DAILY_ITEMS,
    TREND_DIFFUSE,
    TREND_GRADUAL,
    TREND_GRADUAL_UP,
    TREND_SINGLE_DAY,
    TREND_SINGLE_DAY_UP,
    DiagnosisService,
    DishDailyService,
)
from backend.app.service.order import OrderService

# Both windows sit entirely in the past, so the "current window is still running"
# annotation never interferes with what these tests are actually asserting.
_TODAY = datetime.now(timezone.utc).date()
_CUR_END = _TODAY - timedelta(days=1)
_CUR_START = _CUR_END - timedelta(days=6)
_BASE_END = _CUR_START - timedelta(days=1)
_BASE_START = _BASE_END - timedelta(days=6)

# A second, shorter window: the seven-day default cannot produce a fall past the
# 30% threshold out of a single bad day, so the shape tests need three.
_SHAPE_END = _TODAY - timedelta(days=1)
_SHAPE_START = _SHAPE_END - timedelta(days=2)
_SHAPE_BASE_END = _SHAPE_START - timedelta(days=1)
_SHAPE_BASE_START = _SHAPE_BASE_END - timedelta(days=2)

# A branch-bound staffer for the branch-scope test. Only ``branch_id`` is read by
# the statistics service, so the row never needs to be persisted.
_STAFF_BRANCH_1 = StaffUser(username="__dx", role="staff", branch_id=1)

# Same, but unbound: it can act on any branch, which is what a freshly created
# test branch needs. Used only to attribute an audit row to a staff actor.
_UNSCOPED_STAFF = StaffUser(username="__dx_admin", role="admin", branch_id=None)


# --------------------------------------------------------------------------- #
# Scenario builders
# --------------------------------------------------------------------------- #
async def _make_branch() -> int:
    async with transactional_session() as s:
        branch = Branch(
            name=f"__dx{uuid4().hex[:8]}",
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
            name="__dx", email=f"__dx_{uuid4().hex}@example.com", password_hash="x"
        )
        s.add(customer)
        await s.flush()
        return customer.id


async def _make_item(branch_id: int, *, name: str, price: float = 10.0) -> int:
    async with transactional_session() as s:
        category = MenuCategory(branch_id=branch_id, name=f"__dx{uuid4().hex[:8]}")
        s.add(category)
        await s.flush()
        item = MenuItem(
            category_id=category.id, name=name, base_price=price, is_available=True
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


def _at(day) -> datetime:
    """Midday UTC on ``day`` -- safely inside the UTC bucket the aggregates use."""
    return datetime.combine(day, time(12, 0), tzinfo=timezone.utc)


async def _place(branch_id: int, customer_id: int, item_id: int, quantity: int = 1, *,
                 created_at: datetime = None) -> Order:
    """One order, optionally backdated into a comparison window."""
    order = await OrderService.place_order(
        customer_id, branch_id, "dine_in", [{"menu_item_id": item_id, "quantity": quantity}]
    )
    if created_at is not None:
        async with transactional_session() as s:
            await s.execute(
                update(Order).where(Order.id == order.id).values(created_at=created_at)
            )
    return order


def _days(start, end):
    """Every UTC day in ``[start, end]``."""
    day, out = start, []
    while day <= end:
        out.append(day)
        day += timedelta(days=1)
    return out


async def _backdate_cancellations(order_ids, moment):
    """Move cancellation audit rows into the window they belong to.

    ``audit_logs.created_at`` is a server default, so unlike ``orders.created_at``
    it cannot be passed at insert time. The rows are genuine; only their clock is
    nudged, which is what a cancellation on that day would have looked like.
    """
    async with transactional_session() as s:
        await s.execute(
            update(AuditLog)
            .where(
                AuditLog.entity_id.in_(order_ids),
                AuditLog.action == "order.status_changed",
            )
            .values(created_at=moment)
        )


async def _seed_window(branch_id, customer_id, item_id, start, end, *, per_day, quantity=1):
    """One order per day for ``per_day`` units across the whole window."""
    for day in _days(start, end):
        for _ in range(per_day):
            await _place(
                branch_id, customer_id, item_id, quantity, created_at=_at(day)
            )


async def _place_lines(branch_id, customer_id, lines, *, created_at=None) -> Order:
    """One backdated order carrying several dishes (``[(item_id, qty), ...]``)."""
    order = await OrderService.place_order(
        customer_id,
        branch_id,
        "dine_in",
        [{"menu_item_id": item_id, "quantity": qty} for item_id, qty in lines],
    )
    if created_at is not None:
        async with transactional_session() as s:
            await s.execute(
                update(Order)
                .where(Order.id == order.id)
                .values(created_at=created_at)
            )
    return order


def _metrics(result):
    return [f["metric"] for f in result["findings"]]


# --------------------------------------------------------------------------- #
# A real fall is reported
# --------------------------------------------------------------------------- #
async def test_reports_order_and_revenue_drop():
    """12 orders across four baseline days -> 4 across four current days."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_a")

    await _seed_window(
        branch_id, customer_id, item_id, _BASE_START, _BASE_START + timedelta(days=3),
        per_day=3,
    )
    await _seed_window(
        branch_id, customer_id, item_id, _CUR_START, _CUR_START + timedelta(days=3),
        per_day=1,
    )

    result = await DiagnosisService.diagnose(
        branch_id, _CUR_START, _CUR_END
    )

    assert result["status"] == "ok"
    assert result["current_period"]["order_count"] == 4
    assert result["baseline_period"]["order_count"] == 12
    assert result["baseline_period"]["active_days"] == 4

    metrics = _metrics(result)
    assert "order_count" in metrics
    assert "revenue" in metrics
    # The cancellation rate did not move, so it must not be reported.
    assert "cancelled_rate" not in metrics

    order_finding = next(f for f in result["findings"] if f["metric"] == "order_count")
    assert order_finding["direction"] == "down"
    assert order_finding["severity"] == "critical"  # -66.7% passes the 60% mark
    assert order_finding["change_pct"] == pytest.approx(-66.67, abs=0.01)


async def test_average_order_value_is_judged_on_its_own():
    """Same order count both windows, cheaper basket now: only AOV falls."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    dear = await _make_item(branch_id, name="__dx_dear", price=20.0)
    cheap = await _make_item(branch_id, name="__dx_cheap", price=5.0)

    await _seed_window(
        branch_id, customer_id, dear, _BASE_START, _BASE_START + timedelta(days=3),
        per_day=2,
    )
    await _seed_window(
        branch_id, customer_id, cheap, _CUR_START, _CUR_START + timedelta(days=3),
        per_day=2,
    )

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)
    metrics = _metrics(result)

    assert "average_order_value" in metrics
    assert "order_count" not in metrics  # 8 both windows
    aov = next(f for f in result["findings"] if f["metric"] == "average_order_value")
    assert aov["current"] == pytest.approx(5.0)
    assert aov["previous"] == pytest.approx(20.0)


async def test_reports_cancellation_rate_rise():
    """A quiet baseline (0% cancelled) against a current week at 2 of 10 placed."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_cancel")

    # Baseline: 2/day for five days, none cancelled -> a 0% cancellation rate.
    await _seed_window(
        branch_id, customer_id, item_id, _BASE_START, _BASE_START + timedelta(days=4),
        per_day=2,
    )
    # Current: the same volume, plus two of the ten placed get cancelled.
    for day in _days(_CUR_START, _CUR_START + timedelta(days=3)):
        for _ in range(2):
            await _place(branch_id, customer_id, item_id, created_at=_at(day))
    for day in (_CUR_START, _CUR_START + timedelta(days=1)):
        cancelled = await _place(branch_id, customer_id, item_id, created_at=_at(day))
        await OrderService.update_status(cancelled.id, "cancelled")

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    assert "cancelled_rate" in _metrics(result)
    finding = next(f for f in result["findings"] if f["metric"] == "cancelled_rate")
    assert finding["direction"] == "up"
    assert finding["previous"] == pytest.approx(0.0)
    # 2 cancellations out of 10 placed = 20 percentage points above a 0% base.
    assert finding["change_pct"] == pytest.approx(20.0, abs=0.01)
    assert finding["severity"] == "critical"  # past the 15-point mark


async def test_no_findings_when_the_move_stays_under_the_threshold():
    """10 -> 8 orders is a 20% dip: real, but not the 30% this layer reports."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_flat")

    await _seed_window(
        branch_id, customer_id, item_id, _BASE_START, _BASE_START + timedelta(days=4),
        per_day=2,
    )
    await _seed_window(
        branch_id, customer_id, item_id, _CUR_START, _CUR_START + timedelta(days=3),
        per_day=2,
    )

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    assert result["current_period"]["order_count"] == 8
    assert result["baseline_period"]["order_count"] == 10
    assert result["status"] == "ok"
    assert result["findings"] == []
    assert result["notes"] == []


# --------------------------------------------------------------------------- #
# Refusals: "cannot tell" must never render as "collapsed"
# --------------------------------------------------------------------------- #
async def test_both_windows_empty_is_reported_as_insufficient_not_as_a_collapse():
    """The state of every freshly seeded database (``seed.py`` creates no orders)."""
    branch_id = await _make_branch()

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    assert result["status"] == "insufficient_data"
    assert result["findings"] == []
    assert result["current_period"]["order_count"] == 0
    assert any("无法进行经营诊断" in note for note in result["notes"])


async def test_empty_baseline_is_refused_rather_than_read_as_a_total_fall():
    """Orders now, nothing before: -100% would be a fabrication."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_nobase")

    await _seed_window(
        branch_id, customer_id, item_id, _CUR_START, _CUR_START + timedelta(days=3),
        per_day=3,
    )

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    assert result["status"] == "insufficient_data"
    assert result["findings"] == []
    assert result["current_period"]["order_count"] == 12
    assert result["baseline_period"]["order_count"] == 0
    assert any("没有成交订单" in note for note in result["notes"])


async def test_thin_baseline_is_refused_even_though_it_has_orders():
    """Five orders on a single day describe one evening, not a normal week."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_thin")

    for _ in range(5):
        await _place(branch_id, customer_id, item_id, created_at=_at(_BASE_START))
    await _seed_window(
        branch_id, customer_id, item_id, _CUR_START, _CUR_START + timedelta(days=3),
        per_day=1,
    )

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    assert result["status"] == "insufficient_data"
    assert result["findings"] == []
    assert result["baseline_period"]["order_count"] == 5
    assert result["baseline_period"]["active_days"] == 1
    assert any("有效经营天数仅 1 天" in note for note in result["notes"])


async def test_dish_outside_the_baseline_ranking_is_unjudged_not_a_collapse():
    """The ranking is capped, so absence from it proves nothing."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    steady = await _make_item(branch_id, name="__dx_steady")
    newcomer = await _make_item(branch_id, name="__dx_newcomer")

    await _seed_window(
        branch_id, customer_id, steady, _BASE_START, _BASE_START + timedelta(days=3),
        per_day=3,
    )
    await _seed_window(
        branch_id, customer_id, steady, _CUR_START, _CUR_START + timedelta(days=3),
        per_day=1,
    )
    # Only ever sold in the current window.
    await _seed_window(
        branch_id, customer_id, newcomer, _CUR_START, _CUR_START + timedelta(days=1),
        per_day=1,
    )

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    dish_findings = [f for f in result["findings"] if f["metric"] == "dish_sales"]
    assert [f["menu_item_id"] for f in dish_findings] == [steady]
    assert any("__dx_newcomer" in note for note in result["notes"])
    assert any("无法判断其销量变化" in note for note in result["notes"])


async def test_an_orders_absence_from_the_current_ranking_is_also_unjudged():
    """The reverse direction: it may simply have slipped below the cut-off."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    steady = await _make_item(branch_id, name="__dx_steady2")
    dropped = await _make_item(branch_id, name="__dx_dropped")

    await _seed_window(
        branch_id, customer_id, steady, _BASE_START, _BASE_START + timedelta(days=3),
        per_day=3,
    )
    await _seed_window(
        branch_id, customer_id, dropped, _BASE_START, _BASE_START + timedelta(days=1),
        per_day=1,
    )
    await _seed_window(
        branch_id, customer_id, steady, _CUR_START, _CUR_START + timedelta(days=3),
        per_day=3,
    )

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    assert [f for f in result["findings"] if f["metric"] == "dish_sales"] == []
    assert any("__dx_dropped" in note for note in result["notes"])


# --------------------------------------------------------------------------- #
# Dish movements are bidirectional (Task21)
# --------------------------------------------------------------------------- #
async def test_a_dish_rise_is_reported_as_up_and_capped_at_warning():
    """4 -> 12 units is +200%: report it, but never call growth "critical"."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_rise")

    await _seed_window(
        branch_id, customer_id, item_id, _BASE_START, _BASE_END,
        per_day=1,
    )
    await _seed_window(
        branch_id, customer_id, item_id, _CUR_START, _CUR_END,
        per_day=3,
    )

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    finding = _finding_for(result, "dish_sales")
    assert finding["direction"] == "up"
    assert finding["severity"] == "warning"  # rises cannot escalate past warning
    assert finding["change_pct"] == pytest.approx(200.0, abs=0.01)
    shape = _evidence(finding, "「__dx_rise」逐日走势")
    assert shape["value"] == TREND_GRADUAL_UP
    series = _evidence(finding, "「__dx_rise」本期逐日销量")
    assert series["value"] == [3, 3, 3, 3, 3, 3, 3]


@pytest.mark.parametrize(
    ("current_per_day", "expected_direction", "expected_sign"),
    [(7, "up", 1), (3, "down", -1)],
)
async def test_dish_movements_are_symmetric_around_the_threshold(
    current_per_day, expected_direction, expected_sign
):
    """20 units -> 28 (+40%) and 20 -> 12 (-40%) are judged the same way."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_symmetric")

    await _seed_window(
        branch_id, customer_id, item_id, _BASE_START, _BASE_START + timedelta(days=3),
        per_day=5,
    )
    await _seed_window(
        branch_id, customer_id, item_id, _CUR_START, _CUR_START + timedelta(days=3),
        per_day=current_per_day,
    )

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    finding = _finding_for(result, "dish_sales")
    assert finding["direction"] == expected_direction
    assert finding["severity"] == "warning"
    assert finding["change_pct"] * expected_sign == pytest.approx(40.0, abs=0.01)


async def test_a_dish_fall_past_sixty_percent_stays_critical():
    """The alert level is asymmetric by design; the threshold is not."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_critical_fall")

    await _seed_window(
        branch_id, customer_id, item_id, _BASE_START, _BASE_START + timedelta(days=3),
        per_day=3,
    )
    await _seed_window(
        branch_id, customer_id, item_id, _CUR_START, _CUR_START + timedelta(days=3),
        per_day=1,
    )

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    finding = _finding_for(result, "dish_sales")
    assert finding["direction"] == "down"
    assert finding["severity"] == "critical"


@pytest.mark.parametrize("current_per_day", [6, 4])
async def test_dish_change_below_the_threshold_produces_no_finding(
    current_per_day,
):
    """20 -> 24 is +20% and 20 -> 16 is -20%: neither is worth a finding."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_small_move")

    await _seed_window(
        branch_id, customer_id, item_id, _BASE_START, _BASE_START + timedelta(days=3),
        per_day=5,
    )
    await _seed_window(
        branch_id, customer_id, item_id, _CUR_START, _CUR_START + timedelta(days=3),
        per_day=current_per_day,
    )

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    assert result["findings"] == []


async def test_dish_daily_is_not_queried_without_a_dish_finding(monkeypatch):
    """A quiet window must not pay for the per-dish series query (Task24)."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_steady")

    await _seed_window(
        branch_id, customer_id, item_id, _BASE_START, _BASE_END, per_day=2
    )
    await _seed_window(
        branch_id, customer_id, item_id, _CUR_START, _CUR_END, per_day=2
    )

    calls = []

    async def _spy(*args, **kwargs):
        calls.append(args)
        raise AssertionError("per-dish daily query ran without a dish finding")

    monkeypatch.setattr(DishDailyService, "get_dish_daily", _spy)
    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    assert result["findings"] == []
    assert calls == []


# --------------------------------------------------------------------------- #
# Per-dish daily series and shape (Task25)
# --------------------------------------------------------------------------- #
async def test_a_dish_surge_on_one_day_is_reported_as_a_single_day_surge():
    """15 -> 20 with the whole gain on the last day: 单日冲高."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_surge")

    await _seed_window(
        branch_id, customer_id, item_id, _SHAPE_BASE_START, _SHAPE_BASE_END,
        per_day=5,
    )
    days = _days(_SHAPE_START, _SHAPE_END)
    await _place(branch_id, customer_id, item_id, 5, created_at=_at(days[0]))
    await _place(branch_id, customer_id, item_id, 5, created_at=_at(days[1]))
    await _place(branch_id, customer_id, item_id, 10, created_at=_at(days[2]))

    result = await DiagnosisService.diagnose(
        branch_id, _SHAPE_START, _SHAPE_END
    )

    finding = _finding_for(result, "dish_sales")
    assert finding["direction"] == "up"
    assert finding["change_pct"] == pytest.approx(33.33, abs=0.01)
    assert _evidence(finding, "「__dx_surge」逐日走势")["value"] == (
        TREND_SINGLE_DAY_UP
    )
    assert _evidence(finding, "「__dx_surge」本期逐日销量")["value"] == [5, 5, 10]


async def test_a_dish_fall_on_one_day_is_reported_as_a_single_day_fall():
    """15 -> 10, all of the shortfall on the last day: 单日塌方."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_dish_dead")
    anchor = await _make_item(branch_id, name="__dx_dish_dead_anchor")

    await _seed_window(
        branch_id, customer_id, item_id, _SHAPE_BASE_START, _SHAPE_BASE_END,
        per_day=5,
    )
    days = _days(_SHAPE_START, _SHAPE_END)
    await _place(branch_id, customer_id, item_id, 5, created_at=_at(days[0]))
    await _place(branch_id, customer_id, item_id, 5, created_at=_at(days[1]))
    # A sale of another dish keeps the final day a business day with a real zero
    # for this dish, not a no-record day.
    await _place(branch_id, customer_id, anchor, 1, created_at=_at(days[2]))

    result = await DiagnosisService.diagnose(
        branch_id, _SHAPE_START, _SHAPE_END
    )

    finding = _finding_for(result, "dish_sales")
    assert finding["direction"] == "down"
    assert finding["change_pct"] == pytest.approx(-33.33, abs=0.01)
    assert _evidence(finding, "「__dx_dish_dead」逐日走势")["value"] == (
        TREND_SINGLE_DAY
    )
    assert _evidence(finding, "「__dx_dish_dead」本期逐日销量")["value"] == [5, 5, 0]


async def test_a_dish_series_separates_a_real_zero_from_a_missing_day():
    """A business day with no sale is 0; a day with no orders at all is null."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_zero_vs_null")
    anchor = await _make_item(branch_id, name="__dx_zero_vs_null_anchor")

    await _seed_window(
        branch_id, customer_id, item_id, _BASE_START, _BASE_END, per_day=3
    )
    days = _days(_CUR_START, _CUR_END)
    await _place(branch_id, customer_id, item_id, 3, created_at=_at(days[0]))
    await _place(branch_id, customer_id, anchor, 1, created_at=_at(days[2]))
    # Days 1 and 3..6 have no orders at all.

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    finding = _finding_for(result, "dish_sales")
    series_evidence = _evidence(finding, "「__dx_zero_vs_null」本期逐日销量")
    assert series_evidence["value"] == [3, None, 0, None, None, None, None]
    assert "null 表示当天没有任何订单记录" in series_evidence["detail"]
    assert "不代表该菜品销量为零" in series_evidence["detail"]
    assert "availability: unknown" in series_evidence["detail"]


async def test_a_dispersed_dish_fall_is_labelled_diffuse():
    """14 -> 9 spread evenly over several days, none of them dominant."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_diffuse")

    await _seed_window(
        branch_id, customer_id, item_id, _BASE_START, _BASE_END, per_day=2
    )
    days = _days(_CUR_START, _CUR_END)
    for day in (days[0], days[3], days[5]):
        await _place(branch_id, customer_id, item_id, 3, created_at=_at(day))

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    finding = _finding_for(result, "dish_sales")
    assert finding["change_pct"] == pytest.approx(-35.71, abs=0.01)
    assert _evidence(finding, "「__dx_diffuse」逐日走势")["value"] == TREND_DIFFUSE


async def test_dish_daily_cover_is_capped_at_the_request_limit():
    """Eleven dish findings: ten get a series, the eleventh says it did not."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    items = [
        await _make_item(branch_id, name=f"__dx_k{index:02d}")
        for index in range(MAX_DISH_DAILY_ITEMS + 1)
    ]

    for day in _days(_BASE_START, _BASE_END):
        await _place_lines(
            branch_id, customer_id, [(item, 3) for item in items],
            created_at=_at(day),
        )
    for day in _days(_CUR_START, _CUR_END):
        await _place_lines(
            branch_id, customer_id, [(item, 1) for item in items],
            created_at=_at(day),
        )

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    dish_findings = [f for f in result["findings"] if f["metric"] == "dish_sales"]
    assert len(dish_findings) == MAX_DISH_DAILY_ITEMS + 1
    enriched = [
        f
        for f in dish_findings
        if any(e["label"].endswith("本期逐日销量") for e in f["evidence"])
    ]
    unexpanded = [f for f in dish_findings if f not in enriched]
    assert len(enriched) == MAX_DISH_DAILY_ITEMS
    assert len(unexpanded) == 1
    assert any(
        "没有展开该菜品的逐日明细" in e["detail"]
        for e in unexpanded[0]["evidence"]
    )
    assert any(
        f"最多覆盖 {MAX_DISH_DAILY_ITEMS} 个菜品" in note
        for note in result["notes"]
    )


async def test_dish_shape_reaches_the_facts_and_checks(monkeypatch):
    """The classified dish shape is citable evidence, not prose."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_shape_fact")
    anchor = await _make_item(branch_id, name="__dx_shape_fact_anchor")

    await _seed_window(
        branch_id, customer_id, item_id, _SHAPE_BASE_START, _SHAPE_BASE_END,
        per_day=5,
    )
    days = _days(_SHAPE_START, _SHAPE_END)
    await _place(branch_id, customer_id, item_id, 5, created_at=_at(days[0]))
    await _place(branch_id, customer_id, item_id, 5, created_at=_at(days[1]))
    await _place(branch_id, customer_id, anchor, 1, created_at=_at(days[2]))

    diagnosis = await DiagnosisService.diagnose(
        branch_id, _SHAPE_START, _SHAPE_END
    )
    _stub_llm(monkeypatch, RuntimeError("connection refused"))
    analysis = await analyze_diagnosis(diagnosis)

    finding = _finding_for(diagnosis, "dish_sales")
    shape_ref = _evidence(finding, "「__dx_shape_fact」逐日走势")["ref"]
    assert any(
        "菜品销量的下降形态为「单日塌方」" in fact["text"]
        and shape_ref in fact["evidence_refs"]
        for fact in analysis["facts"]
    )
    assert any(
        "集中在单日" in check["text"] and shape_ref in check["evidence_refs"]
        for check in analysis["checks"]
    )


# --------------------------------------------------------------------------- #
# Shape and scope
# --------------------------------------------------------------------------- #
async def test_every_evidence_item_carries_a_unique_ref():
    """T3 may only cite evidence by ``ref``; an unresolvable ref is dropped."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_refs")

    await _seed_window(
        branch_id, customer_id, item_id, _BASE_START, _BASE_START + timedelta(days=3),
        per_day=3,
    )
    await _seed_window(
        branch_id, customer_id, item_id, _CUR_START, _CUR_START + timedelta(days=1),
        per_day=1,
    )

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    refs = [e["ref"] for f in result["findings"] for e in f["evidence"]]
    assert refs, "findings must carry evidence"
    assert len(refs) == len(set(refs))
    assert all(not f["evidence"] == [] for f in result["findings"])


async def test_current_window_including_today_is_flagged_as_incomplete():
    """A window that still contains today has only partly elapsed totals."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_partial")

    await _seed_window(
        branch_id, customer_id, item_id, _BASE_START, _BASE_START + timedelta(days=3),
        per_day=3,
    )
    await _place(branch_id, customer_id, item_id)

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _TODAY)

    assert result["current_period"]["end_date"] == _TODAY
    assert any("尚未过完" in note for note in result["notes"])


async def test_a_branch_bound_staffer_cannot_diagnose_another_branch():
    """Scope is inherited from StatsService -- this layer must not re-decide it."""
    other_branch = await _make_branch()

    with pytest.raises(ForbiddenError):
        await DiagnosisService.diagnose(
            other_branch, _CUR_START, _CUR_END, staff=_STAFF_BRANCH_1
        )


# --------------------------------------------------------------------------- #
# T2: evidence assembly
# --------------------------------------------------------------------------- #
def _evidence(finding, prefix):
    return next(e for e in finding["evidence"] if e["label"].startswith(prefix))


def _finding_for(result, metric):
    return next(f for f in result["findings"] if f["metric"] == metric)


async def test_a_single_dead_evening_is_told_apart_from_a_slow_week():
    """30 orders over three days -> 20, all of the loss on the last day.

    The total (-33%) says nothing about *how*; the daily series does, and the
    response has to carry that judgement rather than leave it to prose.
    """
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_dead")

    await _seed_window(
        branch_id, customer_id, item_id, _SHAPE_BASE_START, _SHAPE_BASE_END,
        per_day=10,
    )
    # Ten a day, then nothing at all on the final day.
    await _seed_window(
        branch_id, customer_id, item_id, _SHAPE_START,
        _SHAPE_START + timedelta(days=1), per_day=10,
    )

    result = await DiagnosisService.diagnose(branch_id, _SHAPE_START, _SHAPE_END)

    finding = _finding_for(result, "order_count")
    assert finding["change_pct"] == pytest.approx(-33.33, abs=0.01)
    assert _evidence(finding, "订单量逐日走势")["value"] == TREND_SINGLE_DAY
    # The raw series is shown too, with the silent day filled as a true zero.
    assert _evidence(finding, "本期订单量逐日明细")["value"] == [10.0, 10.0, 0.0]


async def test_a_week_of_thinning_traffic_is_reported_as_a_gradual_decline():
    """Same window, same -60%, but every day is down -- not one bad night."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_thin")

    await _seed_window(
        branch_id, customer_id, item_id, _SHAPE_BASE_START, _SHAPE_BASE_END,
        per_day=10,
    )
    await _seed_window(
        branch_id, customer_id, item_id, _SHAPE_START, _SHAPE_END, per_day=4
    )

    result = await DiagnosisService.diagnose(branch_id, _SHAPE_START, _SHAPE_END)

    finding = _finding_for(result, "order_count")
    assert _evidence(finding, "订单量逐日走势")["value"] == TREND_GRADUAL


async def test_average_order_value_carries_no_daily_shape():
    """A per-order average has no value on a day with no orders.

    Classifying its "shape" would mean inventing a 0 for those days, so the
    Service declines and the raw series is shown with ``null`` instead.
    """
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    dear = await _make_item(branch_id, name="__dx_dear2", price=20.0)
    cheap = await _make_item(branch_id, name="__dx_cheap2", price=5.0)

    await _seed_window(
        branch_id, customer_id, dear, _BASE_START, _BASE_START + timedelta(days=3),
        per_day=2,
    )
    await _seed_window(
        branch_id, customer_id, cheap, _CUR_START, _CUR_START + timedelta(days=3),
        per_day=2,
    )

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    finding = _finding_for(result, "average_order_value")
    assert _evidence(finding, "客单价逐日走势")["value"] is None
    assert "每单平均量" in _evidence(finding, "客单价逐日走势")["detail"]
    # The three days the window had no orders on are null, never 0.
    series = _evidence(finding, "本期客单价逐日明细")["value"]
    assert series[:4] == [5.0, 5.0, 5.0, 5.0]
    assert series[4:] == [None, None, None]


async def test_a_dish_fall_carries_its_own_daily_series_and_shape():
    """The finding carries the dish's daily series, not a "no data" placeholder."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_dishgap")

    await _seed_window(
        branch_id, customer_id, item_id, _BASE_START, _BASE_END,
        per_day=3,
    )
    await _seed_window(
        branch_id, customer_id, item_id, _CUR_START, _CUR_END,
        per_day=1,
    )

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    finding = _finding_for(result, "dish_sales")
    shape = _evidence(finding, "「__dx_dishgap」逐日走势")
    assert shape["value"] == TREND_GRADUAL
    series = _evidence(finding, "「__dx_dishgap」本期逐日销量")
    assert series["value"] == [1, 1, 1, 1, 1, 1, 1]
    assert "null 表示当天没有任何订单记录" in series["detail"]


async def test_cancellations_are_split_between_customer_and_staff():
    """The count is in the statistics; *who* is only in the audit trail."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_who")

    # Baseline: 2/day over five days, none cancelled -> a 0% rate.
    await _seed_window(
        branch_id, customer_id, item_id, _BASE_START, _BASE_START + timedelta(days=4),
        per_day=2,
    )
    # Current: eight orders that stick, on the window's first four days.
    await _seed_window(
        branch_id, customer_id, item_id, _CUR_START, _CUR_START + timedelta(days=3),
        per_day=2,
    )

    customer_orders = []
    for day in (_CUR_START, _CUR_START + timedelta(days=1)):
        order = await _place(branch_id, customer_id, item_id, created_at=_at(day))
        await OrderService.cancel_order(order.id, customer_id)
        customer_orders.append(order.id)

    staff_order = await _place(
        branch_id, customer_id, item_id, created_at=_at(_CUR_START + timedelta(days=2))
    )
    await OrderService.update_status(
        staff_order.id, "cancelled", staff=_UNSCOPED_STAFF
    )

    # Two of the three cancellations were the customer's own doing, one was staff.
    await _backdate_cancellations(
        customer_orders + [staff_order.id], _at(_CUR_START)
    )

    result = await DiagnosisService.diagnose(
        branch_id, _CUR_START, _CUR_END, staff=_UNSCOPED_STAFF
    )

    finding = _finding_for(result, "cancelled_rate")
    # 3 cancellations out of 11 placed = 27.27 points above a 0% base.
    assert finding["change_pct"] == pytest.approx(27.27, abs=0.01)
    attribution = _evidence(finding, "本期取消归因")
    assert attribution["value"] == {
        "customer": 2,
        "staff": 1,
        "unattributed": 0,
        "total": 3,
    }
    # The accounting difference is stated, not left for the reader to discover.
    assert "取消动作发生在本期" in attribution["detail"]


async def test_cancellation_attribution_states_the_gap_when_identity_is_missing():
    """No staff identity means no branch-scoped audit read -- say so, don't guess."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_anon")

    await _seed_window(
        branch_id, customer_id, item_id, _BASE_START, _BASE_START + timedelta(days=4),
        per_day=2,
    )
    await _seed_window(
        branch_id, customer_id, item_id, _CUR_START, _CUR_START + timedelta(days=3),
        per_day=2,
    )
    for day in (_CUR_START, _CUR_START + timedelta(days=1)):
        order = await _place(branch_id, customer_id, item_id, created_at=_at(day))
        await OrderService.cancel_order(order.id, customer_id)

    result = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)

    attribution = _evidence(_finding_for(result, "cancelled_rate"), "本期取消归因")
    assert attribution["value"] is None
    assert "员工身份" in attribution["detail"]


# --------------------------------------------------------------------------- #
# T3: the Agent's fact / inference / hypothesis analysis
# --------------------------------------------------------------------------- #
def _stub_llm(monkeypatch, answer):
    """Point the Agent's LLM seam at a canned answer, or at a failure."""
    calls = {"n": 0}

    async def _fake(*_args, **_kwargs):
        calls["n"] += 1
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr("backend.app.agent.diagnosis.llm_client.generate", _fake)
    return calls


async def _falling_branch():
    """A branch whose volume, revenue and one dish all fell by two thirds."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_model")
    await _seed_window(
        branch_id, customer_id, item_id, _BASE_START, _BASE_START + timedelta(days=3),
        per_day=3,
    )
    await _seed_window(
        branch_id, customer_id, item_id, _CUR_START, _CUR_START + timedelta(days=3),
        per_day=1,
    )
    return await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)


def _valid_ref(diagnosis):
    return diagnosis["findings"][0]["evidence"][0]["ref"]


def _all_refs(diagnosis):
    return {e["ref"] for f in diagnosis["findings"] for e in f["evidence"]}


async def test_a_quiet_period_never_reaches_the_model(monkeypatch):
    """Nothing to explain means nothing to ask -- the seam stays untouched."""
    branch_id = await _make_branch()
    customer_id = await _make_customer()
    item_id = await _make_item(branch_id, name="__dx_quiet")
    await _seed_window(
        branch_id, customer_id, item_id, _BASE_START, _BASE_START + timedelta(days=4),
        per_day=2,
    )
    await _seed_window(
        branch_id, customer_id, item_id, _CUR_START, _CUR_START + timedelta(days=3),
        per_day=2,
    )
    diagnosis = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)
    assert diagnosis["findings"] == []

    # Any call at all fails the test, so "no LLM when there is no anomaly" is
    # enforced rather than merely intended.
    calls = _stub_llm(monkeypatch, RuntimeError("the model must not be consulted"))
    analysis = await analyze_diagnosis(diagnosis)

    assert calls["n"] == 0
    assert analysis["status"] == ANALYSIS_NO_FINDINGS
    assert analysis["inferences"] == []
    assert analysis["hypotheses"] == []


async def test_a_refusal_is_not_talked_into_a_cause(monkeypatch):
    """An unjudgeable window has no cause to give, only a reason it is unjudgeable."""
    branch_id = await _make_branch()
    diagnosis = await DiagnosisService.diagnose(branch_id, _CUR_START, _CUR_END)
    assert diagnosis["status"] == "insufficient_data"

    calls = _stub_llm(
        monkeypatch,
        json.dumps({"inferences": [{"text": "客流下降。", "evidence_refs": ["f0.e0"]}]}),
    )
    analysis = await analyze_diagnosis(diagnosis)

    assert calls["n"] == 0
    assert analysis["status"] == ANALYSIS_INSUFFICIENT
    assert analysis["inferences"] == []
    assert analysis["hypotheses"] == []
    # The Service's own refusal is what the reader gets, verbatim.
    assert any("无法进行经营诊断" in item["text"] for item in analysis["checks"])


async def test_a_cause_citing_an_unknown_ref_is_dropped(monkeypatch):
    """A citation either resolves or the sentence it supports does not exist."""
    diagnosis = await _falling_branch()
    good = _valid_ref(diagnosis)
    _stub_llm(
        monkeypatch,
        json.dumps(
            {
                "inferences": [
                    {
                        "text": "订单量下滑期间取消率没有上升，流失更可能发生在上游。",
                        "evidence_refs": [good],
                    },
                    {"text": "客流可能整体转移了。", "evidence_refs": ["f9.e9"]},
                ],
                "checks": [],
            }
        ),
    )

    analysis = await analyze_diagnosis(diagnosis)

    assert analysis["source"] == SOURCE_LLM
    assert analysis["dropped"] == 1
    assert [c["text"] for c in analysis["inferences"]] == [
        "订单量下滑期间取消率没有上升，流失更可能发生在上游。"
    ]


async def test_a_cause_quoting_a_number_the_payload_does_not_contain_is_dropped(
    monkeypatch,
):
    """The model may cite a number, not invent one."""
    diagnosis = await _falling_branch()
    good = _valid_ref(diagnosis)
    _stub_llm(
        monkeypatch,
        json.dumps(
            {
                "inferences": [
                    {"text": "订单量下降了 45%。", "evidence_refs": [good]},
                ],
                "checks": [],
            }
        ),
    )

    analysis = await analyze_diagnosis(diagnosis)

    # Every inference was thrown away, so the rules answer instead.
    assert analysis["source"] == SOURCE_RULE
    assert analysis["inferences"]


async def test_a_cause_the_system_cannot_see_is_dropped(monkeypatch):
    """Weather is not in the database, so it is not a cause here."""
    diagnosis = await _falling_branch()
    good = _valid_ref(diagnosis)
    _stub_llm(
        monkeypatch,
        json.dumps(
            {
                "inferences": [{"text": "可能与近期天气有关。", "evidence_refs": [good]}],
                "checks": [],
            }
        ),
    )

    analysis = await analyze_diagnosis(diagnosis)

    assert analysis["source"] == SOURCE_RULE
    assert all(
        not any(word in c["text"] for word in FORBIDDEN_CAUSE_KEYWORDS)
        for c in analysis["inferences"]
    )


async def test_an_unreachable_model_falls_back_to_the_rules(monkeypatch):
    """The seam degrades; the feature does not."""
    diagnosis = await _falling_branch()
    _stub_llm(monkeypatch, RuntimeError("connection refused"))

    analysis = await analyze_diagnosis(diagnosis)

    assert analysis["source"] == SOURCE_RULE
    assert analysis["dropped"] == 0
    assert analysis["inferences"]
    # The rules do not speculate: no hypothesis survives a model outage.
    assert analysis["hypotheses"] == []
    # A fallback inference is still evidence-backed, so the UI can render it the same.
    known = _all_refs(diagnosis)
    assert all(
        ref in known for cause in analysis["inferences"] for ref in cause["evidence_refs"]
    )
    assert all(
        ref in known for check in analysis["checks"] for ref in check["evidence_refs"]
    )


async def test_the_facts_are_written_from_the_payload_not_the_model(monkeypatch):
    """A claim the model returns under ``facts`` is ignored, not rendered."""
    diagnosis = await _falling_branch()

    _stub_llm(
        monkeypatch, json.dumps({"inferences": [], "hypotheses": [], "checks": []})
    )
    rules_only = await analyze_diagnosis(diagnosis)

    _stub_llm(
        monkeypatch,
        json.dumps(
            {
                "facts": [{"text": "本店客流暴跌 90%。", "evidence_refs": []}],
                "inferences": [
                    {
                        "text": "订单量下滑期间取消率没有上升。",
                        "evidence_refs": [_valid_ref(diagnosis)],
                    }
                ],
                "checks": [],
            }
        ),
    )
    with_a_model = await analyze_diagnosis(diagnosis)

    assert rules_only["facts"] == with_a_model["facts"]
    assert rules_only["facts"], "the confirmed facts must not be empty"
    assert not any("90" in fact["text"] for fact in with_a_model["facts"])
    # Facts and inferences stay separate fields: a claim the model wrote under
    # either bucket is never rendered as a fact.
    assert {"facts", "inferences", "hypotheses", "checks"} <= set(with_a_model)


async def test_a_hypothesis_is_kept_but_marked_unverified(monkeypatch):
    """An unevidenced guess is allowed -- in its own bucket, labelled by us."""
    diagnosis = await _falling_branch()
    good = _valid_ref(diagnosis)
    _stub_llm(
        monkeypatch,
        json.dumps(
            {
                "inferences": [
                    {
                        "text": "订单量下滑期间取消率没有上升。",
                        "evidence_refs": [good],
                    }
                ],
                "hypotheses": [
                    {"text": "可能是附近新开了一家同类门店，分流了部分客源。"},
                    # A model that already prefixes its own guess must not end up
                    # with the label twice.
                    {"text": HYPOTHESIS_PREFIX + "可能是外卖平台页面出了短暂问题。"},
                ],
                "checks": [],
            }
        ),
    )

    analysis = await analyze_diagnosis(diagnosis)

    assert analysis["source"] == SOURCE_LLM
    assert [item["text"] for item in analysis["hypotheses"]] == [
        HYPOTHESIS_PREFIX + "可能是附近新开了一家同类门店，分流了部分客源。",
        HYPOTHESIS_PREFIX + "可能是外卖平台页面出了短暂问题。",
    ]
    # A hypothesis never borrows the authority of an evidence ref.
    assert all(item["evidence_refs"] == [] for item in analysis["hypotheses"])


async def test_a_hypothesis_citing_evidence_is_dropped(monkeypatch):
    """The bucket for guesses is defined by having no citations."""
    diagnosis = await _falling_branch()
    good = _valid_ref(diagnosis)
    _stub_llm(
        monkeypatch,
        json.dumps(
            {
                "inferences": [
                    {
                        "text": "订单量下滑期间取消率没有上升。",
                        "evidence_refs": [good],
                    }
                ],
                "hypotheses": [
                    {"text": "可能是附近新店分流。", "evidence_refs": [good]}
                ],
                "checks": [],
            }
        ),
    )

    analysis = await analyze_diagnosis(diagnosis)

    assert analysis["source"] == SOURCE_LLM
    assert analysis["dropped"] == 1
    assert analysis["hypotheses"] == []


async def test_a_hypothesis_blaming_weather_is_dropped(monkeypatch):
    """Labelling a guess unverified does not license an invisible cause."""
    diagnosis = await _falling_branch()
    good = _valid_ref(diagnosis)
    _stub_llm(
        monkeypatch,
        json.dumps(
            {
                "inferences": [
                    {
                        "text": "订单量下滑期间取消率没有上升。",
                        "evidence_refs": [good],
                    }
                ],
                "hypotheses": [{"text": "可能是因为周末天气不好。"}],
                "checks": [],
            }
        ),
    )

    analysis = await analyze_diagnosis(diagnosis)

    assert analysis["source"] == SOURCE_LLM
    assert analysis["dropped"] == 1
    assert analysis["hypotheses"] == []


async def test_a_hypothesis_quoting_an_unknown_number_is_dropped(monkeypatch):
    """Unverified is not unaccountable: the number whitelist still applies."""
    diagnosis = await _falling_branch()
    good = _valid_ref(diagnosis)
    _stub_llm(
        monkeypatch,
        json.dumps(
            {
                "inferences": [
                    {
                        "text": "订单量下滑期间取消率没有上升。",
                        "evidence_refs": [good],
                    }
                ],
                "hypotheses": [{"text": "可能是订单量一次性下降了 45%。"}],
                "checks": [],
            }
        ),
    )

    analysis = await analyze_diagnosis(diagnosis)

    assert analysis["source"] == SOURCE_LLM
    assert analysis["dropped"] == 1
    assert analysis["hypotheses"] == []


# --------------------------------------------------------------------------- #
# T4: the HTTP route
# --------------------------------------------------------------------------- #
async def _make_staff(branch_id=None, role="staff"):
    """A *persisted* staffer -- ``get_current_staff`` looks the token's id up."""
    async with transactional_session() as s:
        staff = StaffUser(
            username=f"__dx{uuid4().hex[:8]}",
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


async def test_the_route_requires_a_token(async_client):
    resp = await async_client.get("/api/staff/diagnosis?branch_id=1")
    assert resp.status_code == 401


async def test_the_route_refuses_another_branch(async_client):
    """Scope is the statistics service's rule, inherited rather than re-decided."""
    branch_id = await _make_branch()
    other_branch = await _make_branch()
    staff_id = await _make_staff(branch_id=branch_id)

    refused = await async_client.get(
        f"/api/staff/diagnosis?branch_id={other_branch}", headers=_headers(staff_id)
    )
    assert refused.status_code == 403
    assert "权限" in refused.json()["detail"]

    allowed = await async_client.get(
        f"/api/staff/diagnosis?branch_id={branch_id}", headers=_headers(staff_id)
    )
    assert allowed.status_code == 200


async def test_the_route_returns_the_judged_shape(async_client, staff_auth_headers,
                                                 monkeypatch):
    """A fall comes back whole: findings, their evidence, and the Agent's reading."""
    # The seam is stubbed to the rule path: these assertions are about the HTTP
    # contract, and they must not depend on an LLM endpoint being reachable.
    _stub_llm(monkeypatch, RuntimeError("no model in this test"))
    diagnosis = await _falling_branch()

    resp = await async_client.get(
        f"/api/staff/diagnosis?branch_id={diagnosis['branch_id']}"
        f"&start_date={_CUR_START.isoformat()}&end_date={_CUR_END.isoformat()}",
        headers=staff_auth_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {
        "branch_id",
        "branch_name",
        "status",
        "notes",
        "current_period",
        "baseline_period",
        "thresholds",
        "findings",
        "analysis",
    }
    assert body["status"] == "ok"
    assert body["findings"]
    assert body["analysis"]["status"] == ANALYSIS_OK
    assert body["analysis"]["facts"]
    assert body["analysis"]["source"] == SOURCE_RULE


async def test_every_finding_reaches_the_client_with_its_evidence(
    async_client, staff_auth_headers, monkeypatch
):
    """The refs the Agent cites must survive serialization, or citations break."""
    _stub_llm(monkeypatch, RuntimeError("no model in this test"))
    diagnosis = await _falling_branch()

    resp = await async_client.get(
        f"/api/staff/diagnosis?branch_id={diagnosis['branch_id']}"
        f"&start_date={_CUR_START.isoformat()}&end_date={_CUR_END.isoformat()}",
        headers=staff_auth_headers,
    )

    body = resp.json()
    refs = [e["ref"] for f in body["findings"] for e in f["evidence"]]
    assert refs and all(refs)
    assert len(refs) == len(set(refs))
    # Every citation the Agent made resolves against what the client received.
    for claim in body["analysis"]["inferences"] + body["analysis"]["checks"]:
        assert all(ref in refs for ref in claim["evidence_refs"])


async def test_the_route_answers_an_empty_window_with_200_not_500(
    async_client, staff_auth_headers
):
    """The state of a freshly seeded database must be a readable answer."""
    branch_id = await _make_branch()

    resp = await async_client.get(
        f"/api/staff/diagnosis?branch_id={branch_id}", headers=staff_auth_headers
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "insufficient_data"
    assert body["findings"] == []
    assert body["analysis"]["status"] == ANALYSIS_INSUFFICIENT
    assert body["analysis"]["inferences"] == []
    assert body["analysis"]["hypotheses"] == []
    # The reason is spelled out for the reader, not left as an empty page.
    assert any("无法进行经营诊断" in note for note in body["notes"])


async def test_the_route_rejects_a_malformed_date(async_client, staff_auth_headers):
    """Same contract as the stats route: the query parser rejects it, not a 500."""
    resp = await async_client.get(
        "/api/staff/diagnosis?branch_id=1&start_date=yesterday",
        headers=staff_auth_headers,
    )
    assert resp.status_code == 422
