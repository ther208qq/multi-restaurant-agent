"""Import a public multi-store sales dataset into the project for diagnosis trials.

Source: Maven Analytics "Coffee Shop Sales" sample dataset (Maven Roasters), a
fictitious coffee-shop chain with three NYC locations. Distributed by Maven
Analytics as a free public sample dataset; this project uses it only for local
evaluation. The workbook itself is not committed (``scripts/backups/`` is
git-ignored).

  https://mavenanalytics.io/data-playground/coffee-shop-sales
  mirror used for this run:
  https://github.com/datalopes1/maven_coffee/blob/main/data/Coffee%20Shop%20Sales.xlsx

Shape: 149,116 line items, 2023-01-01 .. 2023-06-30, stores Astoria /
Hell's Kitchen / Lower Manhattan, 82-95 products per store. Every row is one
transaction with one product line, which maps naturally onto one Order with one
OrderItem.

What this script deliberately does **not** invent
--------------------------------------------------
* No cancellations: the source has none, so every imported order is "delivered".
  Cancellation findings cannot be exercised with this dataset.
* No dish availability history: availability stays unknown, exactly as the
  existing contract says.
* Times are imported as-is and treated as UTC (the diagnosis buckets by UTC
  date). This is a labeling assumption, not a claim about NYC local time; it is
  consistent across both compared windows, so week-over-week comparisons hold.

Isolation
---------
The default target is a separate evaluation database, ``restaurant_ai_eval``,
derived from ``DATABASE_URL`` by replacing the database name. The development
database is untouched. Imported branches are named ``公开数据·<store>``; both
``--replace`` and ``--rollback`` remove exactly those rows.

Usage
-----
    .venv/Scripts/python.exe scripts/import_public_sales_dataset.py \
        --source scripts/backups/coffee_shop_sales.xlsx \
        --create-database

    .venv/Scripts/python.exe scripts/import_public_sales_dataset.py --rollback

Evaluation-only login created in the target database:
    eval_admin / admin123        (role=admin)
    eval_staff / staff123        (branch-bound to the first store in the source;
                                  Lower Manhattan for the shipped workbook)
"""

import argparse
import asyncio
import os
import re
import sys
import zipfile
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Tuple
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.engine import make_url  # noqa: E402

from backend.app.config import settings  # noqa: E402

BRANCH_PREFIX = "公开数据·"
CUSTOMER_EMAIL_PREFIX = "public-dataset-"
EVAL_USERNAMES = ("eval_admin", "eval_staff")
ORDER_ID_OFFSET = 10_000_000
DEFAULT_DATABASE_NAME = "restaurant_ai_eval"
DEFAULT_SOURCE = os.path.join("scripts", "backups", "coffee_shop_sales.xlsx")

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
HEADER = [
    "transaction_id",
    "transaction_date",
    "transaction_time",
    "transaction_qty",
    "store_id",
    "store_location",
    "product_id",
    "unit_price",
    "product_category",
    "product_type",
    "product_detail",
]


# --------------------------------------------------------------------------- #
# XLSX reading (stdlib only -- the project does not depend on openpyxl/pandas)
# --------------------------------------------------------------------------- #

def _column_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - 64)
    return index - 1


def read_xlsx(path: str) -> List[Dict[str, Any]]:
    """Read the first worksheet into dicts keyed by the documented header."""
    with zipfile.ZipFile(path) as workbook:
        shared = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
            for item in root.findall(f"{NS}si"):
                shared.append("".join(t.text or "" for t in item.iter(f"{NS}t")))

        rows: List[Dict[str, Any]] = []
        header_seen = False
        with workbook.open("xl/worksheets/sheet1.xml") as sheet:
            for _, element in ET.iterparse(sheet, events=("end",)):
                if element.tag != f"{NS}row":
                    continue
                cells: Dict[int, Any] = {}
                for cell in element.findall(f"{NS}c"):
                    position = _column_index(cell.get("r"))
                    value_node = cell.find(f"{NS}v")
                    cell_type = cell.get("t")
                    if cell_type == "inlineStr":
                        value = "".join(
                            t.text or "" for t in cell.iter(f"{NS}t")
                        )
                    elif value_node is None:
                        value = None
                    elif cell_type == "s":
                        value = shared[int(value_node.text)]
                    else:
                        value = value_node.text
                    cells[position] = value

                if not header_seen:
                    header = [cells.get(i) for i in range(len(HEADER))]
                    if header != HEADER:
                        raise SystemExit(
                            f"unexpected header: {header!r}; expected {HEADER!r}"
                        )
                    header_seen = True
                else:
                    rows.append(
                        {name: cells.get(i) for i, name in enumerate(HEADER)}
                    )
                element.clear()
        return rows


def _excel_date(serial: Any) -> date:
    return date(1899, 12, 30) + timedelta(days=int(float(serial)))


def _excel_time(fraction: Any) -> time:
    seconds = round(float(fraction) * 86400)
    return time(seconds // 3600, seconds % 3600 // 60, seconds % 60)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# --------------------------------------------------------------------------- #
# Database target
# --------------------------------------------------------------------------- #

def _target_database(name: str) -> str:
    return make_url(settings.DATABASE_URL).set(database=name)


async def _create_database(name: str) -> None:
    import asyncpg

    url = make_url(settings.DATABASE_URL)
    conn = await asyncpg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        database="postgres",
    )
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", name
        )
        if exists:
            print(f"database {name}: already exists")
        else:
            await conn.execute(f'CREATE DATABASE "{name}"')
            print(f"database {name}: created")
    finally:
        await conn.close()


def _activate_database(name: str) -> None:
    """Point every backend module imported after this call at the eval DB."""
    # ``str(URL)`` masks the password with ``***``; the engine needs the real one.
    url = _target_database(name).render_as_string(hide_password=False)
    os.environ["DATABASE_URL"] = url
    settings.DATABASE_URL = url


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #

def _dataset_stores(rows: Iterable[Dict[str, Any]]) -> List[str]:
    seen = []
    for row in rows:
        store = row["store_location"]
        if store not in seen:
            seen.append(store)
    return seen


def _product_key(row: Dict[str, Any]) -> Tuple[str, str]:
    return str(row["store_location"]), str(row["product_id"])


async def rollback(database_name: str) -> None:
    _activate_database(database_name)
    from sqlalchemy import delete, select

    from backend.app.database import AsyncSessionLocal
    from backend.app.models.audit import AuditLog
    from backend.app.models.branch import Branch
    from backend.app.models.order import Order
    from backend.app.models.user import Customer, StaffUser

    async with AsyncSessionLocal() as session:
        branch_ids = (
            await session.execute(
                select(Branch.id).where(Branch.name.like(f"{BRANCH_PREFIX}%"))
            )
        ).scalars().all()
        if not branch_ids:
            print(f"没有找到名字以「{BRANCH_PREFIX}」开头的门店，无需回滚。")
            return

        await session.execute(
            delete(AuditLog).where(AuditLog.branch_id.in_(branch_ids))
        )
        order_count = len(
            (
                await session.execute(
                    select(Order.id).where(Order.branch_id.in_(branch_ids))
                )
            ).scalars().all()
        )
        # Orders first: order_items cascade, and menu items cannot be deleted
        # while their order_items still reference them.
        await session.execute(delete(Order).where(Order.branch_id.in_(branch_ids)))
        await session.execute(delete(Branch).where(Branch.id.in_(branch_ids)))
        await session.execute(
            delete(StaffUser).where(StaffUser.username.in_(EVAL_USERNAMES))
        )
        await session.execute(
            delete(Customer).where(
                Customer.email.like(f"{CUSTOMER_EMAIL_PREFIX}%")
            )
        )
        await session.commit()
        print(
            f"已回滚评测数据：门店 {list(branch_ids)}、订单 {order_count} 张、"
            f"评测账号 {list(EVAL_USERNAMES)}（订单行/菜单/库存随门店级联删除）。"
        )


async def import_dataset(source: str, database_name: str) -> None:
    rows = read_xlsx(source)
    if not rows:
        raise SystemExit("数据文件没有任何数据行。")

    _activate_database(database_name)

    from sqlalchemy import func, insert, select

    from backend.app import models  # noqa: F401  register all tables
    from backend.app.auth.security import get_password_hash
    from backend.app.database import AsyncSessionLocal, init_db
    from backend.app.models.branch import Branch
    from backend.app.models.menu import MenuCategory, MenuItem
    from backend.app.models.order import Order, OrderItem
    from backend.app.models.user import Customer, StaffUser

    await init_db()
    print(f"schema ready: {database_name}")

    stores = _dataset_stores(rows)
    print(f"rows={len(rows)} stores={stores}")

    # Validate the two assumptions the import depends on before writing anything.
    transaction_ids = Counter(str(row["transaction_id"]) for row in rows)
    duplicated = [tx for tx, count in transaction_ids.items() if count > 1]
    if duplicated:
        raise SystemExit(
            f"transaction_id 不是全局唯一（示例：{duplicated[:3]}），无法直接映射为订单。"
        )

    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(func.count()).select_from(Branch).where(
                    Branch.name.like(f"{BRANCH_PREFIX}%")
                )
            )
        ).scalar_one()
        if existing:
            raise SystemExit(
                f"评测门店已存在（{existing} 家）。请先加 --replace 或 --rollback。"
            )

        # ---- branches, menus, products -------------------------------------
        branches: Dict[str, Branch] = {}
        for store in stores:
            branch = Branch(
                name=f"{BRANCH_PREFIX}{store}",
                address=f"{store}, New York, NY (public sample dataset)",
                phone="+1-000-000-0000",
                opening_hours="06:00 - 20:00",
                is_active=True,
            )
            session.add(branch)
            branches[store] = branch
        await session.flush()

        categories: Dict[Tuple[str, str], MenuCategory] = {}
        for row in rows:
            key = (row["store_location"], row["product_category"])
            if key not in categories:
                category = MenuCategory(
                    branch_id=branches[row["store_location"]].id,
                    name=row["product_category"],
                )
                session.add(category)
                categories[key] = category
        await session.flush()

        items: Dict[Tuple[str, str], int] = {}
        menu_rows = []
        for row in rows:
            key = _product_key(row)
            if key in items:
                continue
            store, product_id = key
            item_id = 2_000_000 + stores.index(store) * 100_000 + int(product_id)
            items[key] = item_id
            menu_rows.append(
                {
                    "id": item_id,
                    "category_id": categories[
                        (store, row["product_category"])
                    ].id,
                    "name": row["product_detail"],
                    "description": f"{row['product_category']} / {row['product_type']}",
                    "base_price": float(row["unit_price"]),
                    "is_available": True,
                }
            )
        await session.execute(insert(MenuItem.__table__), menu_rows)
        print(f"menu items: {len(menu_rows)} in {len(categories)} categories")

        # ---- one customer per store ----------------------------------------
        customers: Dict[str, int] = {}
        for store in stores:
            customer = Customer(
                name=f"公开数据评测顾客·{store}",
                email=f"{CUSTOMER_EMAIL_PREFIX}{_slug(store)}@example.invalid",
                password_hash=get_password_hash("dataset-only"),
                phone=None,
            )
            session.add(customer)
            await session.flush()
            customers[store] = customer.id

        # ---- evaluation-only staff -----------------------------------------
        session.add(
            StaffUser(
                username="eval_admin",
                password_hash=get_password_hash("admin123"),
                role="admin",
                branch_id=None,
            )
        )
        session.add(
            StaffUser(
                username="eval_staff",
                password_hash=get_password_hash("staff123"),
                role="staff",
                branch_id=branches[stores[0]].id,
            )
        )

        # ---- orders and order items ----------------------------------------
        order_rows = []
        item_rows = []
        for row in rows:
            transaction_id = int(row["transaction_id"])
            order_id = ORDER_ID_OFFSET + transaction_id
            quantity = int(float(row["transaction_qty"]))
            unit_price = float(row["unit_price"])
            created_at = datetime.combine(
                _excel_date(row["transaction_date"]),
                _excel_time(row["transaction_time"]),
                tzinfo=timezone.utc,
            )
            order_rows.append(
                {
                    "id": order_id,
                    "customer_id": customers[row["store_location"]],
                    "branch_id": branches[row["store_location"]].id,
                    "order_type": "takeaway",
                    "status": "delivered",
                    "delivery_address": None,
                    "special_notes": "imported public sample dataset",
                    "total_amount": round(quantity * unit_price, 2),
                    "kds_status": "done",
                    "kds_held_reason": None,
                    "created_at": created_at,
                    "updated_at": created_at,
                }
            )
            item_rows.append(
                {
                    "order_id": order_id,
                    "menu_item_id": items[_product_key(row)],
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "selected_variants": None,
                    "item_notes": None,
                }
            )

        batch = 5000
        for start in range(0, len(order_rows), batch):
            await session.execute(
                insert(Order.__table__), order_rows[start:start + batch]
            )
        for start in range(0, len(item_rows), batch):
            await session.execute(
                insert(OrderItem.__table__), item_rows[start:start + batch]
            )
        await session.commit()

        print(
            f"imported orders={len(order_rows)} order_items={len(item_rows)}"
        )

        # A short, checkable profile of what landed in the target database.
        counts = (
            await session.execute(
                select(Order.branch_id, func.count(), func.min(Order.created_at),
                       func.max(Order.created_at))
                .where(Order.branch_id.in_([b.id for b in branches.values()]))
                .group_by(Order.branch_id)
            )
        ).all()
        by_id = {branch.id: store for store, branch in branches.items()}
        for branch_id, count, first, last in counts:
            print(
                f"  {by_id[branch_id]:16} orders={count:6d} "
                f"{first:%Y-%m-%d} .. {last:%Y-%m-%d}"
            )
        print("evaluation logins: eval_admin/admin123, eval_staff/staff123")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument(
        "--database-name", default=DEFAULT_DATABASE_NAME,
        help=f"target database derived from DATABASE_URL (default: {DEFAULT_DATABASE_NAME})",
    )
    parser.add_argument(
        "--create-database", action="store_true",
        help="create the target database if it does not exist",
    )
    parser.add_argument(
        "--replace", action="store_true",
        help="rollback previously imported evaluation rows, then import again",
    )
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    async def run():
        if args.create_database:
            await _create_database(args.database_name)
        if args.rollback:
            await rollback(args.database_name)
            return
        if args.replace:
            await rollback(args.database_name)
        await import_dataset(args.source, args.database_name)

    asyncio.run(run())


if __name__ == "__main__":
    main()
