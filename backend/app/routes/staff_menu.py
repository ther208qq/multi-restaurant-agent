"""Staff Menu and Inventory Management Routes."""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.security import get_current_staff
from backend.app.database import get_db
from backend.app.models.user import StaffUser
from backend.app.schemas.menu import (
    MenuCategoryCreate,
    MenuCategoryResponse,
    MenuItemCreate,
    MenuItemResponse,
    MenuItemStockResponse,
    MenuItemStockUpdate,
    MenuItemUpdate,
)
from backend.app.service.menu import MenuService, item_payload

router = APIRouter(prefix="/staff/menu", tags=["Staff Menu & Inventory Management"])


# Categories
@router.get("/categories", response_model=List[MenuCategoryResponse])
async def list_categories(
    branch_id: Optional[int] = Query(None),
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """List menu categories for a branch."""
    return await MenuService.list_categories(current_staff, branch_id, session=db)


@router.post("/categories", response_model=MenuCategoryResponse)
async def create_category(
    payload: MenuCategoryCreate,
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """Create a new menu category."""
    return await MenuService.create_category(
        current_staff, payload.branch_id, payload.name, session=db
    )


# Items
@router.post("/items", response_model=MenuItemResponse)
async def create_item(
    payload: MenuItemCreate,
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """Create a new menu item and initialize its stock."""
    item, stock = await MenuService.create_item(
        current_staff,
        category_id=payload.category_id,
        name=payload.name,
        description=payload.description,
        base_price=payload.base_price,
        is_available=payload.is_available,
        variants=[v.model_dump() for v in (payload.variants or [])],
        initial_stock=payload.initial_stock,
        session=db,
    )
    # ``item_payload`` applies the centralized availability rule.
    return MenuItemResponse(**item_payload(item, stock.stock_quantity))


@router.patch("/items/{item_id}", response_model=MenuItemResponse)
async def update_item(
    item_id: int,
    payload: MenuItemUpdate,
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """Update item details or toggle availability."""
    item = await MenuService.update_item(
        current_staff, item_id, payload.model_dump(exclude_unset=True), session=db
    )

    stock_val = item.stocks[0].stock_quantity if item.stocks else 0
    # ``item_payload`` applies the centralized availability rule and variant shape.
    return MenuItemResponse(**item_payload(item, stock_val, item.variants))


# Stock Management
@router.put("/stock", response_model=MenuItemStockResponse)
async def update_stock(
    payload: MenuItemStockUpdate,
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """Update or restock quantity for a menu item at a branch."""
    return await MenuService.update_stock(
        current_staff,
        payload.menu_item_id,
        payload.branch_id,
        payload.stock_quantity,
        session=db,
    )
