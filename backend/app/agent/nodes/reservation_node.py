"""Reservation Flow LangGraph Node: collect the slot, then book it as pending."""

from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any, Dict
from langchain_core.messages import AIMessage

from backend.app.agent.flows import stamp
from backend.app.agent.labels import RESERVATION_STATUS_LABELS, seating_area_label
from backend.app.agent.llm import extract_json_from_text, llm_client
from backend.app.agent.tools.table_tools import check_table_availability_and_book


async def reservation_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Handle table booking: check real-time availability and book the table."""
    branch_id = state.get("branch_id")
    customer_id = state.get("customer_id")
    messages = state.get("messages", [])
    user_msg = messages[-1].content.strip() if messages else ""
    user_lower = user_msg.lower()

    # 1. Parse reservation parameters from message
    # The extractor resolves relative dates ("tomorrow", "tonight") against the
    # reference date below, so it must be *today*: a hardcoded reference silently
    # books every relative-date reservation in the past.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sys_prompt = f"""你是餐厅的预订信息提取器。
当前参考日期：{today}。
请从顾客的消息中提取预订信息：
"{user_msg}"

输出一个 JSON 对象，严格使用以下结构：
{{
  "party_size": <int or null>,
  "date": "<YYYY-MM-DD or null>",
  "time": "<24 小时制的 HH:MM，或 null>",
  "seating_area": "indoor" | "outdoor" | "rooftop" | "any",
  "notes": "<string or null>"
}}

只输出合法的 JSON。
"""
    raw_llm = await llm_client.generate(prompt=user_msg, system_prompt=sys_prompt)
    extracted = extract_json_from_text(raw_llm) or {}

    # Merge with existing draft if present
    draft = state.get("reservation_draft") or {}
    party_size = extracted.get("party_size") or draft.get("party_size")
    date_str = extracted.get("date") or draft.get("date")
    time_str = extracted.get("time") or draft.get("time")
    seating_area = extracted.get("seating_area") or draft.get("seating_area") or "indoor"
    notes = extracted.get("notes") or draft.get("notes")

    # Regex heuristic fallbacks for common phrases
    if not party_size:
        size_match = re.search(r"(?:table\s+for|party\s+of|for)\s+(\d+)", user_lower)
        if not size_match:
            # Chinese: "4 人桌" / "订 4 位" / "一共 4 个"
            size_match = re.search(r"([0-9]+) *[人位名]", user_lower)
        if size_match:
            party_size = int(size_match.group(1))

    if not time_str:
        time_match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", user_lower)
        if time_match:
            hr = int(time_match.group(1))
            mn = int(time_match.group(2) or 0)
            ampm = time_match.group(3).lower()
            if ampm == "pm" and hr < 12:
                hr += 12
            elif ampm == "am" and hr == 12:
                hr = 0
            time_str = f"{hr:02d}:{mn:02d}"

    if not time_str:
        # Chinese times the am/pm regex cannot see: "晚上 7 点", "7 点半", "19:30".
        cn_time = re.search(
            r"(上午|中午|下午|晚上|傍晚|今晚|凌晨)? *([0-9]{1,2}) *"
            r"(?::([0-9]{2})|点 *(?:(半)|([0-9]{1,2}) *分?)?)",
            user_lower,
        )
        if cn_time:
            period = cn_time.group(1) or ""
            hour = int(cn_time.group(2))
            minute = 30 if cn_time.group(4) else int(cn_time.group(3) or cn_time.group(5) or 0)
            if period in ("下午", "晚上", "傍晚", "今晚") and hour < 12:
                hour += 12
            elif period == "中午" and hour < 11:
                hour += 12
            elif period == "凌晨" and hour == 12:
                hour = 0
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                time_str = f"{hour:02d}:{minute:02d}"

    if not date_str:
        if any(k in user_msg for k in ("today", "tonight", "今天", "今晚")):
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        elif "后天" in user_msg:
            date_str = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
        elif any(k in user_msg for k in ("tomorrow", "明天")):
            date_str = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

    # Check for seating area preference keywords. Rooftop is tested first so a
    # Chinese "天台花园" is not read as the outdoor "花园".
    if any(k in user_msg for k in ("rooftop", "terrace", "roof", "天台", "屋顶", "楼顶")):
        seating_area = "rooftop"
    elif any(k in user_msg for k in ("outdoor", "patio", "garden", "户外", "露台", "花园", "室外")):
        seating_area = "outdoor"
    elif any(k in user_msg for k in ("indoor", "室内")):
        seating_area = "indoor"

    # 2. Check if we need more information from the customer
    missing_fields = []
    if not party_size:
        missing_fields.append("就餐人数")
    if not date_str:
        missing_fields.append("日期（例如：今天、明天，或 2026-10-15）")
    if not time_str:
        missing_fields.append("时间（例如：晚上 7 点）")

    if missing_fields:
        prompt_reply = (
            f"很乐意为您预订座位！为了查询可订情况，请告诉我"
            f"{'、'.join(missing_fields)}。另外，也请告知您偏好室内、户外露台还是天台花园。"
        )
        return {
            **state,
            "active_flow": "reservation",
            "reservation_draft": stamp({
                "party_size": party_size,
                "date": date_str,
                "time": time_str,
                "seating_area": seating_area,
                "notes": notes,
            }),
            "last_reply": prompt_reply,
            "messages": [AIMessage(content=prompt_reply)],
        }

    # 3. All info collected: parse datetime, check availability and book as pending
    try:
        start_datetime = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        # Fallback to evening time if malformed
        start_datetime = datetime.now(timezone.utc).replace(hour=19, minute=0, second=0, microsecond=0)

    # Call availability and booking tool (guarded by PostgreSQL exclusion constraint)
    res_result = await check_table_availability_and_book(
        customer_id=customer_id,
        branch_id=branch_id,
        start_time=start_datetime,
        party_size=party_size,
        duration_minutes=90,
        seating_area=seating_area,
        notes=notes,
    )

    if res_result.get("success"):
        res_id = res_result["reservation_id"]
        table_label = res_result["table_label"]
        area = seating_area_label(res_result["seating_area"])
        dt_display = res_result["date"]
        tm_display = res_result["time"]
        branch_name = res_result["branch_name"]
        status = res_result["status"]
        status_label = RESERVATION_STATUS_LABELS.get(status, status)

        reply = (
            f"⏳ **预订已提交，等待门店确认**\n\n"
            f"• 预订编号：#{res_id}\n"
            f"• 门店：{branch_name}\n"
            f"• 餐桌：{table_label}（{area}）\n"
            f"• 时间：{dt_display} {tm_display}（90 分钟）\n"
            f"• 人数：{party_size} 位\n"
            f"• 状态：{status_label}\n\n"
            f"门店确认后即可到店；您也可以在「我的记录」中查看本次预订。"
        )
        return {
            **state,
            "active_flow": "none",
            "reservation_draft": None,
            "reservation_id": res_id,
            "last_reply": reply,
            "messages": [AIMessage(content=reply)],
        }
    else:
        # Table unavailable or race-condition conflict caught gracefully
        error_msg = res_result.get("error", "当前没有可用的餐桌。")
        reply = f"❌ {error_msg}"
        return {
            **state,
            "active_flow": "reservation",
            "reservation_draft": stamp({
                "party_size": party_size,
                "date": date_str,
                "time": None,  # Reset time so user can pick another slot
                "seating_area": seating_area,
                "notes": notes,
            }),
            "last_reply": reply,
            "messages": [AIMessage(content=reply)],
        }
