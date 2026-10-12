"""Order Flow LangGraph Node."""

import json
import re
from typing import Any, Dict
from langchain_core.messages import AIMessage

from backend.app.agent.flows import stamp
from backend.app.agent.labels import order_type_label
from backend.app.agent.llm import extract_json_from_text, llm_client
from backend.app.agent.tools.menu_tools import format_menu_for_prompt, get_branch_menu_data
from backend.app.agent.tools.order_tools import create_order_atomic
from backend.app.service.order import OrderService


async def order_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Handles menu presentation, item selection, stock checks, order summary, and atomic creation."""
    branch_id = state.get("branch_id")
    customer_id = state.get("customer_id")
    messages = state.get("messages", [])
    user_msg = messages[-1].content.strip() if messages else ""
    user_lower = user_msg.lower()

    # 1. Handle menu browsing request
    if any(
        k in user_lower
        for k in [
            "what's on the menu",
            "show menu",
            "view menu",
            "see menu",
            "what do you have",
            "what pizzas",
            # Chinese: the frontend's menu chip and its close paraphrases
            "菜单上有什么",
            "菜单里有什么",
            "有什么菜",
            "看看菜单",
            "看下菜单",
        ]
    ):
        menu_text = await format_menu_for_prompt(branch_id)
        reply = (
            f"以下是本门店当前的菜单与实时库存：\n\n"
            f"{menu_text}\n\n"
            f"请问您想点些什么？（请注明数量、规格，以及用餐方式：堂食、外带或外卖）"
        )
        return {
            **state,
            "active_flow": "order",
            "last_reply": reply,
            "messages": [AIMessage(content=reply)],
        }

    # 2. Check if user is confirming an existing order draft
    order_draft = state.get("order_draft")
    if order_draft and order_draft.get("awaiting_confirmation"):
        if any(
            w in user_lower
            for w in [
                "yes",
                "confirm",
                "place order",
                "go ahead",
                "sure",
                "yep",
                "ok",
                "okay",
                # Chinese confirmations ("是的"/"好的" rather than a bare "是",
                # which also appears inside "不是")
                "确认",
                "下单",
                "好的",
                "可以",
                "是的",
                "没问题",
                "就这些",
                "对的",
                "没错",
            ]
        ):
            # Execute atomic order creation + stock decrement
            result = await create_order_atomic(
                customer_id=customer_id,
                branch_id=branch_id,
                order_type=order_draft.get("order_type", "dine_in"),
                items=order_draft.get("items", []),
                delivery_address=order_draft.get("delivery_address"),
                special_notes=order_draft.get("special_notes"),
            )

            if result.get("success"):
                order_id = result["order_id"]
                total = result["total_amount"]
                order_type = order_type_label(result["order_type"])
                reply = (
                    f"🎉 下单成功！您的订单 #{order_id} 已提交。\n\n"
                    f"• 用餐方式：{order_type}\n"
                    f"• 订单金额：${total:.2f}\n"
                    f"• 订单状态：已下单（即将开始制作）\n\n"
                    f"请在取餐或送餐时以现金或银行卡付款。感谢您的光临！"
                )
                return {
                    **state,
                    "active_flow": "none",
                    "order_draft": None,
                    "order_id": order_id,
                    "last_reply": reply,
                    "messages": [AIMessage(content=reply)],
                }
            else:
                # Stock error or validation error
                err_reply = f"❌ 下单失败：{result.get('error')}"
                return {
                    **state,
                    "active_flow": "order",
                    "order_draft": None,  # Reset draft so user can adjust
                    "last_reply": err_reply,
                    "messages": [AIMessage(content=err_reply)],
                }

        elif any(
            w in user_lower
            for w in [
                "no",
                "cancel",
                "change",
                "stop",
                "abort",
                "wait",
                # Chinese: cancel / don't want it / forget it / switch items
                "取消",
                "不用了",
                "不要了",
                "算了",
                "换个",
                "改一下",
                "再想想",
            ]
        ):
            cancel_reply = "已取消本次下单。还需要其他帮助吗？"
            return {
                **state,
                "active_flow": "none",
                "order_draft": None,
                "last_reply": cancel_reply,
                "messages": [AIMessage(content=cancel_reply)],
            }

    # 3. Extract items, quantities, variants, and order type from user message
    menu_data = await get_branch_menu_data(branch_id)
    menu_summary = await format_menu_for_prompt(branch_id)

    extraction_prompt = f"""你是餐厅的订单解析器。
请依据以下真实菜单，从顾客的消息中提取他想要点的内容：
{menu_summary}

顾客消息："{user_msg}"

返回一个 JSON 对象，严格使用以下结构：
{{
  "items": [
    {{
      "menu_item_id": <int>,
      "name": "<string>",
      "quantity": <int>,
      "selected_variants": [{{"name": "<string>", "delta": <float>}}],
      "item_notes": "<string or null>"
    }}
  ],
  "order_type": "dine_in" | "takeaway" | "delivery",
  "delivery_address": "<string or null>",
  "special_notes": "<string or null>"
}}

若消息中未提及 order_type，默认填 "dine_in"。
若没有点到任何菜品，返回 {{"items": []}}。
只输出这个 JSON 对象。
"""
    raw_llm = await llm_client.generate(prompt=user_msg, system_prompt=extraction_prompt)
    extracted = extract_json_from_text(raw_llm) or {}

    # Rule-based fallback extraction if LLM JSON parse yielded empty items
    items_to_order = extracted.get("items", [])
    if not items_to_order:
        # Match against menu items in menu_data
        for cat in menu_data.get("categories", []):
            for item in cat.get("items", []):
                item_name_lower = item["name"].lower()
                if item_name_lower in user_lower or any(word in user_lower for word in item_name_lower.split() if len(word) > 4):
                    # Find quantity
                    qty_match = re.search(r"(\d+)\s*(?:x\s*)?" + re.escape(item_name_lower[:4]), user_lower)
                    qty = int(qty_match.group(1)) if qty_match else 1
                    items_to_order.append({
                        "menu_item_id": item["id"],
                        "name": item["name"],
                        "quantity": qty,
                        "selected_variants": [],
                        "item_notes": None,
                    })

    if not items_to_order:
        reply = (
            f"抱歉，我没能识别出您想点的菜品。以下是本店菜单：\n\n"
            f"{menu_summary}\n\n"
            f"请回复菜品名称与数量（例如：「玛格丽特披萨 2 份、柠檬水 1 杯，外卖送到 XX 路 123 号」）。"
        )
        return {
            **state,
            "active_flow": "order",
            "last_reply": reply,
            "messages": [AIMessage(content=reply)],
        }

    # Determine order type and address
    order_type = extracted.get("order_type", "dine_in")
    if any(
        k in user_lower
        for k in ["deliver", "delivery", "home delivery", "外卖", "配送", "送餐", "送到"]
    ):
        order_type = "delivery"
    elif any(
        k in user_lower
        for k in ["takeaway", "take out", "takeout", "pick up", "pickup", "外带", "打包", "自取", "带走"]
    ):
        order_type = "takeaway"

    delivery_address = extracted.get("delivery_address")
    if order_type == "delivery" and not delivery_address:
        # Check if address was provided in message
        addr_match = re.search(r"(?:to|at)\s+([0-9]+\s+[A-Za-z0-9\s,.-]+(?:st|ave|rd|blvd|street|avenue|road|lane|apt|suite)?)", user_msg, re.IGNORECASE)
        if addr_match:
            delivery_address = addr_match.group(1).strip()
        else:
            ask_addr_reply = (
                f"您的订单已经准备好，但外卖需要您提供配送地址。"
                f"请回复您的收货地址（例如：「送到枫叶路 456 号 2B 室」）。"
            )
            return {
                **state,
                "active_flow": "order",
                "order_draft": stamp({
                    "items": items_to_order,
                    "order_type": "delivery",
                    "awaiting_address": True,
                }),
                "last_reply": ask_addr_reply,
                "messages": [AIMessage(content=ask_addr_reply)],
            }

    # 4. Check stock availability and estimate the total. Pricing and the
    #    stock-shortfall rule come from OrderService.preview_order -- this node
    #    keeps no copy of them. Items whose id is not on this branch's menu are
    #    dropped, matching the previous behaviour.
    items_map = {
        it["id"]: it
        for cat in menu_data.get("categories", [])
        for it in cat.get("items", [])
    }
    known_items = [it for it in items_to_order if it.get("menu_item_id") in items_map]
    preview = (
        await OrderService.preview_order(branch_id, known_items)
        if known_items
        else {"items": [], "total_amount": 0.0}
    )

    stock_errors = []
    summary_lines = []
    for line in preview["items"]:
        if line["shortfall"]:
            if line["available_quantity"] == 0:
                stock_errors.append(f"• 「{line['name']}」目前已售罄。")
            else:
                stock_errors.append(
                    f"• 「{line['name']}」仅剩 {line['available_quantity']} 份"
                    f"（需要 {line['qty']} 份，缺 {line['shortfall']} 份）。"
                )
            continue
        summary_lines.append(f"  • {line['qty']} × {line['name']} — ${line['subtotal']:.2f}")
    total_est = preview["total_amount"]

    if stock_errors:
        err_msg = (
            f"⚠️ 以下菜品在本门店库存不足：\n\n"
            + "\n".join(stock_errors)
            + "\n\n请调整数量，或从菜单中选择其他菜品。"
        )
        return {
            **state,
            "active_flow": "order",
            "last_reply": err_msg,
            "messages": [AIMessage(content=err_msg)],
        }

    # 5. Present Order Summary and Ask for Explicit Confirmation
    order_type_display = order_type_label(order_type)
    summary_text = (
        f"📝 **订单确认（{state.get('branch_name', '本门店')}）：**\n\n"
        + "\n".join(summary_lines)
        + f"\n\n**用餐方式：** {order_type_display}\n"
        + (f"**配送地址：** {delivery_address}\n" if delivery_address else "")
        + f"**预计金额：** ${total_est:.2f}\n"
        + f"*(请在取餐或送餐时以现金或银行卡支付)*\n\n"
        + f"👉 **请回复「确认」提交订单，或回复「取消」进行修改。**"
    )

    return {
        **state,
        "active_flow": "order",
        "order_draft": stamp({
            "items": items_to_order,
            "order_type": order_type,
            "delivery_address": delivery_address,
            "total_estimated": total_est,
            "awaiting_confirmation": True,
        }),
        "last_reply": summary_text,
        "messages": [AIMessage(content=summary_text)],
    }
