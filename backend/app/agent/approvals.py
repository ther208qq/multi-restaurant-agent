"""Agent sub-flow for the order-approval gate (``sdd/AgentFeatures.md`` §5).

Third of the three agent-embedded business flows. When a customer places an order
whose notes read as a request for a freebie ("送我一个甜品呗" / "有优惠券吗"),
the order is not placed outright: it enters ``pending_approval`` and a manager
decides. This module is the agent's part of that gate -- it *understands* the
request and *proposes* a gift; the business execution (recording the pending
order, approving, issuing the voucher) stays in the Service layer.

Intent recognition is **LLM-first** (the seam KDS reserved for it): a reachable
``llm_client`` decides whether the note asks for a freebie, and only when it is
unreachable, times out or answers unparseably does the deterministic
:func:`rule_based_gift_intent` take over. The rules are deliberately narrow --
a bare "送" counts as a freebie only when it is *not* part of delivery wording
("送餐" / "送达" / "配送"), so an ordinary delivery note never trips the gate.

The gift proposal reuses feature 2's surplus-inventory tool
(:func:`~backend.app.agent.tools.gift_tools.get_surplus_inventory`, a thin
adapter over ``GiftService``): the agent reads real, currently-available
dessert / drink candidates and recommends the most abundant. It never touches the
database itself -- the facts come through the Service-backed tool.
"""

from typing import Any, Dict, List, Optional

from backend.app.agent.llm import extract_json_from_text, llm_client
from backend.app.agent.tools.gift_tools import get_surplus_inventory

# The agent's reading of an order note, stored on the approval row.
GIFT_REQUEST = "gift_request"
NO_GIFT = "no_gift"

# Strong, unambiguous freebie cues -- any one of these alone decides it. Note that
# "免费" is included, so "免费送一份" is caught without relying on the bare "送".
GIFT_STRONG_KEYWORDS = (
    "免费",
    "赠送",
    "赠品",
    "送我",
    "不要钱",
    "优惠券",
    "券",
    "白送",
    "gift",
    "free",
)

# Delivery wording: a "送" inside one of these is about delivering the order, not
# about a freebie, so the bare-"送" rule must ignore it (AgentFeatures §5 note).
DELIVERY_KEYWORDS = (
    "送餐",
    "送达",
    "送到",
    "送货",
    "送来",
    "送过",
    "送上",
    "送错",
    "送迟",
    "配送",
    "外卖",
    "快递",
)

_GIFT_INTENT_SYSTEM_PROMPT = (
    "你是餐厅订单备注的意图分类器。判断顾客的备注是否在『索要赠品 / 免费的东西』"
    "（例如索要赠品、索要优惠券、要求免费赠送某道菜）。"
    "注意：『送餐』『送达』『配送』『外卖』等描述配送方式的说法不算索要赠品。"
    '只输出 JSON：{"gift_request": true} 或 {"gift_request": false}。'
)


def rule_based_gift_intent(message: str) -> str:
    """The offline verdict -- ``GIFT_REQUEST`` or ``NO_GIFT`` -- from wording.

    A strong cue decides on its own; otherwise a bare "送" counts only when it is
    not part of delivery wording, so "送餐快点" / "麻烦送到前台" stay ``NO_GIFT``.
    """
    text = (message or "").strip().lower()
    if not text:
        return NO_GIFT
    if any(keyword in text for keyword in GIFT_STRONG_KEYWORDS):
        return GIFT_REQUEST
    if "送" in text and not any(keyword in text for keyword in DELIVERY_KEYWORDS):
        return GIFT_REQUEST
    return NO_GIFT


async def _llm_gift_intent(message: str) -> Optional[str]:
    """Ask the LLM; ``None`` when its answer cannot be read as a verdict."""
    raw = await llm_client.generate(
        prompt=message, system_prompt=_GIFT_INTENT_SYSTEM_PROMPT
    )
    parsed = extract_json_from_text(raw)
    if isinstance(parsed, dict) and "gift_request" in parsed:
        return GIFT_REQUEST if parsed["gift_request"] else NO_GIFT
    lowered = (raw or "").strip().lower()
    if "true" in lowered:
        return GIFT_REQUEST
    if "false" in lowered:
        return NO_GIFT
    return None


async def classify_gift_intent(message: str) -> str:
    """Whether an order note asks for a freebie, LLM-first.

    An empty note is never a request. Otherwise the LLM is consulted; only an
    unreachable / unparseable answer falls back to :func:`rule_based_gift_intent`,
    which keeps the gate working with no LLM endpoint at all.
    """
    text = (message or "").strip()
    if not text:
        return NO_GIFT
    try:
        verdict = await _llm_gift_intent(text)
    except Exception:  # defensive: the seam degrades, it should not raise
        verdict = None
    if verdict is not None:
        return verdict
    return rule_based_gift_intent(text)


async def build_gift_suggestion(branch_id: int) -> Optional[Dict[str, Any]]:
    """The agent's gift proposal for a pending order at ``branch_id``.

    Reuses the surplus-inventory tool (feature 2): the dessert / drink items the
    branch can afford to give away, most abundant first. Returns
    ``{"message", "recommended_item", "candidates"}`` -- the recommended pick plus
    the full candidate list with their stock (the "related inventory" the manager
    reads) -- or ``None`` when the branch has nothing to give. The manager is free
    to override the recommendation with any other available item at approval time.
    """
    result = await get_surplus_inventory(branch_id)
    items: List[Dict[str, Any]] = list(result.get("items") or [])
    if not items:
        return None
    recommended = items[0]
    return {
        "message": (
            f"顾客索要赠品，建议赠送「{recommended['name']}」"
            f"（当前库存 {recommended['stock_quantity']} 份）。"
        ),
        "recommended_item": recommended,
        "candidates": items,
    }


async def assess_order_note(
    branch_id: int, special_notes: Optional[str]
) -> Dict[str, Any]:
    """The agent's reading of an order's notes for the approval gate.

    Returns ``{"intent", "suggestion"}``. ``intent`` is ``gift_request`` /
    ``no_gift``; ``suggestion`` is only built when a gift is requested, and is
    ``None`` when the branch has nothing to offer (the order is still gated -- the
    manager can approve with an item of their own choosing, or reject).
    """
    intent = await classify_gift_intent(special_notes or "")
    suggestion = None
    if intent == GIFT_REQUEST:
        suggestion = await build_gift_suggestion(branch_id)
    return {"intent": intent, "suggestion": suggestion}


__all__ = [
    "GIFT_REQUEST",
    "NO_GIFT",
    "GIFT_STRONG_KEYWORDS",
    "DELIVERY_KEYWORDS",
    "assess_order_note",
    "build_gift_suggestion",
    "classify_gift_intent",
    "rule_based_gift_intent",
]
