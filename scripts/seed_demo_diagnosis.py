"""Seed a self-checking demo dataset for the store-anomaly diagnosis (dev only).

Why a generated dataset instead of imported real-world sales data: the diagnosis
decides nothing interesting in the middle of its range, only at the edges -- a
baseline with too few active days, a day that sold nothing at all, a dish that
dropped out of the ranking, a ratio that is undefined on an empty day. Public
datasets never land on those edges, so they would test the happy path and nothing
else. This script builds the edges deliberately, and then asks the real
``DiagnosisService`` what it makes of them, so every number below is checkable
against a value this script can compute.

What it writes (two branches, both named with the ``演示门店`` prefix):

  演示门店·诊断       an unambiguous decline, to be detected and explained
  演示门店·基线不足   too thin a baseline, to be refused rather than judged

Everything is dated relative to today in UTC -- the same day boundary the
statistics layer uses -- against the window the 经营诊断 tab opens with, so the
demo works the moment the tab loads without anyone picking a date. The layout:

                     上期 (today-13 .. today-7)   本期 (today-6 .. today)
  有效经营天数         7 天                          6 天（today-4 一张单都没有）
  成交订单             70                            20
  取消订单             1                             4
  销售额               ¥1456                         ¥324

The edges it pins down:

  * 订单量 -71.4% / 销售额 -77.7%  -> 两条 critical
  * 客单价 -22.1%, 在 30% 阈值内    -> 不得出现在结论里（不是所有指标都该跟着跌）
  * 冰镇酸梅汤 -28.6%, 同样在阈值内  -> 不得出现
  * 招牌牛肉面 / 菌菇汤面 / 凉拌黄瓜 -> 三条 critical
  * 手工饺子 只在上期榜内            -> 只能出「无法判定」说明，绝不能报成 -100%
  * 取消归因 顾客 2 / 员工 1 / 未归属 1 -> 归因必须分得开
  * 演示门店·基线不足 上期只有 2 天   -> 必须拒绝判定

It also inserts ONE row shape the API writes but a seed otherwise would not: the
staff cancellation carries no ``by`` key and the system one has no actor at all,
so the third attribution bucket is exercised too.

The LLM is never called here -- the script runs the deterministic Service only,
so seeding costs nothing. Use the 经营诊断 tab (or curl) to see the Agent's
analysis on top of it.

    .venv/Scripts/python.exe scripts/seed_demo_diagnosis.py
    .venv/Scripts/python.exe scripts/seed_demo_diagnosis.py --rollback
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone

# Run as a plain script, so the repo root has to be importable for ``backend.app``.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete, select  # noqa: E402

from backend.app.database import AsyncSessionLocal  # noqa: E402
from backend.app.models.audit import AuditLog  # noqa: E402
from backend.app.models.branch import Branch  # noqa: E402
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock  # noqa: E402
from backend.app.models.order import Order, OrderItem  # noqa: E402
from backend.app.models.user import Customer, StaffUser  # noqa: E402
from backend.app.service.audit import actor_fields  # noqa: E402
from backend.app.service.diagnostics import (  # noqa: E402
    CHANGE_THRESHOLD_PCT,
    DEFAULT_WINDOW_DAYS,
    DiagnosisService,
)

BRANCH_PREFIX = "演示门店"
BRANCH_DECLINE = "演示门店·诊断"
BRANCH_THIN = "演示门店·基线不足"

# Customers that already exist in the dev seed; orders need a customer_id and
# creating throwaway accounts would leave rows behind after a rollback.
CUSTOMER_IDS = (1, 2)

BACKUP_PATH = os.path.join("scripts", "backups", "demo_diagnosis_seed.json")

# --------------------------------------------------------------------------- #
# The dataset, as data
# --------------------------------------------------------------------------- #

# (key, name, price) -- one line per order keeps the dish totals exact.
DISHES = [
    ("beef_noodle", "招牌牛肉面", 28.0),
    ("mushroom_noodle", "菌菇汤面", 26.0),
    ("dumpling", "手工饺子", 22.0),
    ("plum_drink", "冰镇酸梅汤", 8.0),
    ("cucumber", "凉拌黄瓜", 12.0),
]

# key -> price, for the mixes below.
PRICES = {key: price for key, _, price in DISHES}

# Offset from today -> (customer index, hour) for the baseline's cancelled order.
BASELINE_CANCEL_OFFSET = -10

# Baseline: 10 sold orders a day, every day. 7 active days, ¥208 a day.
BASELINE_MIX = [
    "beef_noodle", "beef_noodle", "beef_noodle",
    "mushroom_noodle", "mushroom_noodle",
    "dumpling", "dumpling",
    "plum_drink", "plum_drink",
    "cucumber",
]

# Current window: fewer orders, and today-4 is deliberately empty. The mix is
# what makes 客单价 fall only 22% while orders fall 71% -- the AOV drop is real
# but below the threshold, so it must NOT be reported.
CURRENT_MIX_BY_OFFSET = {
    -6: ["beef_noodle", "beef_noodle", "plum_drink", "plum_drink", "cucumber"],
    -5: ["beef_noodle", "plum_drink", "plum_drink", "mushroom_noodle"],
    -4: [],
    -3: ["beef_noodle", "beef_noodle", "plum_drink", "plum_drink"],
    -2: ["plum_drink", "plum_drink", "cucumber"],
    -1: ["mushroom_noodle", "plum_drink"],
    0: ["beef_noodle", "plum_drink"],
}

# Offset -> how the order was cancelled. The three shapes are the three the
# audit trail can hold; ``by`` and the actor come from different fields, which is
# exactly the distinction the attribution buckets on.
CURRENT_CANCELLATIONS = {
    -6: "customer",
    -3: "customer",
    -2: "staff",
    -1: "system",
}

# The dish that makes it into the baseline ranking but not the current one.
ABSENT_DISH = "手工饺子"
UNDER_THRESHOLD_DISH = "冰镇酸梅汤"


def _today() -> date:
    """Today in UTC -- mirrors ``service.diagnostics._today``."""
    return datetime.now(timezone.utc).date()


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=timezone.utc)


def _window(today: date):
    """The two windows the 经营诊断 tab opens with, as (start, end) dates."""
    current_end = today
    current_start = current_end - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
    baseline_end = current_start - timedelta(days=1)
    baseline_start = baseline_end - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
    return current_start, current_end, baseline_start, baseline_end


def _expected(today: date):
    """The numbers this dataset is built to produce, computed from the plan."""
    baseline_days = [today + timedelta(days=offset) for offset in range(-13, -6)]
    baseline_orders = len(baseline_days) * len(BASELINE_MIX)

    baseline_dish_qty = {}
    for key in BASELINE_MIX * len(baseline_days):
        baseline_dish_qty[key] = baseline_dish_qty.get(key, 0) + 1
    baseline_revenue = sum(
        PRICES[key] * count for key, count in baseline_dish_qty.items()
    )

    current_mix = [key for mix in CURRENT_MIX_BY_OFFSET.values() for key in mix]
    current_orders = len(current_mix)
    current_revenue = sum(PRICES[key] for key in current_mix)
    current_dish_qty = {}
    for key in current_mix:
        current_dish_qty[key] = current_dish_qty.get(key, 0) + 1

    return {
        "baseline_orders": baseline_orders,
        "baseline_revenue": baseline_revenue,
        "baseline_active_days": len(baseline_days),
        "baseline_cancelled": 1,
        "current_orders": current_orders,
        "current_revenue": current_revenue,
        "current_active_days": sum(
            1 for mix in CURRENT_MIX_BY_OFFSET.values() if mix
        ),
        "current_cancelled": len(CURRENT_CANCELLATIONS),
        "baseline_dish_qty": baseline_dish_qty,
        "current_dish_qty": current_dish_qty,
        "absent_dish": ABSENT_DISH,
        "under_threshold_dish": UNDER_THRESHOLD_DISH,
    }


# --------------------------------------------------------------------------- #
# Rollback
# --------------------------------------------------------------------------- #

async def rollback() -> None:
    async with AsyncSessionLocal() as s:
        ids = (
            await s.execute(
                select(Branch.id).where(Branch.name.like(f"{BRANCH_PREFIX}%"))
            )
        ).scalars().all()
        if not ids:
            print(f"没有找到名字以「{BRANCH_PREFIX}」开头的门店，无需回滚。")
            return

        # Audit rows must go first: their branch_id is ON DELETE SET NULL, so
        # deleting the branch would strand them as unattributed history rather
        # than remove them.
        audits = await s.execute(
            delete(AuditLog).where(AuditLog.branch_id.in_(ids))
        )
        orders = (
            await s.execute(
                select(Order.id).where(Order.branch_id.in_(ids))
            )
        ).scalars().all()
        branches = await s.execute(delete(Branch).where(Branch.id.in_(ids)))
        await s.commit()
        print(
            f"已回滚：门店 {list(ids)}、订单 {len(orders)} 张、审计 {audits.rowcount} 条"
            f"（订单行/菜单/桌台随门店级联删除）。"
        )


# --------------------------------------------------------------------------- #
# Seed
# --------------------------------------------------------------------------- #

async def _make_menu(s, branch):
    """One category per logical group, items and stock rows for a demo branch."""
    category = MenuCategory(branch_id=branch.id, name="演示菜品")
    s.add(category)
    await s.flush()

    items = {}
    for key, name, price in DISHES:
        item = MenuItem(
            category_id=category.id,
            name=name,
            description="演示数据，仅用于经营诊断功能验证。",
            base_price=price,
            is_available=True,
        )
        s.add(item)
        items[key] = item
    await s.flush()

    for item in items.values():
        s.add(
            MenuItemStock(
                menu_item_id=item.id, branch_id=branch.id, stock_quantity=999
            )
        )
    return items


async def _place_order(s, *, branch, customer_id, item_price, moment, dish_qty=None):
    """One order; ``dish_qty`` of None means a cancelled order with no lines."""
    order = Order(
        customer_id=customer_id,
        branch_id=branch.id,
        order_type="dine_in",
        status="placed" if dish_qty is not None else "cancelled",
        total_amount=item_price * dish_qty if dish_qty else 0.0,
        created_at=moment,
    )
    s.add(order)
    await s.flush()
    return order


async def _add_line(s, order, item, price):
    s.add(
        OrderItem(
            order_id=order.id,
            menu_item_id=item.id,
            quantity=1,
            unit_price=price,
            selected_variants=None,
        )
    )


async def _cancel_audit(s, *, order, branch, kind, moment, customer, staff):
    """Write the cancellation audit row in the shape the real path writes it.

    ``OrderService`` stamps ``by: customer`` only for a customer's own cancel;
    a staff cancel carries no ``by`` and a system one has no actor at all. All
    three must land in different attribution buckets.
    """
    if kind == "customer":
        actor, details = customer, {"from": "placed", "to": "cancelled", "by": "customer"}
    elif kind == "staff":
        actor, details = staff, {"from": "preparing", "to": "cancelled"}
    else:
        actor, details = None, {"from": "placed", "to": "cancelled"}

    s.add(
        AuditLog(
            created_at=moment,
            branch_id=branch.id,
            action="order.status_changed",
            entity_type="order",
            entity_id=order.id,
            details=details,
            **actor_fields(actor),
        )
    )


async def seed() -> None:
    today = _today()
    cur_start, cur_end, base_start, base_end = _window(today)
    expected = _expected(today)

    async with AsyncSessionLocal() as s:
        existing = (
            await s.execute(
                select(Branch.name).where(Branch.name.like(f"{BRANCH_PREFIX}%"))
            )
        ).scalars().all()
        if existing:
            raise SystemExit(
                f"演示门店已存在：{existing}。先跑 --rollback 再重新播种。"
            )

        # The staff member used as the actor on the staff-cancellation audit row.
        # Any existing staff row will do; the field only has to be denormalized
        # onto the log the way the API does it.
        staff = (
            await s.execute(select(StaffUser).order_by(StaffUser.id).limit(1))
        ).scalars().first()
        if staff is None:
            raise SystemExit("库里没有员工账号，无法写入员工取消的审计行。")
        customer = (
            await s.execute(
                select(Customer).where(Customer.id == CUSTOMER_IDS[0])
            )
        ).scalars().first()
        if customer is None:
            raise SystemExit(f"库里没有客户 #{CUSTOMER_IDS[0]}，无法写入顾客取消的审计行。")

        # ---------------------------------------------------------------- #
        # Branch A: a real decline, meant to be detected
        # ---------------------------------------------------------------- #
        decline = Branch(
            name=BRANCH_DECLINE,
            address="演示数据，非真实门店（可用 --rollback 删除）",
            phone="0000-0000000",
            opening_hours="10:00 - 22:00",
            is_active=True,
        )
        s.add(decline)
        await s.flush()
        items = await _make_menu(s, decline)

        seq = 0

        def next_customer():
            nonlocal seq
            seq += 1
            return CUSTOMER_IDS[(seq - 1) % len(CUSTOMER_IDS)]

        # Baseline: a full week of normal trade.
        for day_index in range(7):
            day = base_start + timedelta(days=day_index)
            for slot, key in enumerate(BASELINE_MIX):
                price = PRICES[key]
                order = await _place_order(
                    s,
                    branch=decline,
                    customer_id=next_customer(),
                    item_price=price,
                    moment=_at(day, 11 + (slot % 9), (slot * 7) % 60),
                    dish_qty=1,
                )
                await _add_line(s, order, items[key], price)

        # One cancelled order in the baseline: it sets a low rate for the
        # current window to rise from, and its audit row sits in the *baseline*
        # window so the attribution must not count it.
        cancel_day = today + timedelta(days=BASELINE_CANCEL_OFFSET)
        cancelled = await _place_order(
            s, branch=decline, customer_id=next_customer(), item_price=0.0,
            moment=_at(cancel_day, 13), dish_qty=None,
        )
        await _cancel_audit(
            s, order=cancelled, branch=decline, kind="staff",
            moment=_at(cancel_day, 13, 30), customer=customer, staff=staff,
        )

        # Current window: thinner, pricier, and with one day that sold nothing.
        for offset, mix in CURRENT_MIX_BY_OFFSET.items():
            day = today + timedelta(days=offset)
            for slot, key in enumerate(mix):
                price = PRICES[key]
                order = await _place_order(
                    s,
                    branch=decline,
                    customer_id=next_customer(),
                    item_price=price,
                    moment=_at(day, 12 + (slot % 8), (slot * 11) % 60),
                    dish_qty=1,
                )
                await _add_line(s, order, items[key], price)

        # Cancellations, each in a different audit shape.
        for offset, kind in CURRENT_CANCELLATIONS.items():
            day = today + timedelta(days=offset)
            gone = await _place_order(
                s, branch=decline, customer_id=next_customer(), item_price=0.0,
                moment=_at(day, 20), dish_qty=None,
            )
            await _cancel_audit(
                s, order=gone, branch=decline, kind=kind,
                moment=_at(day, 20, 15), customer=customer, staff=staff,
            )

        # ---------------------------------------------------------------- #
        # Branch B: a baseline too thin to judge
        # ---------------------------------------------------------------- #
        thin = Branch(
            name=BRANCH_THIN,
            address="演示数据，非真实门店（可用 --rollback 删除）",
            phone="0000-0000001",
            opening_hours="10:00 - 22:00",
            is_active=True,
        )
        s.add(thin)
        await s.flush()
        thin_items = await _make_menu(s, thin)
        _, _, thin_price = DISHES[0]

        # Two active days in the baseline, three in the current window: the
        # current window looks fine, which is the point -- the refusal is about
        # the "before" picture, not about this one.
        for offset in (-12, -9):
            for slot in range(3):
                order = await _place_order(
                    s, branch=thin, customer_id=next_customer(),
                    item_price=thin_price,
                    moment=_at(today + timedelta(days=offset), 12 + slot),
                    dish_qty=1,
                )
                await _add_line(s, order, thin_items["beef_noodle"], thin_price)
        for offset in (-5, -3, -1):
            for slot in range(3):
                order = await _place_order(
                    s, branch=thin, customer_id=next_customer(),
                    item_price=thin_price,
                    moment=_at(today + timedelta(days=offset), 12 + slot),
                    dish_qty=1,
                )
                await _add_line(s, order, thin_items["beef_noodle"], thin_price)

        decline_id, thin_id = decline.id, thin.id
        await s.commit()

    print(f"已写入演示数据（按 UTC 日期，今天 {today}）：")
    print(f"  上期窗口 {base_start} ~ {base_end}   本期窗口 {cur_start} ~ {cur_end}")
    print(
        f"  {BRANCH_DECLINE}: 上期 {expected['baseline_orders']} 单 /"
        f" ¥{expected['baseline_revenue']:.2f} / {expected['baseline_active_days']} 个有效日 /"
        f" {expected['baseline_cancelled']} 单取消"
    )
    print(
        f"  {BRANCH_DECLINE}: 本期 {expected['current_orders']} 单 /"
        f" ¥{expected['current_revenue']:.2f} / {expected['current_active_days']} 个有效日 /"
        f" {expected['current_cancelled']} 单取消"
    )
    print(f"  {BRANCH_THIN}: 上期只有 2 个有效经营日（低于门槛）")

    _write_backup(today, decline_id, thin_id, expected)
    await _verify(decline_id, thin_id, expected)


def _write_backup(today, decline_id, thin_id, expected) -> None:
    os.makedirs(os.path.dirname(BACKUP_PATH), exist_ok=True)
    with open(BACKUP_PATH, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "seeded_on": today.isoformat(),
                "branches": {BRANCH_DECLINE: decline_id, BRANCH_THIN: thin_id},
                "customers_reused": list(CUSTOMER_IDS),
                "expected": expected,
                "rollback": "python scripts/seed_demo_diagnosis.py --rollback",
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )
    print(f"  备份记录：{BACKUP_PATH}")


async def _verify(decline_id: int, thin_id: int, expected: dict) -> None:
    """Ask the real Service about the data just written, and grade the answer."""
    # An unscoped admin sees every branch; nothing is persisted from this object.
    viewer = StaffUser(username="__seed", role="admin", branch_id=None)
    async with AsyncSessionLocal() as s:
        decline = await DiagnosisService.diagnose(
            decline_id, None, None, staff=viewer, session=s
        )
        thin = await DiagnosisService.diagnose(
            thin_id, None, None, staff=viewer, session=s
        )

    print("\n" + "=" * 78)
    print("服务层真实结论（未调用 LLM）")
    print("=" * 78)
    for label, result in ((BRANCH_DECLINE, decline), (BRANCH_THIN, thin)):
        print(f"\n[{label}] status = {result['status']}")
        for note in result["notes"]:
            print(f"  · {note}")
        for finding in result["findings"]:
            print(
                f"  - {finding['label']:<8} {finding['severity']:<8}"
                f" {finding['direction']:<5} {finding['change_pct']:+.2f}%"
                f"  ({finding['previous']} -> {finding['current']})"
            )

    metrics = {f["metric"] for f in decline["findings"]}
    dish_cards = [f for f in decline["findings"] if f["metric"] == "dish_sales"]
    attribution = next(
        (
            e["value"]
            for f in decline["findings"]
            if f["metric"] == "cancelled_rate"
            for e in f["evidence"]
            if e["label"].startswith("本期取消归因")
        ),
        None,
    )
    skipped_note = next(
        (n for n in decline["notes"] if expected["absent_dish"] in n), None
    )

    checks = [
        # The design's whole point: the edges, not the totals.
        ("诊断得出结论（而非拒绝）", decline["status"] == "ok", decline["status"]),
        (
            "订单量按预期下降",
            next(
                (f for f in decline["findings"] if f["metric"] == "order_count"),
                {},
            ).get("current")
            == expected["current_orders"],
            f"本期 {expected['current_orders']} 单",
        ),
        (
            "销售额按预期下降",
            next(
                (f for f in decline["findings"] if f["metric"] == "revenue"), {}
            ).get("current")
            == expected["current_revenue"],
            f"本期 ¥{expected['current_revenue']:.2f}",
        ),
        (
            f"客单价（{-(1 - expected['current_revenue'] / expected['current_orders'] / (expected['baseline_revenue'] / expected['baseline_orders'])) * 100:.1f}%，在阈值内）不得被报为下降",
            "average_order_value" not in metrics,
            f"阈值 {CHANGE_THRESHOLD_PCT}%",
        ),
        (
            f"「{expected['under_threshold_dish']}」跌幅在阈值内，不得报为异常",
            all(
                expected["under_threshold_dish"] not in e["label"]
                for f in dish_cards
                for e in f["evidence"]
            ),
            f"阈值 {CHANGE_THRESHOLD_PCT}%",
        ),
        ("三条菜品异常被检出", len(dish_cards) == 3, f"实际 {len(dish_cards)} 条"),
        (
            f"「{expected['absent_dish']}」只能出「无法判定」说明，不能报成下降",
            skipped_note is not None,
            skipped_note or "（没有这条说明）",
        ),
        (
            "取消归因区分顾客 / 员工 / 未归属",
            attribution == {"customer": 2, "staff": 1, "unattributed": 1, "total": 4},
            str(attribution),
        ),
        (
            "薄基线门店被拒绝判定，不出任何结论",
            thin["status"] == "insufficient_data" and not thin["findings"],
            thin["status"],
        ),
    ]

    print("\n" + "=" * 78)
    print("自查")
    print("=" * 78)
    failed = 0
    for name, ok, detail in checks:
        failed += 0 if ok else 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  ({detail})")
    print()
    if failed:
        print(f"{failed} 项不符合预期 —— 数据或检测逻辑需要复查。")
        raise SystemExit(1)
    print(f"{len(checks)} 项全部符合预期。")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="演示门店的诊断测试数据：播种 / 回滚"
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="删除名字以「演示门店」开头的门店及其数据，然后退出",
    )
    args = parser.parse_args()
    if args.rollback:
        await rollback()
    else:
        await seed()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
