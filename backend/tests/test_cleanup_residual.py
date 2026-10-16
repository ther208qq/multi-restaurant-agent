"""Task13 tests: no residual direct DB access or duplicated business rules.

Covers the leftovers the per-module migrations did not reach: the staff menu
route's inline availability rule (now the centralized one) and the Agent order
node's own price/stock math (now OrderService.preview_order).
"""

import json
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import HumanMessage
from sqlalchemy import delete, select

from backend.app.agent.nodes import order_node as order_node_module
from backend.app.agent.nodes.order_node import order_node
from backend.app.database import transactional_session
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock
from backend.app.routes import staff_menu as staff_menu_module
from backend.app.service.branch import BranchService


def _src(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


# ``test_chat_route_has_no_direct_db_access`` used to live here, guarding the
# chat route's branch lookup against a direct DB read. ``routes/chat.py`` has
# since been deleted, so the test had nothing left to inspect while its import
# kept the whole module from collecting -- dropping it, not the assertions it
# made about code that still exists below.


def test_staff_menu_route_uses_centralized_availability_rule():
    src = _src(staff_menu_module)
    assert "is_available and" not in src
    assert "item_payload" in src


def test_order_node_has_no_local_price_math():
    src = _src(order_node_module)
    assert "OrderService.preview_order" in src
    assert '["base_price"]' not in src
    assert "price_delta" not in src


async def test_branch_service_get_branch_found_and_missing():
    branch = await BranchService.get_branch(1)
    assert branch is not None
    assert branch.id == 1
    assert await BranchService.get_branch(999999) is None


async def _make_item(stock: int, base_price: float = 10.0):
    async with transactional_session() as s:
        cat = MenuCategory(branch_id=1, name='__cl_' + uuid4().hex[:8])
        s.add(cat)
        await s.flush()
        item = MenuItem(
            category_id=cat.id,
            name='__cl_item',
            base_price=base_price,
            is_available=True,
        )
        s.add(item)
        await s.flush()
        s.add(MenuItemStock(menu_item_id=item.id, branch_id=1, stock_quantity=stock))
        await s.flush()
        return item.id, cat.id


async def _cleanup(cat_id):
    async with transactional_session() as s:
        rows = await s.execute(select(MenuItem.id).where(MenuItem.category_id == cat_id))
        item_ids = [row[0] for row in rows.all()]
        if item_ids:
            await s.execute(
                delete(MenuItemStock).where(MenuItemStock.menu_item_id.in_(item_ids))
            )
            await s.execute(delete(MenuItem).where(MenuItem.id.in_(item_ids)))
        await s.execute(delete(MenuCategory).where(MenuCategory.id == cat_id))


def _stub_generate(monkeypatch, payload):
    async def fake_generate(prompt=None, system_prompt=None, temperature=0.1):
        return json.dumps(payload)

    monkeypatch.setattr(order_node_module.llm_client, 'generate', fake_generate)


def _order_state():
    return {
        'branch_id': 1,
        'customer_id': 1,
        'branch_name': 'B',
        'messages': [HumanMessage(content='I would like to order please')],
    }


async def test_order_node_estimates_total_via_service(monkeypatch):
    item_id, cat_id = await _make_item(stock=5, base_price=10.0)
    try:
        _stub_generate(monkeypatch, {'items': [{'menu_item_id': item_id, 'name': 'x', 'quantity': 2, 'selected_variants': [], 'item_notes': None}], 'order_type': 'dine_in'})
        result = await order_node(_order_state())
        draft = result['order_draft']
        assert draft['awaiting_confirmation'] is True
        assert draft['total_estimated'] == 20.0
        assert '预计金额' in result['last_reply']
    finally:
        await _cleanup(cat_id)


async def test_order_node_reports_shortfall(monkeypatch):
    item_id, cat_id = await _make_item(stock=1, base_price=10.0)
    try:
        _stub_generate(monkeypatch, {'items': [{'menu_item_id': item_id, 'name': 'x', 'quantity': 3, 'selected_variants': [], 'item_notes': None}], 'order_type': 'dine_in'})
        result = await order_node(_order_state())
        assert '库存不足' in result['last_reply']
        assert '仅剩 1 份' in result['last_reply']
        assert result.get('order_draft') is None
    finally:
        await _cleanup(cat_id)
