"""Customer -> Agent routes.

The customer-facing entry point into the embedded Agent flows: an endpoint here
is a *business* endpoint that happens to consult the Agent, not a chat surface
(AgentFeatures §1). Today it hosts the "contact the kitchen" request (§3), where
the customer's free-text message is classified and drives the order's simulated
Kitchen Display System state.

Thin HTTP layer: identity always comes from the JWT, ownership and existence are
checked through the Service before the Agent runs (so the endpoint answers
404 / 403 correctly), and every business-state outcome -- including "you cannot
hold an order that is already out" -- comes back as the Agent's ``reply``.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.kds import handle_kds_request
from backend.app.auth.security import get_current_customer
from backend.app.database import get_db
from backend.app.exceptions import ValidationError
from backend.app.models.user import Customer
from backend.app.schemas.order import AgentKdsResponse, OrderKdsRequest
from backend.app.service.order import OrderService

router = APIRouter(tags=["Customer Agent"])


@router.post("/agent/order/{order_id}/request", response_model=AgentKdsResponse)
async def request_kitchen_contact(
    order_id: int,
    payload: OrderKdsRequest,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """Send the customer's message to the kitchen for one of their orders.

    The message is classified (hold / resume / status) and the order's simulated
    KDS state moves accordingly; the response is the Agent's ``reply``. An empty
    message is a 400, an unknown order a 404 and someone else's order a 403 --
    all decided before the Agent is involved.
    """
    message = (payload.message or "").strip()
    if not message:
        raise ValidationError("请填写要传达给后厨的内容。")
    await OrderService.get_customer_order(
        order_id, current_customer.id, session=db
    )
    result = await handle_kds_request(
        order_id=order_id, customer_id=current_customer.id, message=message
    )
    return AgentKdsResponse(reply=result["reply"])
