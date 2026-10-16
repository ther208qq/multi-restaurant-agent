"""Staff Orders Management Routes.

Thin HTTP layer: request parsing, branch scoping and response shaping only.
All order business rules (status transitions, stock reversal) live in
:class:`backend.app.service.order.OrderService`; domain exceptions raised there
are mapped to HTTP status codes by the global exception handler.
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.security import get_current_staff
from backend.app.database import get_db
from backend.app.models.user import StaffUser
from backend.app.schemas.order import OrderKdsUpdate, OrderResponse, OrderStatusUpdate
from backend.app.service.order import OrderService

router = APIRouter(prefix="/staff/orders", tags=["Staff Orders Management"])


@router.get("", response_model=List[OrderResponse])
async def list_staff_orders(
    branch_id: Optional[int] = Query(None),
    order_status: Optional[str] = Query(None, alias="status"),
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """List orders, optionally filtered by branch and status."""
    # Scoping: a branch-restricted staff member always sees their own branch;
    # otherwise the optional ?branch_id filter applies.
    effective_branch = current_staff.branch_id or branch_id
    orders = await OrderService.list_orders(
        branch_id=effective_branch,
        order_status=order_status,
        session=db,
    )
    return [OrderResponse.from_order(o) for o in orders]


@router.patch("/{order_id}/status", response_model=OrderResponse)
async def update_order_status(
    order_id: int,
    payload: OrderStatusUpdate,
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """Update order status through the legal lifecycle (placed -> confirmed ->
    preparing -> ready -> delivered, or cancelled from any non-terminal state)."""
    order = await OrderService.update_status(
        order_id,
        payload.status,
        staff=current_staff,
        session=db,
    )
    return OrderResponse.from_order(order)


@router.patch("/{order_id}/kds", response_model=OrderResponse)
async def update_order_kds_status(
    order_id: int,
    payload: OrderKdsUpdate,
    current_staff: StaffUser = Depends(get_current_staff),
    db: AsyncSession = Depends(get_db),
):
    """Advance the order's simulated KDS state (the kitchen's side).

    The staff drives ``queued -> cooking -> done`` and can send a held task back
    to the queue. Legal moves are enforced by the Service's KDS transition matrix.
    """
    order = await OrderService.update_kds_status(
        order_id,
        payload.status,
        staff=current_staff,
        session=db,
    )
    return OrderResponse.from_order(order)
