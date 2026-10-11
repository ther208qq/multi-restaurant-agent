"""Task7 tool tests: ``create_order_atomic`` is a thin adapter over OrderService.

The tool must hold no business rules and no database access of its own: it
delegates all pricing / stock / persistence work to OrderService (verified here
by monkeypatching the Service) and only translates the result -- and domain
exceptions -- back into the legacy dict contract the order node depends on.
"""

from pathlib import Path
from types import SimpleNamespace

from backend.app.agent.tools import order_tools
from backend.app.agent.tools.order_tools import create_order_atomic
from backend.app.exceptions import ConflictError, NotFoundError
from backend.app.service.order import OrderService

TOOL_SOURCE = Path(order_tools.__file__).read_text(encoding="utf-8")


def _preview(item_id=1, name="Pizza", qty=2, unit_price=2.0, available=10, shortfall=0):
    return {
        "items": [
            {
                "menu_item_id": item_id,
                "name": name,
                "qty": qty,
                "unit_price": unit_price,
                "subtotal": round(unit_price * qty, 2),
                "available_quantity": available,
                "shortfall": shortfall,
            }
        ],
        "total_amount": round(unit_price * qty, 2),
        "has_shortfall": bool(shortfall),
    }


def _patch(monkeypatch, *, preview=None, preview_exc=None, order=None, place_exc=None):
    """Replace the OrderService entry points and record how the tool calls them."""
    calls = {}

    async def _preview_stub(branch_id, items, **kwargs):
        calls["preview"] = (branch_id, items)
        if preview_exc is not None:
            raise preview_exc
        return preview

    async def _place_stub(customer_id, branch_id, order_type, items, **kwargs):
        calls["place"] = (customer_id, branch_id, order_type, items, kwargs)
        if place_exc is not None:
            raise place_exc
        return order

    monkeypatch.setattr(OrderService, "preview_order", _preview_stub)
    monkeypatch.setattr(OrderService, "place_order", _place_stub)
    return calls


async def test_adapter_has_no_database_access():
    """The tool must not open sessions or import SQLAlchemy itself."""
    assert "AsyncSessionLocal" not in TOOL_SOURCE
    assert "sqlalchemy" not in TOOL_SOURCE


async def test_success_delegates_and_returns_legacy_shape(monkeypatch):
    order = SimpleNamespace(id=7, status="placed", total_amount=6.0, order_type="dine_in")
    calls = _patch(monkeypatch, preview=_preview(), order=order)
    result = await create_order_atomic(
        customer_id=3,
        branch_id=1,
        order_type="dine_in",
        items=[{"menu_item_id": 1, "quantity": 2, "selected_variants": [{"delta": 1.0}]}],
    )
    assert result["success"] is True
    assert result["order_id"] == 7
    assert result["status"] == "placed"
    assert result["total_amount"] == 6.0
    assert result["order_type"] == "dine_in"
    assert result["items"][0]["menu_item_name"] == "Pizza"
    assert result["items"][0]["quantity"] == 2
    assert result["items"][0]["selected_variants"] == [{"delta": 1.0}]
    assert calls["place"][0] == 3
    assert calls["place"][1] == 1


async def test_insufficient_stock_shape_without_placing(monkeypatch):
    calls = _patch(monkeypatch, preview=_preview(qty=5, available=2, shortfall=3))
    result = await create_order_atomic(
        customer_id=1,
        branch_id=1,
        order_type="dine_in",
        items=[{"menu_item_id": 1, "quantity": 5}],
    )
    assert result["success"] is False
    assert result["shortfall"] == 3
    assert result["available_quantity"] == 2
    assert result["shortfall_item"] == "Pizza"
    assert "Insufficient stock" in result["error"]
    assert "place" not in calls


async def test_out_of_stock_shape_without_placing(monkeypatch):
    calls = _patch(monkeypatch, preview=_preview(qty=1, available=0, shortfall=1))
    result = await create_order_atomic(
        customer_id=1,
        branch_id=1,
        order_type="takeaway",
        items=[{"menu_item_id": 1, "quantity": 1}],
    )
    assert result["success"] is False
    assert result["out_of_stock_item"] == "Pizza"
    assert result["available_quantity"] == 0
    assert "place" not in calls


async def test_empty_items_short_circuits(monkeypatch):
    calls = _patch(monkeypatch, preview=_preview())
    result = await create_order_atomic(
        customer_id=1, branch_id=1, order_type="dine_in", items=[]
    )
    assert result == {"success": False, "error": order_tools.EMPTY_ORDER_ERROR}
    assert calls == {}


async def test_delivery_without_address_short_circuits(monkeypatch):
    calls = _patch(monkeypatch, preview=_preview())
    result = await create_order_atomic(
        customer_id=1,
        branch_id=1,
        order_type="delivery",
        items=[{"menu_item_id": 1, "quantity": 1}],
    )
    assert result == {"success": False, "error": order_tools.MISSING_ADDRESS_ERROR}
    assert calls == {}


async def test_missing_item_domain_error_becomes_dict(monkeypatch):
    _patch(monkeypatch, preview_exc=NotFoundError("Item #9 does not exist."))
    result = await create_order_atomic(
        customer_id=1,
        branch_id=1,
        order_type="dine_in",
        items=[{"menu_item_id": 9, "quantity": 1}],
    )
    assert result == {"success": False, "error": "Item #9 does not exist."}


async def test_place_conflict_becomes_dict(monkeypatch):
    _patch(
        monkeypatch,
        preview=_preview(),
        place_exc=ConflictError("'Pizza' is currently unavailable."),
    )
    result = await create_order_atomic(
        customer_id=1,
        branch_id=1,
        order_type="dine_in",
        items=[{"menu_item_id": 1, "quantity": 1}],
    )
    assert result["success"] is False
    assert "currently unavailable" in result["error"]

