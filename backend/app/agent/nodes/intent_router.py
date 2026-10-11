"""Intent Router Node for LangGraph."""

import re
from typing import Any, Dict, Optional

from langchain_core.messages import AIMessage

from backend.app.agent.flows import DRAFT_KEYS, is_cancel, pending_flow
from backend.app.agent.llm import extract_json_from_text, llm_client

# Keyword fast path, matched on word boundaries so "order" no longer fires inside
# unrelated words.
#
# Order matters. A customer *asking about* an existing order or booking ("where is
# my order?") must win over the bare nouns that place one ("order"), so the status
# phrases are matched before the order/menu keywords.
_STATUS_PATTERNS = (
    r"\bstatus\b",
    r"\btrack\b",
    r"\bwhere(?:'s| is| are)\s+my\b",
    r"\bmy\s+(?:orders?|reservations?|bookings?|tables?)\b",
    r"\bdid\s+my\b",
    r"\bcheck\s+(?:on\s+)?my\b",
)
_ORDER_PATTERNS = (
    r"\b(?:re)?order(?:s|ing)?\b",
    r"\bpizzas?\b",
    r"\bpastas?\b",
    r"\bdrinks?\b",
    r"\bbuy\b",
    r"\bcart\b",
    r"\beat\b",
    r"\bmenus?\b",
    r"\bfood\b",
    r"\btiramisu\b",
    r"\blemonade\b",
    r"\bcoke\b",
)
_RESERVATION_PATTERNS = (
    r"\breserve",
    r"\breservations?\b",
    r"\btables?\b",
    r"\bbook",
    r"\bseat",
)
_FAQ_PATTERNS = (
    r"\bhours?\b",
    r"\bopen",
    r"\bclose",
    r"\btimes?\b",
    r"\baddress\b",
    r"\blocation\b",
    r"\bwhere are\b",
    r"\bphone\b",
    r"\bpark",
    r"\bdress\b",
)
_KEYWORD_ROUTES = (
    ("status", _STATUS_PATTERNS),
    ("order", _ORDER_PATTERNS),
    ("reservation", _RESERVATION_PATTERNS),
    ("faq", _FAQ_PATTERNS),
)


def keyword_route(text: str) -> Optional[str]:
    """Return the first matching keyword route for ``text``, or ``None``."""
    for route, patterns in _KEYWORD_ROUTES:
        if any(re.search(pattern, text) for pattern in patterns):
            return route
    return None


def _cleared_drafts() -> Dict[str, Any]:
    """State fields that abandon every in-progress draft."""
    return {"active_flow": "none", **{key: None for key in DRAFT_KEYS}}


async def intent_router_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Route customer request based on active flow or LLM intent classification."""
    # If branch is not set, must route to branch selector first
    if not state.get("branch_id"):
        return {**state, "next_node": "branch_selector"}

    messages = state.get("messages", [])
    if not messages:
        return {**state, "next_node": "general"}

    last_user_msg = messages[-1].content.strip().lower()
    route = keyword_route(last_user_msg)

    # A half-filled draft only *continues* the flow it belongs to; it never
    # overrides a message that asks for something else. Letting it win meant one
    # abandoned booking swallowed every later question ("what's on the menu?")
    # for the rest of the thread's life.
    pending = pending_flow(state)
    if pending:
        if is_cancel(last_user_msg):
            reply = "No problem — I've cleared that. What would you like to do instead?"
            return {
                **state,
                **_cleared_drafts(),
                "next_node": "end",
                "last_reply": reply,
                "messages": [AIMessage(content=reply)],
            }
        if route is None or route == pending:
            return {**state, "active_flow": pending, "next_node": pending}
        # Otherwise serve the new intent below and keep the draft for later.
    else:
        # Nothing in progress: drop any draft that has aged out, so it stops
        # being reported as active (it lingers in the checkpointer otherwise).
        leftovers = {key: None for key in DRAFT_KEYS if state.get(key)}
        if leftovers:
            state = {**state, "active_flow": "none", **leftovers}

    # Keyword fast path for reliability & speed
    if route:
        return {**state, "next_node": route}

    # Fallback to DeepSeek R1 classification
    sys_prompt = """You are an intent classification system for a restaurant AI assistant.
Classify the user's intent into EXACTLY ONE of the following JSON formats:
{"intent": "order"}
{"intent": "reservation"}
{"intent": "status"}
{"intent": "faq"}
{"intent": "general"}

Output ONLY valid JSON. Do not output conversational filler.
"""
    raw_response = await llm_client.generate(prompt=last_user_msg, system_prompt=sys_prompt)
    parsed = extract_json_from_text(raw_response)
    intent = parsed.get("intent", "general") if parsed else "general"

    if intent in ["order", "reservation", "status", "faq"]:
        return {**state, "next_node": intent}

    # General greeting / fallback
    reply = "Hello! I can help you view our menu, order delicious food, book a table reservation, or check the status of your existing orders and bookings. What would you like to do?"
    return {
        **state,
        "next_node": "end",
        "last_reply": reply,
        "messages": [AIMessage(content=reply)],
    }
