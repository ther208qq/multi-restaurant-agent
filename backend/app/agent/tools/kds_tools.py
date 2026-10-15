"""Simulated-KDS tools: thin adapters over :class:`OrderService`.

The "contact the kitchen" flow (AgentFeatures §3) reads and moves the order's
simulated Kitchen Display System state. All rules -- ownership, which KDS moves
are legal, what a hold records -- live in the Service; these adapters only keep
the caller-facing dict contract (``success`` / ``error`` plus a ``code``) that
the KDS sub-flow depends on, and turn the Service's domain exceptions into that
shape. They hold no session and touch no SQLAlchemy directly.

Naming follows the plan: ``query_kds_status`` and ``suspend_kds_task`` (a hold),
plus ``resume_kds_task``. Each also takes ``customer_id`` so the ownership rule
is enforced wherever the tool is called, not only at the HTTP edge.
"""

from typing import Any, Dict, Optional

from backend.app.exceptions import (
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
)
from backend.app.service.order import OrderService


def _error_code(exc: DomainError) -> str:
    """A stable machine code for a domain failure, for the route to map."""
    if isinstance(exc, NotFoundError):
        return "not_found"
    if isinstance(exc, ForbiddenError):
        return "forbidden"
    if isinstance(exc, ConflictError):
        return "conflict"
    return "invalid"


def _failure(exc: DomainError) -> Dict[str, Any]:
    return {"success": False, "error": exc.message, "code": _error_code(exc)}


async def query_kds_status(order_id: int, customer_id: int) -> Dict[str, Any]:
    """The order's current kitchen state, or an ``error`` when it is not yours.

    Returns ``{"success": True, "order_status", "kds_status", "held_reason"}``.
    """
    try:
        order = await OrderService.get_customer_order(order_id, customer_id)
    except DomainError as exc:
        return _failure(exc)
    return {
        "success": True,
        "order_status": order.status,
        "kds_status": order.kds_status,
        "held_reason": order.kds_held_reason,
    }


async def suspend_kds_task(
    order_id: int, customer_id: int, reason: Optional[str] = None
) -> Dict[str, Any]:
    """Hold the order's kitchen task, recording ``reason`` as why."""
    try:
        order = await OrderService.hold_kds_task(order_id, customer_id, reason)
    except DomainError as exc:
        return _failure(exc)
    return {
        "success": True,
        "order_status": order.status,
        "kds_status": order.kds_status,
        "held_reason": order.kds_held_reason,
    }


async def resume_kds_task(order_id: int, customer_id: int) -> Dict[str, Any]:
    """Send a held kitchen task back to the queue."""
    try:
        order = await OrderService.resume_kds_task(order_id, customer_id)
    except DomainError as exc:
        return _failure(exc)
    return {
        "success": True,
        "order_status": order.status,
        "kds_status": order.kds_status,
        "held_reason": order.kds_held_reason,
    }


__all__ = ["query_kds_status", "resume_kds_task", "suspend_kds_task"]
