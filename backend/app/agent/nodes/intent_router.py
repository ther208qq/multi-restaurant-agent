"""Intent Router Node for LangGraph."""

import re
from typing import Any, Dict, Optional

from langchain_core.messages import AIMessage

from backend.app.agent.flows import DRAFT_KEYS, is_cancel, pending_flow
from backend.app.agent.llm import extract_json_from_text, llm_client

# Keyword fast path, matched on word boundaries so "order" no longer fires inside
# unrelated words. Chinese patterns are plain substrings: Chinese has no word
# boundaries, and "\b" behaves erratically next to CJK characters.
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
    # Chinese: "status", "my order/reservation", "check my ...", "how far along"
    r"状态",
    r"我的\s*(?:订单|预订|订位|订座|桌子|桌位)",
    r"(?:查|查询|查看)(?:一下|下)?\s*我",
    r"我(?:的)?\s*(?:订单|预订|订位|订座)",
    r"进度",
    r"到哪(?:儿|里)?了",
    r"(?:订单|预订|订位)\s*(?:号|编号)",
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
    # Chinese: "order food", "I'd like ...", menu nouns, quantity + serving.
    # Bare "点" is deliberately excluded — it also means o'clock ("7 点").
    r"点(?:单|餐|菜|个|一份)",
    r"我想点",
    r"帮我点",
    r"来(?:一|两|三|几|\d+)\s*(?:份|个|杯|瓶)",
    r"\d+\s*份",
    r"(?:披萨|比萨|意面|意大利面|饮料|菜单|提拉米苏|柠檬水|可乐|菜品|食物|小吃|甜点)",
    r"(?:买|吃|要一份|要两份)",
)
_RESERVATION_PATTERNS = (
    r"\breserve",
    r"\breservations?\b",
    r"\btables?\b",
    r"\bbook",
    r"\bseat",
    # Chinese: "reserve/book a table", "table", "dine in"
    r"(?:预订|预定|订位|订座|订桌|订一张|订个)",
    r"订\s*(?:一)?\s*(?:张|个|桌)",
    r"(?:座位|桌子|桌位|餐桌)",
    r"(?:就餐|用餐)",
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
    # Chinese: hours / address / location / phone / parking / dress code
    r"营业",
    r"(?:开门|关门|打烊)",
    r"几点\s*(?:开门|关门|营业|打烊)",
    r"(?:地址|位置|在哪)",
    r"(?:电话|联系方式)",
    r"(?:停车|车位)",
    r"(?:着装|穿着|dress\s*code)",
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
            reply = "好的，已为您取消当前操作。请问还需要什么帮助？"
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
    sys_prompt = """你是餐厅 AI 助手的意图分类系统。
请将用户的意图归类为以下 JSON 格式中的且仅一个（中英文消息都同样处理）：
{"intent": "order"}
{"intent": "reservation"}
{"intent": "status"}
{"intent": "faq"}
{"intent": "general"}

只输出合法的 JSON，不要输出任何对话性内容。
"""
    raw_response = await llm_client.generate(prompt=last_user_msg, system_prompt=sys_prompt)
    parsed = extract_json_from_text(raw_response)
    intent = parsed.get("intent", "general") if parsed else "general"

    if intent in ["order", "reservation", "status", "faq"]:
        return {**state, "next_node": intent}

    # General greeting / fallback
    reply = "您好！我可以帮您查看菜单、点餐、预订座位，或查询已有订单与预订的状态。请问需要什么帮助？"
    return {
        **state,
        "next_node": "end",
        "last_reply": reply,
        "messages": [AIMessage(content=reply)],
    }
