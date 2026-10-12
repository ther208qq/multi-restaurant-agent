"""One-off dev-DB localization: rename the English seed rows to Chinese in place.

Backs up every row it touches to a JSON file first, then renames in a single
transaction. Existing orders / reservations / customers are left untouched, so
their history survives.

Also deletes the two leftover ``__route_test_branch`` rows (ids 7 and 8) that an
earlier routing-test round leaked into the dev DB; they own no tables, menu or
orders (verified before writing this script).

Run from the repository root:

    ./.venv/Scripts/python.exe -m scripts.localize_dev_seed
"""

import asyncio
import datetime as dt
import json
from pathlib import Path

from sqlalchemy import delete, select

from backend.app.database import AsyncSessionLocal
from backend.app.models.branch import Branch, Table
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemVariant

BACKUP = (
    Path(__file__).resolve().parent
    / "backups"
    / "dev_seed_localize_2026-10-12.json"
)

BRANCH_UPDATES = {
    1: {
        "name": "市中心小馆",
        "address": "中央市主街 123 号",
        "phone": "+1-555-0100",
        "opening_hours": "10:00 - 22:00",
    },
    2: {
        "name": "上城露台餐厅",
        "address": "北山高街 456 号",
        "phone": "+1-555-0200",
        "opening_hours": "11:00 - 23:00",
    },
}

CATEGORY_RENAMES = {
    (1, "Pizzas"): "披萨",
    (1, "Pastas"): "意面",
    (1, "Beverages"): "饮品",
    (1, "Desserts"): "甜点",
    (2, "Artisan Pizzas"): "手工披萨",
    (2, "Specialty Drinks"): "特色饮品",
}

ITEM_UPDATES = {
    "Margherita Pizza": (
        "玛格丽特披萨",
        "经典酸面团披萨，配圣马尔扎诺番茄、新鲜马苏里拉与芳香罗勒。",
    ),
    "Truffle Mushroom Pizza": (
        "松露蘑菇披萨",
        "野生森林蘑菇、白松露油、刨片佩科里诺奶酪，佐以香浓蒜香底酱。",
    ),
    "Pepperoni Feast Pizza": (
        "腊肠盛宴披萨",
        "铺满香脆手工意式辣香肠、碎辣椒片与双倍马苏里拉。",
    ),
    "Fettuccine Alfredo": (
        "阿尔弗雷多奶油宽面",
        "手工宽面条，裹上丝滑的帕玛森奶油酱。",
    ),
    "Artisan Lemonade": (
        "手工柠檬水",
        "鲜榨柠檬汁，融入新鲜薄荷叶与野花蜂蜜。",
    ),
    "Molten Chocolate Lava Cake": (
        "熔岩巧克力蛋糕",
        "温热黑巧克力蛋糕，内含流心松露夹心，配香草荚意式冰淇淋。",
    ),
    "Prosciutto & Arugula Pizza": (
        "帕尔马火腿芝麻菜披萨",
        "陈年帕尔马火腿、嫩芝麻菜、刨片帕玛森，淋上巴萨米克黑醋汁。",
    ),
    "Sparkling Hibiscus Cooler": (
        "洛神花气泡饮",
        "冷萃洛神花茶、气泡矿泉水、新鲜青柠与有机龙舌兰糖浆。",
    ),
}

VARIANT_RENAMES = {
    'Small (10")': "小份（10 英寸）",
    'Medium (12")': "中份（12 英寸）",
    'Large (14")': "大份（14 英寸）",
    "Extra Mozzarella": "加量马苏里拉",
    "Kalamata Olives": "卡拉马塔橄榄",
    "Medium": "中份",
    "Large": "大份",
    "Mild": "微辣",
    "Spicy": "辣",
}

# Table labels are plain identifiers: the seating area is its own column in the
# admin UI and is rendered separately in the agent's booking reply, so embedding
# it in the label duplicated it (``T2（室内）（室内）``). Keyed by the label as it
# stood in the DB, so a re-run also collapses the intermediate Chinese form.
TABLE_LABELS = {
    (1, "T1 (Indoor)"): "T1",
    (1, "T2 (Indoor)"): "T2",
    (1, "T3 (Indoor)"): "T3",
    (1, "P1 (Patio)"): "P1",
    (1, "P2 (Patio)"): "P2",
    (1, "R1 (Roof)"): "R1",
    (2, "U1 (Indoor)"): "U1",
    (2, "U2 (Indoor)"): "U2",
    (2, "O1 (Garden)"): "O1",
    (2, "R1 (Terrace)"): "R1",
    (2, "R2 (Terrace)"): "R2",
    (1, "T1（室内）"): "T1",
    (1, "T2（室内）"): "T2",
    (1, "T3（室内）"): "T3",
    (1, "P1（露台）"): "P1",
    (1, "P2（露台）"): "P2",
    (1, "R1（天台）"): "R1",
    (2, "U1（室内）"): "U1",
    (2, "U2（室内）"): "U2",
    (2, "O1（花园）"): "O1",
    (2, "R1（天台）"): "R1",
    (2, "R2（天台）"): "R2",
}

DELETE_BRANCH_IDS = [7, 8]


def _row(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


async def main() -> None:
    async with AsyncSessionLocal() as s:
        branches = (await s.execute(select(Branch))).scalars().all()
        categories = (await s.execute(select(MenuCategory))).scalars().all()
        items = (await s.execute(select(MenuItem))).scalars().all()
        variants = (await s.execute(select(MenuItemVariant))).scalars().all()
        tables = (await s.execute(select(Table))).scalars().all()

        backup = {
            "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "database": "restaurant_ai",
            "branches": [_row(o) for o in branches],
            "menu_categories": [_row(o) for o in categories],
            "menu_items": [_row(o) for o in items],
            "menu_item_variants": [_row(o) for o in variants],
            "tables": [_row(o) for o in tables],
        }
        BACKUP.parent.mkdir(parents=True, exist_ok=True)
        BACKUP.write_text(
            json.dumps(backup, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"backup written: {BACKUP}")

        renamed = 0
        for b in branches:
            if b.id in BRANCH_UPDATES:
                for key, value in BRANCH_UPDATES[b.id].items():
                    setattr(b, key, value)
                renamed += 1
        for c in categories:
            new_name = CATEGORY_RENAMES.get((c.branch_id, c.name))
            if new_name:
                c.name = new_name
                renamed += 1
        for item in items:
            pair = ITEM_UPDATES.get(item.name)
            if pair:
                item.name, item.description = pair
                renamed += 1
        for v in variants:
            new_name = VARIANT_RENAMES.get(v.variant_name)
            if new_name:
                v.variant_name = new_name
                renamed += 1
        for t in tables:
            new_label = TABLE_LABELS.get((t.branch_id, t.label))
            if new_label:
                t.label = new_label
                renamed += 1

        removed = (
            await s.execute(delete(Branch).where(Branch.id.in_(DELETE_BRANCH_IDS)))
        ).rowcount

        await s.commit()
        print(f"renamed rows: {renamed}; deleted test branches: {removed}")


if __name__ == "__main__":
    asyncio.run(main())



