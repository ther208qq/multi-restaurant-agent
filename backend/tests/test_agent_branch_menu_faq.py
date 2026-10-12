"""Task4 tests: Agent Branch / Menu / FAQ tools are thin adapters over Services.

The tools must return the same payload shapes as before (so the Agent nodes are
unaffected) and convert Service domain exceptions into the ``{"error": ...}``
payload the nodes already handle.
"""

from langchain_core.messages import HumanMessage

from backend.app.agent.nodes.branch_selector import branch_selector_node
from backend.app.agent.tools import menu_tools
from backend.app.agent.tools.faq_tools import get_branch_faq
from backend.app.agent.tools.menu_tools import (
    format_menu_for_prompt,
    get_branch_menu_data,
)


async def test_menu_tool_returns_service_payload():
    data = await get_branch_menu_data(1)
    assert data["branch_id"] == 1
    assert data["categories"]
    item = data["categories"][0]["items"][0]
    assert set(item) >= {"id", "effective_available", "variants"}


async def test_menu_tool_missing_branch_returns_error():
    data = await get_branch_menu_data(999999)
    assert "error" in data
    assert "categories" not in data


async def test_format_menu_for_prompt_renders_text():
    text = await format_menu_for_prompt(1)
    assert text.startswith("--- ")
    assert "菜单" in text
    assert "#" in text  # item ids rendered


async def test_format_menu_for_prompt_missing_branch_returns_message():
    text = await format_menu_for_prompt(999999)
    assert "不存在" in text


async def test_format_menu_for_prompt_skips_empty_categories(monkeypatch):
    """An empty category must not render as a bare header.

    A leftover test category in the database used to show up in the agent's menu
    as a stray "[__route_test_cat]" line.
    """
    payload = {
        "branch_name": "B",
        "categories": [
            {"name": "__route_test_cat", "items": []},
            {
                "name": "Pizzas",
                "items": [
                    {
                        "id": 1,
                        "name": "Margherita Pizza",
                        "effective_available": True,
                        "variants": [],
                        "stock_quantity": 3,
                        "base_price": 12.0,
                        "description": "",
                    }
                ],
            },
        ],
    }

    async def fake_menu(branch_id):
        return payload

    monkeypatch.setattr(menu_tools, "get_branch_menu_data", fake_menu)
    text = await format_menu_for_prompt(1)

    assert "__route_test_cat" not in text
    assert "[Pizzas]" in text
    assert "#1 Margherita Pizza" in text


async def test_faq_tool_returns_policies():
    data = await get_branch_faq(1)
    assert data["branch_id"] == 1
    assert data["policies"]


async def test_faq_tool_missing_branch_returns_error():
    data = await get_branch_faq(999999)
    assert "error" in data


# --------------------------------------------------------------------------- #
# branch_selector node (now backed by BranchService)
# --------------------------------------------------------------------------- #


async def test_branch_selector_short_circuits_when_branch_set():
    state = {"branch_id": 1, "messages": []}
    assert await branch_selector_node(state) is state


async def test_branch_selector_resolves_by_name():
    state = {"messages": [HumanMessage(content="我想去市中心小馆")]}
    out = await branch_selector_node(state)
    assert out["branch_id"] == 1
    assert out["branch_name"]


async def test_branch_selector_prompts_when_unresolved():
    state = {"messages": [HumanMessage(content="hello there")]}
    out = await branch_selector_node(state)
    assert out["active_flow"] == "branch_select"
    assert "门店" in out["last_reply"]
