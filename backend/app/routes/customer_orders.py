"""Customer Orders and Reservations Routes.

Thin HTTP layer: it resolves the authenticated customer and shapes responses;
every business rule (pricing, stock, table selection, the duplicate-request
guard) lives in OrderService / ReservationService, so no query or eager-load
happens here. The customer's identity always comes from the JWT -- never from
the request body -- so a crafted payload cannot place an order or booking on
someone else's behalf.
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.offers import build_agent_offer
from backend.app.auth.security import get_current_customer
from backend.app.database import get_db
from backend.app.models.user import Customer
from backend.app.schemas.gift import GiftResponse
from backend.app.schemas.order import (
    OrderCreate,
    OrderPreviewResponse,
    OrderResponse,
)
from backend.app.schemas.reservation import (
    AgentOffer,
    ReservationAvailabilityItem,
    ReservationAvailabilityResponse,
    ReservationCreate,
    ReservationOfferAccept,
    ReservationOfferAcceptResponse,
    ReservationResponse,
    parse_slot,
)
from backend.app.service.order import OrderService
from backend.app.service.reservation import ReservationService

router = APIRouter(tags=["Customer Activity"])


def _item_payloads(items) -> List[dict]:
    """Turn ``OrderItemCreate`` rows into the plain dicts the Service expects."""
    return [item.model_dump() for item in items]


@router.get("/orders/me", response_model=List[OrderResponse])
async def get_my_orders(
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """Get all orders placed by the current customer across branches."""
    orders = await OrderService.list_orders(
        customer_id=current_customer.id, session=db
    )
    return [
        OrderResponse.from_order(o, customer_name=current_customer.name)
        for o in orders
    ]


@router.post("/orders/preview", response_model=OrderPreviewResponse)
async def preview_my_order(
    payload: OrderCreate,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """Price a would-be order (per-line unit price / subtotal and any stock
    shortfall) without writing anything or reserving stock."""
    preview = await OrderService.preview_order(
        payload.branch_id,
        _item_payloads(payload.items),
        session=db,
    )
    return OrderPreviewResponse.from_preview(preview)


@router.post("/orders", response_model=OrderResponse)
async def create_my_order(
    payload: OrderCreate,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """Place an order for the authenticated customer (status ``placed``).

    ``deduplicate`` is deliberately off here: a customer submitting the same
    basket twice may genuinely want a second helping. The chat/Agent path keeps
    the guard on for a model that emits the same tool call twice.
    """
    order = await OrderService.place_order(
        current_customer.id,
        payload.branch_id,
        payload.order_type,
        _item_payloads(payload.items),
        payload.delivery_address,
        payload.special_notes,
        deduplicate=False,
        session=db,
    )
    # Re-read so the response carries the item menu names and relationships the
    # freshly-created object has not loaded (see ``OrderResponse.from_order``).
    full = await OrderService.get_order(order.id, session=db)
    return OrderResponse.from_order(full, customer_name=current_customer.name)


@router.get(
    "/reservations/availability",
    response_model=ReservationAvailabilityResponse,
)
async def list_my_availability(
    branch_id: int = Query(...),
    date: str = Query(..., description="YYYY-MM-DD"),
    time: str = Query(..., description="HH:MM (24-hour)"),
    party_size: int = Query(2),
    duration_minutes: int = Query(90),
    seating_area: Optional[str] = Query(None),
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """Bookable tables for a slot, plus the agent's upsell offer when it is full.

    When the slot has no free table the endpoint does not merely return an empty
    list: it embeds the Agent's counter-proposal (a nearby time and a surplus
    dessert / drink) as ``agent_offer`` (AgentFeatures §4), which the booking form
    turns into an "accept this arrangement" prompt.
    """
    start_time = parse_slot(date, time)
    tables = await ReservationService.list_available_tables(
        branch_id=branch_id,
        start_time=start_time,
        party_size=party_size,
        duration_minutes=duration_minutes,
        seating_area=seating_area,
        session=db,
    )
    offer = None
    if not tables:
        offer_data = await build_agent_offer(
            branch_id=branch_id,
            start_time=start_time,
            party_size=party_size,
            duration_minutes=duration_minutes,
            seating_area=seating_area,
        )
        if offer_data is not None:
            offer = AgentOffer(**offer_data)
    return ReservationAvailabilityResponse(
        tables=[ReservationAvailabilityItem.from_table(t) for t in tables],
        agent_offer=offer,
    )


@router.post(
    "/reservations/accept-offer", response_model=ReservationOfferAcceptResponse
)
async def accept_reservation_offer(
    payload: ReservationOfferAccept,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """Accept the agent's arrangement: book the nearby time and take the gift.

    The booking and the gift voucher are created together in the Service, so the
    customer either gets both or neither.
    """
    reservation, gift = await ReservationService.accept_offer(
        current_customer.id,
        payload.branch_id,
        payload.to_start_time(),
        payload.party_size,
        payload.duration_minutes,
        payload.seating_area_preference,
        payload.gift_item_id,
        payload.notes,
        session=db,
    )
    return ReservationOfferAcceptResponse(
        reservation=ReservationResponse.from_reservation(
            reservation, customer_name=current_customer.name
        ),
        gift=GiftResponse.from_gift(gift) if gift is not None else None,
    )


@router.post("/reservations", response_model=ReservationResponse)
async def create_my_reservation(
    payload: ReservationCreate,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """Book a table for the authenticated customer (status ``pending``).

    The system picks the table by area -- customers do not choose a table
    number. ``deduplicate`` is on: a repeat submission (double click, retry)
    must return the booking already made rather than hold a second table for the
    same party, which the exclusion constraint alone cannot catch.
    """
    reservation = await ReservationService.book_table(
        current_customer.id,
        payload.branch_id,
        payload.to_start_time(),
        payload.party_size,
        payload.duration_minutes,
        payload.seating_area_preference,
        payload.notes,
        deduplicate=True,
        session=db,
    )
    return ReservationResponse.from_reservation(
        reservation, customer_name=current_customer.name
    )


@router.get("/reservations/me", response_model=List[ReservationResponse])
async def get_my_reservations(
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """Get all table reservations booked by the current customer across branches."""
    reservations = await ReservationService.list_reservations(
        customer_id=current_customer.id, descending=True, session=db
    )
    return [
        ReservationResponse.from_reservation(
            r, customer_name=current_customer.name
        )
        for r in reservations
    ]
