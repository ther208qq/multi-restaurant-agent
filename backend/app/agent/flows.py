"""Lifecycle rules for in-progress order / reservation drafts.

The agent keeps a half-filled order or booking in the graph state
(``order_draft`` / ``reservation_draft``) so that a slot-filling answer such as
"4 people, tomorrow at 7pm" continues the flow it belongs to instead of being
re-classified from scratch.

That state outlives the session: it lives in the PostgreSQL checkpointer and
``routes/chat.py`` feeds it back into every new turn. Without the rules below, a
single abandoned draft hijacks the conversation *permanently* -- once a
reservation draft existed, "what's on the menu?" and "where is my order?" were
both answered with the same "how many guests?" prompt, with no way out.

Two rules keep drafts useful without letting them trap the customer:

* a draft only *continues* a flow -- it never overrides a message that clearly
  asks for something else (see ``intent_router``);
* a draft expires (``DRAFT_TTL_MINUTES``) and an explicit cancel clears it.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

DRAFT_TTL_MINUTES = 15

FLOW_DRAFT_KEYS = {"order": "order_draft", "reservation": "reservation_draft"}
DRAFT_KEYS = tuple(FLOW_DRAFT_KEYS.values())

# Explicit "drop what we were doing" phrases. Unlike a bare "no" (which answers
# a confirmation prompt), these always abandon the in-progress flow.
CANCEL_PATTERNS = (
    r"\bcancel\b",
    r"\bnever ?mind\b",
    r"\bforget it\b",
    r"\bstart over\b",
    r"\breset\b",
    r"\babort\b",
)


def stamp(draft: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Date a draft so :func:`is_stale` can expire it.

    Called wherever a node stores a draft, so the clock starts when the customer
    last moved the flow forward rather than when the session started.
    """
    if not draft:
        return draft
    return {**draft, "updated_at": datetime.now(timezone.utc).isoformat()}


def is_cancel(text: str) -> bool:
    """Whether ``text`` explicitly abandons whatever is in progress."""
    return any(re.search(pattern, text) for pattern in CANCEL_PATTERNS)


def is_stale(
    draft: Optional[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    ttl_minutes: int = DRAFT_TTL_MINUTES,
) -> bool:
    """Whether a present draft is too old to keep steering the conversation.

    An undated draft counts as stale: drafts written before stamping existed
    still sit in persisted checkpoints, and one that can never age out would
    trap the customer in a flow they have long since walked away from. An absent
    draft is *not* stale -- it is simply absent.
    """
    if not draft:
        return False
    raw = draft.get("updated_at")
    if not raw:
        return True
    try:
        updated = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return True
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - updated > timedelta(minutes=ttl_minutes)


def pending_flow(state: Dict[str, Any]) -> Optional[str]:
    """The flow still waiting on the customer, or ``None`` if there is none."""
    for flow, key in FLOW_DRAFT_KEYS.items():
        draft = state.get(key)
        if draft and not is_stale(draft):
            return flow
    return None


__all__ = [
    "CANCEL_PATTERNS",
    "DRAFT_KEYS",
    "DRAFT_TTL_MINUTES",
    "FLOW_DRAFT_KEYS",
    "is_cancel",
    "is_stale",
    "pending_flow",
    "stamp",
]
