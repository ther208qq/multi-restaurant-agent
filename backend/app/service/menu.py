"""Menu and inventory business service.

Owns the transaction boundary and hosts the single definition of the menu
availability rule; reused by the public menu API, the staff menu API and the
Agent's menu tool.
"""

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import transactional_session
from backend.app.exceptions import ForbiddenError, NotFoundError, ValidationError
from backend.app.models.menu import (
    MenuCategory,
    MenuItem,
    MenuItemStock,
    MenuItemVariant,
)
from backend.app.models.user import StaffUser
from backend.app.repo.branch import BranchRepo
from backend.app.repo.menu import MenuRepo

DEFAULT_INITIAL_STOCK = 50
_UPDATABLE_ITEM_FIELDS = ("name", "description", "base_price", "is_available")


def compute_effective_available(is_available: bool, stock_quantity: int) -> bool:
    """Single definition of the menu availability rule.

    An item is effectively available only when staff have not disabled it and
    the branch still has stock.
    """
    return bool(is_available) and (stock_quantity or 0) > 0


def variant_payload(variant: MenuItemVariant) -> Dict[str, Any]:
    """Staff-facing variant shape (matches MenuItemVariantResponse)."""
    return {
        "id": variant.id,
        "menu_item_id": variant.menu_item_id,
        "variant_type": variant.variant_type,
        "variant_name": variant.variant_name,
        "price_delta": variant.price_delta,
    }


def item_payload(
    item: MenuItem,
    stock_quantity: int,
    variants: Optional[List[MenuItemVariant]] = None,
) -> Dict[str, Any]:
    """Staff-facing menu item shape with the centralized availability rule."""
    return {
        "id": item.id,
        "category_id": item.category_id,
        "name": item.name,
        "description": item.description,
        "base_price": item.base_price,
        "is_available": item.is_available,
        "stock_quantity": stock_quantity,
        "effective_available": compute_effective_available(
            item.is_available, stock_quantity
        ),
        "variants": [variant_payload(v) for v in (variants or [])],
    }


def _menu_item_payload(item, stock_quantity):
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "base_price": item.base_price,
        "is_available": item.is_available,
        "stock_quantity": stock_quantity,
        "effective_available": compute_effective_available(item.is_available, stock_quantity),
        "variants": [
            {
                "id": v.id,
                "type": v.variant_type,
                "name": v.variant_name,
                "price_delta": v.price_delta,
            }
            for v in item.variants
        ],
    }


def _menu_payload(branch, categories, stock_map):
    payload = {"branch_id": branch.id, "branch_name": branch.name, "categories": []}
    for category in categories:
        items = [
            _menu_item_payload(item, stock_map.get(item.id, 0))
            for item in category.items
        ]
        # A category with nothing in it is junk on a customer-facing menu -- a
        # leftover test row, say. Skip it so a bare "[Name]" header never reaches
        # the public API or the Agent prompt. (Staff still see it via
        # MenuService.list_categories, which reads categories directly.)
        if not items:
            continue
        payload["categories"].append(
            {"id": category.id, "name": category.name, "items": items}
        )
    return payload


class MenuService:
    @staticmethod
    async def get_branch_menu(branch_id: int, *, session: Optional[AsyncSession] = None) -> Dict[str, Any]:
        """Full active menu for a branch (public API + Agent menu tool)."""
        async with transactional_session(session) as s:
            branch = await BranchRepo.get_branch(s, branch_id, active_only=True)
            if branch is None:
                raise NotFoundError(f"门店 #{branch_id} 不存在或未营业。")
            categories = await MenuRepo.list_categories(s, branch_id)
            stock_map = await MenuRepo.stock_map(s, branch_id)
            return _menu_payload(branch, categories, stock_map)

    @staticmethod
    async def list_categories(
        staff: StaffUser,
        branch_id: Optional[int] = None,
        *,
        session: Optional[AsyncSession] = None,
    ) -> List[MenuCategory]:
        async with transactional_session(session) as s:
            resolved_branch_id = staff.branch_id or branch_id or 1
            return await MenuRepo.list_categories(s, resolved_branch_id)

    @staticmethod
    async def create_category(
        staff: StaffUser,
        branch_id: int,
        name: str,
        *,
        session: Optional[AsyncSession] = None,
    ) -> MenuCategory:
        if staff.branch_id and branch_id != staff.branch_id:
            raise ForbiddenError("您没有权限访问该门店。")
        async with transactional_session(session) as s:
            return await MenuRepo.create_category(s, branch_id, name)

    @staticmethod
    async def create_item(
        staff: StaffUser,
        *,
        category_id: int,
        name: str,
        description: Optional[str],
        base_price: float,
        is_available: bool,
        variants: List[Dict[str, Any]],
        initial_stock: Optional[int] = None,
        session: Optional[AsyncSession] = None,
    ) -> Tuple[MenuItem, MenuItemStock]:
        """Create an item with its variants and initial stock for the branch."""
        async with transactional_session(session) as s:
            category = await MenuRepo.get_category(s, category_id)
            if category is None:
                raise NotFoundError("菜品分类不存在。")
            if staff.branch_id and category.branch_id != staff.branch_id:
                raise ForbiddenError("您没有权限访问该门店。")
            item = await MenuRepo.create_item(
                s,
                category_id=category_id,
                name=name,
                description=description,
                base_price=base_price,
                is_available=is_available,
                variants=variants,
            )
            stock_quantity = (
                initial_stock if initial_stock is not None else DEFAULT_INITIAL_STOCK
            )
            stock = await MenuRepo.set_stock(
                s,
                menu_item_id=item.id,
                branch_id=category.branch_id,
                stock_quantity=stock_quantity,
            )
            return item, stock

    @staticmethod
    async def update_item(
        staff: StaffUser,
        item_id: int,
        values: Dict[str, Any],
        *,
        session: Optional[AsyncSession] = None,
    ) -> MenuItem:
        async with transactional_session(session) as s:
            item = await MenuRepo.get_item(
                s, item_id, with_category=True, with_variants=True, with_stocks=True
            )
            if item is None:
                raise NotFoundError("菜品不存在。")
            if staff.branch_id and item.category.branch_id != staff.branch_id:
                raise ForbiddenError("您没有权限访问该门店。")
            updates = {
                key: value
                for key, value in values.items()
                if key in _UPDATABLE_ITEM_FIELDS and value is not None
            }
            return await MenuRepo.update_item(s, item, updates)

    @staticmethod
    async def update_stock(
        staff: StaffUser,
        menu_item_id: int,
        branch_id: int,
        stock_quantity: int,
        *,
        session: Optional[AsyncSession] = None,
    ) -> MenuItemStock:
        if staff.branch_id and branch_id != staff.branch_id:
            raise ForbiddenError("您没有权限访问该门店。")
        if stock_quantity < 0:
            raise ValidationError("库存数量不能为负数。")
        async with transactional_session(session) as s:
            return await MenuRepo.set_stock(
                s,
                menu_item_id=menu_item_id,
                branch_id=branch_id,
                stock_quantity=stock_quantity,
            )


__all__ = [
    "MenuService",
    "compute_effective_available",
    "item_payload",
    "variant_payload",
    "DEFAULT_INITIAL_STOCK",
]
