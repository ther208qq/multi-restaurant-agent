"""Database Seeder Script."""

import asyncio
from sqlalchemy import select
from backend.app.auth.security import get_password_hash
from backend.app.database import AsyncSessionLocal, init_db
from backend.app.models.branch import Branch, Table
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock, MenuItemVariant
from backend.app.models.user import Customer, StaffUser


async def seed_data() -> None:
    """Populate database with sample branches, tables, menus, variants, inventory, and users."""
    print("正在创建数据表与扩展...")
    await init_db()

    async with AsyncSessionLocal() as session:
        # Check if already seeded
        result = await session.execute(select(Branch).limit(1))
        existing_branch = result.scalar_one_or_none()
        if existing_branch:
            print("数据库中已存在门店数据，跳过重复写入。")
            return

        print("正在写入门店...")
        b1 = Branch(
            name="市中心小馆",
            address="中央市主街 123 号",
            phone="+1-555-0100",
            opening_hours="10:00 - 22:00",
            is_active=True,
        )
        b2 = Branch(
            name="上城露台餐厅",
            address="北山高街 456 号",
            phone="+1-555-0200",
            opening_hours="11:00 - 23:00",
            is_active=True,
        )
        session.add_all([b1, b2])
        await session.flush()

        print("正在写入餐桌...")
        tables = [
            # 市中心小馆餐桌
            Table(branch_id=b1.id, label="T1", seating_area="indoor", capacity=2, is_available=True),
            Table(branch_id=b1.id, label="T2", seating_area="indoor", capacity=4, is_available=True),
            Table(branch_id=b1.id, label="T3", seating_area="indoor", capacity=6, is_available=True),
            Table(branch_id=b1.id, label="P1", seating_area="outdoor", capacity=2, is_available=True),
            Table(branch_id=b1.id, label="P2", seating_area="outdoor", capacity=4, is_available=True),
            Table(branch_id=b1.id, label="R1", seating_area="rooftop", capacity=4, is_available=True),
            # 上城露台餐厅餐桌
            Table(branch_id=b2.id, label="U1", seating_area="indoor", capacity=2, is_available=True),
            Table(branch_id=b2.id, label="U2", seating_area="indoor", capacity=4, is_available=True),
            Table(branch_id=b2.id, label="O1", seating_area="outdoor", capacity=4, is_available=True),
            Table(branch_id=b2.id, label="R1", seating_area="rooftop", capacity=6, is_available=True),
            Table(branch_id=b2.id, label="R2", seating_area="rooftop", capacity=8, is_available=True),
        ]
        session.add_all(tables)
        await session.flush()

        print("正在写入菜单分类...")
        cat_b1_pizza = MenuCategory(branch_id=b1.id, name="披萨")
        cat_b1_pasta = MenuCategory(branch_id=b1.id, name="意面")
        cat_b1_drinks = MenuCategory(branch_id=b1.id, name="饮品")
        cat_b1_desserts = MenuCategory(branch_id=b1.id, name="甜点")

        cat_b2_pizza = MenuCategory(branch_id=b2.id, name="手工披萨")
        cat_b2_drinks = MenuCategory(branch_id=b2.id, name="特色饮品")

        session.add_all([cat_b1_pizza, cat_b1_pasta, cat_b1_drinks, cat_b1_desserts, cat_b2_pizza, cat_b2_drinks])
        await session.flush()

        print("正在写入菜品、规格与库存...")
        # 市中心小馆菜品
        # 1. 玛格丽特披萨
        item_margherita = MenuItem(
            category_id=cat_b1_pizza.id,
            name="玛格丽特披萨",
            description="经典酸面团披萨，配圣马尔扎诺番茄、新鲜马苏里拉与芳香罗勒。",
            base_price=12.00,
            is_available=True,
        )
        session.add(item_margherita)
        await session.flush()

        variants_margherita = [
            MenuItemVariant(menu_item_id=item_margherita.id, variant_type="size", variant_name="小份（10 英寸）",
                            price_delta=-2.00),
            MenuItemVariant(menu_item_id=item_margherita.id, variant_type="size", variant_name="中份（12 英寸）",
                            price_delta=0.00),
            MenuItemVariant(menu_item_id=item_margherita.id, variant_type="size", variant_name="大份（14 英寸）",
                            price_delta=4.00),
            MenuItemVariant(menu_item_id=item_margherita.id, variant_type="topping", variant_name="加量马苏里拉",
                            price_delta=2.00),
            MenuItemVariant(menu_item_id=item_margherita.id, variant_type="topping", variant_name="卡拉马塔橄榄",
                            price_delta=1.50),
        ]
        stock_margherita = MenuItemStock(menu_item_id=item_margherita.id, branch_id=b1.id, stock_quantity=40)
        session.add_all(variants_margherita + [stock_margherita])

        # 2. 松露蘑菇披萨（低库存 = 2，用于触发低库存/售罄流程）
        item_truffle = MenuItem(
            category_id=cat_b1_pizza.id,
            name="松露蘑菇披萨",
            description="野生森林蘑菇、白松露油、刨片佩科里诺奶酪，佐以香浓蒜香底酱。",
            base_price=16.50,
            is_available=True,
        )
        session.add(item_truffle)
        await session.flush()
        variants_truffle = [
            MenuItemVariant(menu_item_id=item_truffle.id, variant_type="size", variant_name="中份", price_delta=0.00),
            MenuItemVariant(menu_item_id=item_truffle.id, variant_type="size", variant_name="大份", price_delta=4.50),
        ]
        stock_truffle = MenuItemStock(menu_item_id=item_truffle.id, branch_id=b1.id, stock_quantity=2)
        session.add_all(variants_truffle + [stock_truffle])

        # 3. 腊肠盛宴披萨
        item_pepperoni = MenuItem(
            category_id=cat_b1_pizza.id,
            name="腊肠盛宴披萨",
            description="铺满香脆手工意式辣香肠、碎辣椒片与双倍马苏里拉。",
            base_price=14.50,
            is_available=True,
        )
        session.add(item_pepperoni)
        await session.flush()
        stock_pepperoni = MenuItemStock(menu_item_id=item_pepperoni.id, branch_id=b1.id, stock_quantity=30)
        session.add(stock_pepperoni)

        # 4. 阿尔弗雷多奶油宽面
        item_alfredo = MenuItem(
            category_id=cat_b1_pasta.id,
            name="阿尔弗雷多奶油宽面",
            description="手工宽面条，裹上丝滑的帕玛森奶油酱。",
            base_price=13.50,
            is_available=True,
        )
        session.add(item_alfredo)
        await session.flush()
        variants_alfredo = [
            MenuItemVariant(menu_item_id=item_alfredo.id, variant_type="spice_level", variant_name="微辣",
                            price_delta=0.00),
            MenuItemVariant(menu_item_id=item_alfredo.id, variant_type="spice_level", variant_name="辣",
                            price_delta=0.50),
        ]
        stock_alfredo = MenuItemStock(menu_item_id=item_alfredo.id, branch_id=b1.id, stock_quantity=25)
        session.add_all(variants_alfredo + [stock_alfredo])

        # 5. 手工柠檬水
        item_lemonade = MenuItem(
            category_id=cat_b1_drinks.id,
            name="手工柠檬水",
            description="鲜榨柠檬汁，融入新鲜薄荷叶与野花蜂蜜。",
            base_price=4.50,
            is_available=True,
        )
        session.add(item_lemonade)
        await session.flush()
        stock_lemonade = MenuItemStock(menu_item_id=item_lemonade.id, branch_id=b1.id, stock_quantity=50)
        session.add(stock_lemonade)

        # 6. 熔岩巧克力蛋糕（低库存 = 1）
        item_lava_cake = MenuItem(
            category_id=cat_b1_desserts.id,
            name="熔岩巧克力蛋糕",
            description="温热黑巧克力蛋糕，内含流心松露夹心，配香草荚意式冰淇淋。",
            base_price=8.00,
            is_available=True,
        )
        session.add(item_lava_cake)
        await session.flush()
        stock_lava_cake = MenuItemStock(menu_item_id=item_lava_cake.id, branch_id=b1.id, stock_quantity=1)
        session.add(stock_lava_cake)

        # 上城露台餐厅菜品
        item_uptown_pizza = MenuItem(
            category_id=cat_b2_pizza.id,
            name="帕尔马火腿芝麻菜披萨",
            description="陈年帕尔马火腿、嫩芝麻菜、刨片帕玛森，淋上巴萨米克黑醋汁。",
            base_price=17.00,
            is_available=True,
        )
        session.add(item_uptown_pizza)
        await session.flush()
        stock_uptown_pizza = MenuItemStock(menu_item_id=item_uptown_pizza.id, branch_id=b2.id, stock_quantity=30)
        session.add(stock_uptown_pizza)

        item_uptown_drink = MenuItem(
            category_id=cat_b2_drinks.id,
            name="洛神花气泡饮",
            description="冷萃洛神花茶、气泡矿泉水、新鲜青柠与有机龙舌兰糖浆。",
            base_price=5.50,
            is_available=True,
        )
        session.add(item_uptown_drink)
        await session.flush()
        stock_uptown_drink = MenuItemStock(menu_item_id=item_uptown_drink.id, branch_id=b2.id, stock_quantity=45)
        session.add(stock_uptown_drink)

        print("正在写入员工与顾客账号...")
        admin_user = StaffUser(
            username="admin",
            password_hash=get_password_hash("admin123"),
            role="admin",
            branch_id=None,  # 全部门店
        )
        downtown_staff = StaffUser(
            username="downtown_staff",
            password_hash=get_password_hash("staff123"),
            role="staff",
            branch_id=b1.id,
        )
        uptown_staff = StaffUser(
            username="uptown_staff",
            password_hash=get_password_hash("staff123"),
            role="staff",
            branch_id=b2.id,
        )
        customer = Customer(
            name="Alice Johnson",
            email="customer@example.com",
            password_hash=get_password_hash("customer123"),
            phone="+1-555-1234",
        )
        session.add_all([admin_user, downtown_staff, uptown_staff, customer])

        await session.commit()
        print("数据库初始化完成：门店、餐桌、菜单、库存、员工与顾客账号均已写入！")


if __name__ == "__main__":
    asyncio.run(seed_data())
