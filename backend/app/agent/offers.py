"""Agent sub-flow for the booking upsell ("逼单") offer.

Second of the three agent-embedded business flows (``sdd/AgentFeatures.md`` §4).
When a branch is fully booked for the slot a customer asked for, the
availability endpoint does not just return an empty list: it asks this module for
a counter-offer -- a nearby free time plus a surplus dessert / drink as a gift.

Layering follows the rest of the agent package: the facts come from Service-backed
tools (:mod:`~backend.app.agent.tools.gift_tools` for the surplus inventory,
:mod:`~backend.app.agent.tools.table_tools` for the nearby times); this module
only decides what to say about them. The wording is composed locally from those
facts rather than by an LLM call, so the availability query -- which runs on every
keystroke in the booking modal -- stays fast and deterministic and keeps working
with no LLM endpoint reachable. The shared ``llm_client`` seam is available when a
flow does need free-form language (see the order-approval intent node).
"""

from typing import Any, Dict, Optional

from backend.app.agent.tools.gift_tools import get_surplus_inventory
from backend.app.agent.tools.table_tools import suggest_reservation_times

__all__ = ["build_agent_offer", "compose_offer_message"]


def compose_offer_message(
    *,
    alternative_times: list,
    gift: Optional[Dict[str, Any]] = None,
    party_size: int = 2,
) -> str:
    """The upsell line for a set of nearby times and an optional gift.

    Kept separate from the data fetch so it can be unit-tested on facts alone.
    Assumes ``alternative_times`` is non-empty (the offer is not built without
    at least one rebooking option).
    """
    times_text = "、".join(alternative_times)
    message = (
        f"您选的时间已经订满了。不过 {party_size} 位的话，{times_text} 都还有空桌。"
    )
    if gift:
        message += f"现在改约，我们再送您一份「{gift['name']}」。"
    message += "要为您安排吗？"
    return message


async def build_agent_offer(
    *,
    branch_id: int,
    start_time: Any,
    party_size: int,
    duration_minutes: int = 90,
    seating_area: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """The upsell counter-offer for a full slot, or ``None`` when there is none.

    Returns ``{"message", "alternative_times", "gift_item"}`` -- the shape the
    ``agent_offer`` field of the availability response carries. There is no offer
    to make when nothing nearby is free either, so that case returns ``None``
    rather than a message that cannot be acted on.
    """
    times_result = await suggest_reservation_times(
        branch_id=branch_id,
        start_time=start_time,
        party_size=party_size,
        duration_minutes=duration_minutes,
        seating_area=seating_area,
    )
    alternative_times = list(times_result.get("times") or [])
    if not alternative_times:
        return None

    surplus_result = await get_surplus_inventory(branch_id)
    items = list(surplus_result.get("items") or [])
    gift = items[0] if items else None

    return {
        "message": compose_offer_message(
            alternative_times=alternative_times, gift=gift, party_size=party_size
        ),
        "alternative_times": alternative_times,
        "gift_item": gift,
    }
