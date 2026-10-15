"""Agent sub-flow for the customer's "contact the kitchen" request.

First of the three agent-embedded business flows (``sdd/AgentFeatures.md`` §3).
A customer who has an order open asks, in free text, for one of three things:
hold the kitchen task ("先别上热菜"), resume it ("可以上菜了"), or ask how it is
going ("我的菜好了吗"). This module classifies that message and drives the
simulated KDS through the thin tools in
:mod:`~backend.app.agent.tools.kds_tools`.

Intent recognition (the ``llm_client`` seam reserved for this and feature 3) is
**rules-first**: the clear phrasings above are matched deterministically, and
only an inconclusive message is handed to the LLM as a tie-breaker. A held task
stops the kitchen, so a mis-classification is a real cost -- acting only on a
confident signal, and defaulting the ambiguous case to the read-only "status"
query, keeps the flow safe and offline-correct. There is nothing to recognise
when no LLM endpoint is reachable, so the rules carry the flow on their own.

Layering matches the rest of the package: the facts and the state changes come
from Service-backed tools; this module only decides what was asked and what to
say about it.
"""

from typing import Any, Dict, List, Optional

from backend.app.agent.llm import extract_json_from_text, llm_client
from backend.app.agent.tools.kds_tools import (
    query_kds_status,
    resume_kds_task,
    suspend_kds_task,
)
from backend.app.labels import KDS_STATUS_LABELS

KDS_INTENTS = ("hold", "resume", "status")

# Deterministic intent signals. Checked resume-first: an explicit "you may serve
# now" is unambiguous, while a hold phrase such as "先别上热菜，等人齐了再上"
# also contains a bare "上". A hold deliberately requires an explicit cue too
# (先别 / 推迟 / 晚点 ...) rather than any mention of waiting.
RESUME_KEYWORDS = (
    "可以上",
    "上菜吧",
    "上菜了",
    "恢复上",
    "继续上",
    "尽快上",
    "开始上",
    "开始做",
    "现在上",
    "不用等",
    "菜可以上",
    "resume",
    "go ahead",
    "continue",
    "serve now",
)
HOLD_KEYWORDS = (
    "先别",
    "先不要",
    "先不上",
    "别上",
    "推迟",
    "晚点",
    "晚一些",
    "稍后上",
    "等一下",
    "等下再",
    "慢点",
    "挂起",
    "暂停",
    "暂缓",
    "等人齐",
    "hold",
    "delay",
    "pause",
    "wait",
    "later",
)
STATUS_KEYWORDS = (
    "好了吗",
    "好了没",
    "做好了吗",
    "做好没",
    "还要多久",
    "多久",
    "什么时候",
    "进度",
    "怎么样了",
    "到哪了",
    "出餐了吗",
    "什么情况",
    "status",
    "how long",
    "progress",
    "where is my",
)


def rule_based_kds_intent(message: str) -> Optional[str]:
    """The intent from explicit phrasing, or ``None`` when it is inconclusive."""
    text = (message or "").lower()
    for keywords, intent in (
        (RESUME_KEYWORDS, "resume"),
        (HOLD_KEYWORDS, "hold"),
        (STATUS_KEYWORDS, "status"),
    ):
        if any(keyword in text for keyword in keywords):
            return intent
    return None


async def classify_kds_intent(message: str) -> str:
    """Classify a kitchen message as ``hold`` / ``resume`` / ``status``.

    Rules first (see the module docstring); an inconclusive message is offered to
    the LLM, whose answer is accepted only when it names one of the three
    intents. Anything else -- an unreachable endpoint, an unparseable reply --
    falls back to ``status``, the one intent that changes nothing.
    """
    clear = rule_based_kds_intent(message)
    if clear is not None:
        return clear

    system_prompt = (
        "你是餐厅后厨留言的意图分类器。只判断顾客想做什么，并只输出一个词："
        "hold（请求暂缓 / 推迟出餐）、resume（请求恢复 / 开始出餐）、"
        "status（询问出餐进度）。"
    )
    try:
        raw = await llm_client.generate(prompt=message, system_prompt=system_prompt)
    except Exception:  # defensive: the seam degrades, it should not raise
        return "status"

    parsed = extract_json_from_text(raw)
    if isinstance(parsed, dict):
        candidate = str(parsed.get("intent") or "").strip().lower()
        if candidate in KDS_INTENTS:
            return candidate

    lowered = (raw or "").lower()
    for intent in ("resume", "hold", "status"):
        if intent in lowered:
            return intent
    return "status"


def compose_kds_reply(intent: str, result: Dict[str, Any], *, order_id: int) -> str:
    """The customer-facing reply for the outcome of one kitchen request.

    Separated from the tools so it can be unit-tested on shaped facts alone.
    """
    if not result.get("success"):
        return result.get("error") or "联系后厨失败，请稍后重试。"

    if intent == "hold":
        reason = result.get("held_reason")
        if reason:
            return f"已通知后厨暂缓出餐：{reason}。需要上菜时请回复「可以上菜了」。"
        return "已通知后厨暂缓出餐。需要上菜时请回复「可以上菜了」。"

    if intent == "resume":
        return "已通知后厨恢复出餐，菜品很快就好，请稍候。"

    label = KDS_STATUS_LABELS.get(result.get("kds_status"), result.get("kds_status"))
    reply = f"订单 #{order_id} 的后厨进度：{label}。"
    if result.get("held_reason"):
        reply += f"（已暂缓：{result['held_reason']}）"
    return reply


async def handle_kds_request(
    *, order_id: int, customer_id: int, message: str
) -> Dict[str, Any]:
    """Classify ``message`` and drive the KDS, returning ``{"reply", "intent"}``.

    The reply is always present; ownership and "does this order exist" are the
    HTTP layer's job (it checks them before calling in), so a failure here is a
    business-state outcome -- an illegal move such as resuming a task that was
    never held -- spoken back to the customer rather than raised.
    """
    intent = await classify_kds_intent(message)
    if intent == "hold":
        result = await suspend_kds_task(order_id, customer_id, reason=message)
    elif intent == "resume":
        result = await resume_kds_task(order_id, customer_id)
    else:
        result = await query_kds_status(order_id, customer_id)
    return {
        "reply": compose_kds_reply(intent, result, order_id=order_id),
        "intent": intent,
    }


__all__ = [
    "KDS_INTENTS",
    "classify_kds_intent",
    "compose_kds_reply",
    "handle_kds_request",
    "rule_based_kds_intent",
]
