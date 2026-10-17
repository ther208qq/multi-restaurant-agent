"""Task13 tests: no residual direct DB access or duplicated business rules.

Covers the leftovers the per-module migrations did not reach: the staff menu
route's inline availability rule (now the centralized one). The Agent chat node
assertions that used to live here were removed together with the unwired legacy
LangGraph chat graph; order pricing and stock behavior are covered directly by
the OrderService tests.
"""

from pathlib import Path

from backend.app.routes import staff_menu as staff_menu_module
from backend.app.service.branch import BranchService


def _src(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


def test_staff_menu_route_uses_centralized_availability_rule():
    src = _src(staff_menu_module)
    assert "is_available and" not in src
    assert "item_payload" in src


async def test_branch_service_get_branch_found_and_missing():
    branch = await BranchService.get_branch(1)
    assert branch is not None
    assert branch.id == 1
    assert await BranchService.get_branch(999999) is None
