"""FAQ Node for Branch Details and Policies."""

from typing import Any, Dict
from langchain_core.messages import AIMessage

from backend.app.agent.tools.faq_tools import get_branch_faq


async def faq_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Retrieve hardcoded/seeded branch hours, address, phone, and policies."""
    branch_id = state.get("branch_id")
    faq_data = await get_branch_faq(branch_id)

    if "error" in faq_data:
        reply = faq_data["error"]
    else:
        policies = faq_data.get("policies", {})
        reply = (
            f"ℹ️ **{faq_data['branch_name']} 门店信息：**\n\n"
            f"• 📍 **地址：** {faq_data['address']}\n"
            f"• 📞 **电话：** {faq_data['phone']}\n"
            f"• ⏰ **营业时间：** {faq_data['opening_hours']}\n"
            f"• 🚗 **停车：** {policies.get('parking', '提供停车位')}\n"
            f"• 👔 **着装要求：** {policies.get('dress_code', '无特别要求（休闲着装）')}\n"
            f"• 💳 **支付方式：** {policies.get('payment_methods', '柜台或送餐时以现金/银行卡支付')}\n"
            f"• 🛵 **配送范围：** {policies.get('delivery_radius', '门店周边常规范围')}\n\n"
            f"需要看看菜单，或者预订座位吗？"
        )

    return {
        **state,
        "active_flow": "none",
        "last_reply": reply,
        "messages": [AIMessage(content=reply)],
    }
