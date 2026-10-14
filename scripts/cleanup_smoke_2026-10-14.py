"""Roll back data the 2026-10-14 acceptance smoke run left in the dev DB.

Placed one order (stock -1 on two items) and booked one reservation
(occupying T1). Restores the stock, then deletes the order, its lines,
its audit rows, the reservation and its audit rows in one transaction.
A JSON backup is written first.
"""

import asyncio
import json
import os

import asyncpg

DSN = dict(
    user="postgres",
    password="postgrespassword",
    database="restaurant_ai",
    host="localhost",
    port=5432,
)

BACKUP = os.path.join("scripts", "backups", "smoke_accept_2026-10-14.json")
ORDER_NOTE = "验收冒烟：少辣"
RESV_NOTE = "验收冒烟：靠窗"


async def main():
    c = await asyncpg.connect(**DSN)
    try:
        order = await c.fetchrow(
            "SELECT * FROM orders WHERE special_notes = $1 ORDER BY id DESC LIMIT 1",
            ORDER_NOTE,
        )
        resv = await c.fetchrow(
            "SELECT * FROM reservations WHERE notes = $1 ORDER BY id DESC LIMIT 1",
            RESV_NOTE,
        )
        oid = order["id"] if order else None
        rid = resv["id"] if resv else None
        print("targets order:", oid, "reservation:", rid)

        items = (
            await c.fetch("SELECT * FROM order_items WHERE order_id = $1", oid)
            if oid else []
        )
        oaudit = (
            await c.fetch(
                "SELECT * FROM audit_logs WHERE entity_type='order' AND entity_id=$1", oid
            )
            if oid else []
        )
        raudit = (
            await c.fetch(
                "SELECT * FROM audit_logs WHERE entity_type='reservation' AND entity_id=$1", rid
            )
            if rid else []
        )

        backup = {
            "order": dict(order) if order else None,
            "order_items": [dict(r) for r in items],
            "reservation": dict(resv) if resv else None,
            "audit_logs": [dict(r) for r in list(oaudit) + list(raudit)],
        }
        os.makedirs(os.path.dirname(BACKUP), exist_ok=True)
        with open(BACKUP, "w", encoding="utf-8") as fh:
            json.dump(backup, fh, ensure_ascii=False, indent=2, default=str)
        print("backup ->", BACKUP)

        async with c.transaction():
            # Restore stock the order took (one unit per line).
            for it in items:
                await c.execute(
                    "UPDATE menu_item_stocks SET stock_quantity = stock_quantity + $1 "
                    "WHERE branch_id = $2 AND menu_item_id = $3",
                    it["quantity"], order["branch_id"], it["menu_item_id"],
                )
            if oid:
                await c.execute("DELETE FROM order_items WHERE order_id=$1", oid)
                await c.execute(
                    "DELETE FROM audit_logs WHERE entity_type='order' AND entity_id=$1", oid
                )
                await c.execute("DELETE FROM orders WHERE id=$1", oid)
            if rid:
                await c.execute(
                    "DELETE FROM audit_logs WHERE entity_type='reservation' AND entity_id=$1", rid
                )
                await c.execute("DELETE FROM reservations WHERE id=$1", rid)
        print("cleaned; stock restored for", len(items), "line(s)")
    finally:
        await c.close()


asyncio.run(main())
