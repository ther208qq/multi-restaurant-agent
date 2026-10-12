"""One-off dev-DB cleanup: remove this round's (2026-10-11) test data.

Backs up every row it is about to delete to a JSON file first, then deletes in
foreign-key order inside a single transaction.

Scope (agreed with the user):
  * orders      created 2026-10-11          -> all 137 (incl. the 4 test-customer ones)
  * reservations  created 2026-10-11         -> #409 / #410 / #412
  * customers    test-email accounts         -> 255,256,257,258,259,308,309,334,335
Kept: 10-07/10-08 orders + reservations #1/#6/#9/#18/#19/#21, customer 1, seeds.
"""

import asyncio
import datetime as dt
import json
from pathlib import Path

import asyncpg

DSN = "postgresql://postgres:postgrespassword@localhost:5432/restaurant_ai"
BACKUP = Path(__file__).resolve().parent / "backups" / "dev_cleanup_2026-10-12.json"

ORDER_WINDOW = ("2026-10-11T00:00:00+00:00", "2026-10-12T00:00:00+00:00")
RESERVATION_IDS = [409, 410, 412]
CUSTOMER_IDS = [255, 256, 257, 258, 259, 308, 309, 334, 335]

_TABLES = (
    ("orders", "id = ANY($1::int[])"),
    ("order_items", "order_id = ANY($1::int[])"),
    ("reservations", "id = ANY($1::int[])"),
    ("customers", "id = ANY($1::int[])"),
)


def _default(obj):
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    return str(obj)


async def _dump(conn, table, where, ids):
    rows = await conn.fetch(
        f"SELECT row_to_json(t) FROM (SELECT * FROM {table} WHERE {where}) t", ids
    )
    return [json.loads(r["row_to_json"]) for r in rows]


async def main() -> None:
    conn = await asyncpg.connect(DSN)
    try:
        order_ids = [
            r["id"]
            for r in await conn.fetch(
                "SELECT id FROM orders WHERE created_at >= $1 AND created_at < $2 ORDER BY id",
                dt.datetime.fromisoformat(ORDER_WINDOW[0]),
                dt.datetime.fromisoformat(ORDER_WINDOW[1]),
            )
        ]
        print(f"orders to delete (created 2026-10-11): {len(order_ids)}")

        # Safety: the agreed reservation/customer sets must be exactly what we expect.
        found_res = [
            r["id"]
            for r in await conn.fetch(
                "SELECT id FROM reservations WHERE id = ANY($1::int[]) ORDER BY id",
                RESERVATION_IDS,
            )
        ]
        found_cust = [
            r["id"]
            for r in await conn.fetch(
                "SELECT id FROM customers WHERE id = ANY($1::int[]) ORDER BY id",
                CUSTOMER_IDS,
            )
        ]
        assert found_res == sorted(RESERVATION_IDS), f"reservations mismatch: {found_res}"
        assert found_cust == sorted(CUSTOMER_IDS), f"customers mismatch: {found_cust}"

        backup = {
            "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "database": "restaurant_ai",
            "orders": await _dump(conn, "orders", "id = ANY($1::int[])", order_ids),
            "order_items": await _dump(
                conn, "order_items", "order_id = ANY($1::int[])", order_ids
            ),
            "reservations": await _dump(
                conn, "reservations", "id = ANY($1::int[])", RESERVATION_IDS
            ),
            "customers": await _dump(
                conn, "customers", "id = ANY($1::int[])", CUSTOMER_IDS
            ),
        }
        BACKUP.parent.mkdir(parents=True, exist_ok=True)
        BACKUP.write_text(
            json.dumps(backup, indent=2, default=_default), encoding="utf-8"
        )
        print(f"backup written: {BACKUP}")

        async with conn.transaction():
            deleted_items = await conn.execute(
                "DELETE FROM order_items WHERE order_id = ANY($1::int[])", order_ids
            )
            deleted_orders = await conn.execute(
                "DELETE FROM orders WHERE id = ANY($1::int[])", order_ids
            )
            deleted_res = await conn.execute(
                "DELETE FROM reservations WHERE id = ANY($1::int[])", RESERVATION_IDS
            )
            deleted_cust = await conn.execute(
                "DELETE FROM customers WHERE id = ANY($1::int[])", CUSTOMER_IDS
            )
        for label, status in (
            ("order_items", deleted_items),
            ("orders", deleted_orders),
            ("reservations", deleted_res),
            ("customers", deleted_cust),
        ):
            print(f"delete {label}: {status}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
