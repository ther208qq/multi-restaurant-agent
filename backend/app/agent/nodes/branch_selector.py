"""Branch Selector LangGraph Node."""

from typing import Any, Dict
from langchain_core.messages import AIMessage

from backend.app.service.branch import BranchService


async def branch_selector_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure conversation has a selected branch; prompt or resolve branch from user input."""
    # If branch is already set in state, continue
    if state.get("branch_id"):
        return state

    messages = state.get("messages", [])
    last_user_msg = messages[-1].content.strip().lower() if messages else ""

    # Fetch active branches via the shared BranchService
    branches = await BranchService.list_active_branches()

    if not branches:
        reply = "抱歉，目前没有营业中的门店，请稍后再试。"
        return {
            **state,
            "last_reply": reply,
            "messages": [AIMessage(content=reply)],
        }

    # Check if user mentioned branch name or number
    selected_branch = None
    for idx, b in enumerate(branches, start=1):
        if (
            str(b.id) in last_user_msg
            or str(idx) in last_user_msg.split()
            or b.name.lower() in last_user_msg
            or any(part in last_user_msg for part in b.name.lower().split() if len(part) > 3)
        ):
            selected_branch = b
            break

    if selected_branch:
        reply = f"好的！已为您选择门店：{selected_branch.name}（{selected_branch.address}）。请问需要什么帮助？您可以浏览菜单、点餐、预订座位，或查询订单状态。"
        return {
            **state,
            "branch_id": selected_branch.id,
            "branch_name": selected_branch.name,
            "active_flow": "none",
            "last_reply": reply,
            "messages": [AIMessage(content=reply)],
        }

    # If not resolved, prompt user with branch options
    branch_list_str = "\n".join([f"• {idx}) {b.name} — {b.address}" for idx, b in enumerate(branches, start=1)])
    prompt_reply = (
        f"欢迎光临！开始之前，请选择您想要咨询的门店：\n\n"
        f"{branch_list_str}\n\n请回复门店名称或编号。"
    )
    return {
        **state,
        "active_flow": "branch_select",
        "last_reply": prompt_reply,
        "messages": [AIMessage(content=prompt_reply)],
    }
