"""Order and OrderItem Schemas."""

from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict


def _loaded(obj: Any, name: str) -> Any:
    """Read an already-loaded relationship without triggering a lazy load.

    The response builders below run in async context, where touching an
    unloaded relationship raises instead of fetching it. Reading the instance
    dict returns the value when the caller eager-loaded it and ``None`` when it
    did not -- the fields are optional, so ``None`` is the right answer either
    way, and it can never fire an implicit query.
    """
    return obj.__dict__.get(name)


class OrderItemCreate(BaseModel):
    menu_item_id: int
    quantity: int = 1
    selected_variants: Optional[List[Dict[str, Any]]] = None
    item_notes: Optional[str] = None


class OrderItemResponse(BaseModel):
    id: int
    order_id: int
    menu_item_id: int
    quantity: int
    unit_price: float
    selected_variants: Optional[List[Dict[str, Any]]] = None
    item_notes: Optional[str] = None
    menu_item_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_item(cls, item: Any) -> "OrderItemResponse":
        """Build the line response from an ``OrderItem`` (see ``_loaded``)."""
        menu_item = _loaded(item, "menu_item")
        return cls(
            id=item.id,
            order_id=item.order_id,
            menu_item_id=item.menu_item_id,
            quantity=item.quantity,
            unit_price=item.unit_price,
            selected_variants=item.selected_variants,
            item_notes=item.item_notes,
            menu_item_name=menu_item.name if menu_item else None,
        )


class OrderCreate(BaseModel):
    branch_id: int
    order_type: str = "dine_in"  # "dine_in", "takeaway", "delivery"
    delivery_address: Optional[str] = None
    special_notes: Optional[str] = None
    items: List[OrderItemCreate]


class OrderStatusUpdate(BaseModel):
    status: str  # placed, confirmed, preparing, ready, delivered, cancelled


class OrderKdsRequest(BaseModel):
    """A customer's free-text message to the kitchen for one of their orders."""

    message: str


class AgentKdsResponse(BaseModel):
    """The agent's spoken reply to a "contact the kitchen" request (§3)."""

    reply: str


class OrderPreviewLine(BaseModel):
    """One priced line of an order preview (see ``OrderService.preview_order``)."""

    menu_item_id: int
    name: str
    qty: int
    unit_price: float
    subtotal: float
    selected_variants: List[Dict[str, Any]] = []
    available_quantity: int
    shortfall: int

    @classmethod
    def from_preview(cls, line: Dict[str, Any]) -> "OrderPreviewLine":
        return cls(**line)


class OrderPreviewResponse(BaseModel):
    """The read-only cost estimate for a would-be order -- nothing is written."""

    items: List[OrderPreviewLine] = []
    total_amount: float
    has_shortfall: bool

    @classmethod
    def from_preview(cls, preview: Dict[str, Any]) -> "OrderPreviewResponse":
        return cls(
            items=[OrderPreviewLine.from_preview(line) for line in preview["items"]],
            total_amount=preview["total_amount"],
            has_shortfall=preview["has_shortfall"],
        )


class OrderResponse(BaseModel):
    id: int
    customer_id: int
    branch_id: int
    order_type: str
    status: str
    delivery_address: Optional[str] = None
    special_notes: Optional[str] = None
    total_amount: float
    # Simulated KDS state (AgentFeatures §3): "queued", "cooking", "held", "done".
    kds_status: str = "queued"
    kds_held_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    items: List[OrderItemResponse] = []
    branch_name: Optional[str] = None
    customer_name: Optional[str] = None
    # Set when this order is gated for manager approval (AgentFeatures §5): the
    # id of the ``OrderApproval`` the manager acts on. ``None`` for a normal order.
    approval_id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_order(
        cls,
        order: Any,
        *,
        customer_name: Optional[str] = None,
        approval_id: Optional[int] = None,
    ) -> "OrderResponse":
        """Build the response from an ``Order`` ORM object.

        Shared by the staff order board and the customer's own order endpoints so
        both describe an order identically. ``customer_name`` is passed by the
        customer endpoints, which already know it from the authenticated caller;
        otherwise it falls back to the loaded relationship.
        """
        loaded_customer = _loaded(order, "customer")
        loaded_branch = _loaded(order, "branch")
        return cls(
            id=order.id,
            customer_id=order.customer_id,
            branch_id=order.branch_id,
            order_type=order.order_type,
            status=order.status,
            delivery_address=order.delivery_address,
            special_notes=order.special_notes,
            total_amount=order.total_amount,
            kds_status=order.kds_status,
            kds_held_reason=order.kds_held_reason,
            created_at=order.created_at,
            updated_at=order.updated_at,
            items=[OrderItemResponse.from_item(item) for item in order.items],
            branch_name=loaded_branch.name if loaded_branch else None,
            customer_name=customer_name
            if customer_name is not None
            else (loaded_customer.name if loaded_customer else None),
            approval_id=approval_id,
        )
