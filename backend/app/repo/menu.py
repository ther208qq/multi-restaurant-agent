"""Menu repository: semantic data access only.

Receives a session from the Service layer, encapsulates queries and updates,
never commits, and never imports ``service`` / ``routes`` / ``agent``.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.menu import (
    MenuCategory,
    MenuItem,
    MenuItemStock,
    MenuItemVariant,
)


class MenuRepo:
    @staticmethod
    async def list_categories(
        session: AsyncSession, branch_id: int
    ) -> List[MenuCategory]:
        stmt = (
            select(MenuCategory)
            .where(MenuCategory.branch_id == branch_id)
            .options(selectinload(MenuCategory.items).selectinload(MenuItem.variants))
            .order_by(MenuCategory.id.asc())
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_category(
        session: AsyncSession, category_id: int
    ) -> Optional[MenuCategory]:
        res = await session.execute(
            select(MenuCategory).where(MenuCategory.id == category_id)
        )
        return res.scalar_one_or_none()

    @staticmethod
    async def create_category(
        session: AsyncSession, branch_id: int, name: str
    ) -> MenuCategory:
        category = MenuCategory(branch_id=branch_id, name=name)
        session.add(category)
        await session.flush()
        return category

    @staticmethod
    async def get_item(
        session: AsyncSession,
        item_id: int,
        *,
        with_category: bool = False,
        with_variants: bool = False,
        with_stocks: bool = False,
    ) -> Optional[MenuItem]:
        stmt = select(MenuItem).where(MenuItem.id == item_id)
        options = []
        if with_category:
            options.append(selectinload(MenuItem.category))
        if with_variants:
            options.append(selectinload(MenuItem.variants))
        if with_stocks:
            options.append(selectinload(MenuItem.stocks))
        if options:
            stmt = stmt.options(*options)
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def create_item(
        session: AsyncSession,
        *,
        category_id: int,
        name: str,
        description: Optional[str],
        base_price: float,
        is_available: bool,
        variants: List[Dict[str, Any]],
    ) -> MenuItem:
        item = MenuItem(
            category_id=category_id,
            name=name,
            description=description,
            base_price=base_price,
            is_available=is_available,
        )
        session.add(item)
        await session.flush()

        for variant in variants:
            session.add(
                MenuItemVariant(
                    menu_item_id=item.id,
                    variant_type=variant["variant_type"],
                    variant_name=variant["variant_name"],
                    price_delta=variant.get("price_delta", 0.0),
                )
            )
        await session.flush()
        return item

    @staticmethod
    async def update_item(
        session: AsyncSession, item: MenuItem, values: Dict[str, Any]
    ) -> MenuItem:
        for key, value in values.items():
            setattr(item, key, value)
        await session.flush()
        return item

    @staticmethod
    async def get_stock(
        session: AsyncSession, menu_item_id: int, branch_id: int
    ) -> Optional[MenuItemStock]:
        stmt = select(MenuItemStock).where(
            MenuItemStock.menu_item_id == menu_item_id,
            MenuItemStock.branch_id == branch_id,
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def set_stock(
        session: AsyncSession,
        *,
        menu_item_id: int,
        branch_id: int,
        stock_quantity: int,
    ) -> MenuItemStock:
        stock = await MenuRepo.get_stock(session, menu_item_id, branch_id)
        if stock is None:
            stock = MenuItemStock(
                menu_item_id=menu_item_id,
                branch_id=branch_id,
                stock_quantity=stock_quantity,
            )
            session.add(stock)
        else:
            stock.stock_quantity = stock_quantity
        await session.flush()
        return stock

    @staticmethod
    async def stock_map(session: AsyncSession, branch_id: int) -> Dict[int, int]:
        res = await session.execute(
            select(MenuItemStock).where(MenuItemStock.branch_id == branch_id)
        )
        return {s.menu_item_id: s.stock_quantity for s in res.scalars().all()}

    @staticmethod
    async def list_surplus_candidates(
        session: AsyncSession,
        branch_id: int,
        *,
        category_keywords: List[str],
        min_stock: int,
    ) -> List[Any]:
        """Available items in the given category *keywords* holding at least
        ``min_stock`` units at the branch, most abundant first.

        Returns ``(MenuItem, category_name, stock_quantity)`` triples. Both the
        keyword list and the threshold are parameters, not constants: the "what
        counts as surplus" rule belongs to the Service layer, this only fetches
        the rows it asks for. Category matching is a substring test so a branch
        that names its category "特色饮品" still counts as a drinks category.
        """
        keyword_clauses = [
            MenuCategory.name.like(f"%{keyword}%") for keyword in category_keywords
        ]
        stmt = (
            select(MenuItem, MenuCategory.name, MenuItemStock.stock_quantity)
            .join(MenuItemStock, MenuItemStock.menu_item_id == MenuItem.id)
            .join(MenuCategory, MenuCategory.id == MenuItem.category_id)
            .where(
                MenuItemStock.branch_id == branch_id,
                MenuItemStock.stock_quantity >= min_stock,
                MenuItem.is_available == True,  # noqa: E712
                MenuCategory.branch_id == branch_id,
            )
            .order_by(MenuItemStock.stock_quantity.desc(), MenuItem.id.asc())
        )
        if keyword_clauses:
            stmt = stmt.where(or_(*keyword_clauses))
        res = await session.execute(stmt)
        return [tuple(row) for row in res.all()]

    # ------------------------------------------------------------------ #
    # Atomic stock operations (semantic data access for the Service layer)
    # ------------------------------------------------------------------ #

    @staticmethod
    async def get_stock_for_update(
        session: AsyncSession, menu_item_id: int, branch_id: int
    ) -> Optional[MenuItemStock]:
        """Row-locked stock read (``SELECT ... FOR UPDATE``) for pre-validation."""
        stmt = (
            select(MenuItemStock)
            .where(
                MenuItemStock.menu_item_id == menu_item_id,
                MenuItemStock.branch_id == branch_id,
            )
            .with_for_update()
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def try_decrement_stock(
        session: AsyncSession, menu_item_id: int, branch_id: int, qty: int
    ) -> bool:
        """Atomically decrement stock only if enough is available.

        Encodes ``UPDATE ... SET stock_quantity = stock_quantity - :qty
        WHERE ... AND stock_quantity >= :qty``. Returns ``True`` when exactly one
        row was updated (condition satisfied), ``False`` otherwise. "Not enough
        stock" is expressed as a return value, never an exception.
        """
        stmt = (
            update(MenuItemStock)
            .where(
                MenuItemStock.menu_item_id == menu_item_id,
                MenuItemStock.branch_id == branch_id,
                MenuItemStock.stock_quantity >= qty,
            )
            .values(stock_quantity=MenuItemStock.stock_quantity - qty)
        )
        res = await session.execute(stmt)
        return res.rowcount == 1

    @staticmethod
    async def restore_stock(
        session: AsyncSession, menu_item_id: int, branch_id: int, qty: int
    ) -> int:
        """Atomically add ``qty`` back to a stock row; returns affected row count."""
        stmt = (
            update(MenuItemStock)
            .where(
                MenuItemStock.menu_item_id == menu_item_id,
                MenuItemStock.branch_id == branch_id,
            )
            .values(stock_quantity=MenuItemStock.stock_quantity + qty)
        )
        res = await session.execute(stmt)
        return res.rowcount


__all__ = ["MenuRepo"]
