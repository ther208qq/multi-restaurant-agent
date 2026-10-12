"""Tests for Task2: Branch / Menu / FAQ Repo+Service and the unified menu
availability rule."""

import pytest
from sqlalchemy import delete

from backend.app.database import transactional_session
from backend.app.exceptions import ForbiddenError, NotFoundError, ValidationError
from backend.app.models.branch import Branch
from backend.app.models.menu import MenuCategory
from backend.app.models.user import StaffUser
from backend.app.service.branch import BranchService
from backend.app.service.faq import FAQService
from backend.app.service.menu import MenuService, compute_effective_available


def _staff(branch_id):
    return StaffUser(
        username="tester",
        role="admin" if branch_id is None else "staff",
        branch_id=branch_id,
    )


@pytest.mark.parametrize(
    "is_available,stock,expected",
    [
        (True, 0, False),
        (True, 1, True),
        (True, 7, True),
        (False, 0, False),
        (False, 5, False),
    ],
)
def test_effective_available_rule(is_available, stock, expected):
    assert compute_effective_available(is_available, stock) is expected


def test_effective_available_handles_missing_stock():
    assert compute_effective_available(True, None) is False


async def test_list_active_branches_returns_tables():
    branches = await BranchService.list_active_branches()
    assert branches
    assert all(b.is_active for b in branches)
    assert all(hasattr(b, "tables") for b in branches)


async def test_list_branches_scopes_to_staff_branch():
    scoped = await BranchService.list_branches(_staff(1))
    assert [b.id for b in scoped] == [1]

    all_branches = await BranchService.list_branches(_staff(None))
    assert len(all_branches) >= 1


async def test_update_branch_missing_raises_not_found():
    with pytest.raises(NotFoundError):
        await BranchService.update_branch(999999, {"name": "x"})


async def test_create_and_update_branch_roundtrip():
    branch = await BranchService.create_branch(
        {"name": "__svc_branch", "address": "a", "phone": "0"}
    )
    try:
        assert branch.id is not None
        updated = await BranchService.update_branch(branch.id, {"name": "__svc_branch2"})
        assert updated.name == "__svc_branch2"
    finally:
        async with transactional_session() as session:
            await session.execute(delete(Branch).where(Branch.id == branch.id))


async def test_create_table_forbidden_for_other_branch():
    with pytest.raises(ForbiddenError):
        await BranchService.create_table(
            _staff(1),
            {"branch_id": 2, "label": "T", "seating_area": "indoor", "capacity": 2},
        )


async def test_update_table_missing_and_forbidden():
    with pytest.raises(NotFoundError):
        await BranchService.update_table(_staff(None), 999999, {"label": "x"})

    tables = await BranchService.list_tables(_staff(1), 1)
    assert tables
    with pytest.raises(ForbiddenError):
        await BranchService.update_table(_staff(2), tables[0].id, {"label": "x"})


async def test_get_branch_menu_shape_and_rule():
    menu = await MenuService.get_branch_menu(1)
    assert menu["branch_id"] == 1
    assert menu["categories"]

    checked = 0
    for category in menu["categories"]:
        assert set(category) == {"id", "name", "items"}
        for item in category["items"]:
            assert item["effective_available"] == compute_effective_available(
                item["is_available"], item["stock_quantity"]
            )
            checked += 1
    assert checked > 0


async def test_get_branch_menu_missing_raises():
    with pytest.raises(NotFoundError):
        await MenuService.get_branch_menu(999999)


async def test_get_branch_menu_hides_empty_categories():
    """An empty category must not reach the public API / Agent (but staff still see it)."""
    category = await MenuService.create_category(_staff(None), 1, "__svc_empty_cat")
    try:
        menu = await MenuService.get_branch_menu(1)
        assert all(c["id"] != category.id for c in menu["categories"])

        staff_categories = await MenuService.list_categories(_staff(None), 1)
        assert any(c.id == category.id for c in staff_categories)
    finally:
        async with transactional_session() as session:
            await session.execute(
                delete(MenuCategory).where(MenuCategory.id == category.id)
            )


async def test_update_stock_negative_raises_validation():
    with pytest.raises(ValidationError):
        await MenuService.update_stock(_staff(None), 1, 1, -1)


async def test_update_stock_forbidden_for_other_branch():
    with pytest.raises(ForbiddenError):
        await MenuService.update_stock(_staff(1), 1, 2, 5)


async def test_create_item_initializes_stock_and_cleans_up():
    category = await MenuService.create_category(_staff(None), 1, "__svc_test_cat")
    try:
        item, stock = await MenuService.create_item(
            _staff(None),
            category_id=category.id,
            name="__svc_test_item",
            description=None,
            base_price=3.5,
            is_available=True,
            variants=[
                {"variant_type": "size", "variant_name": "Large", "price_delta": 1.0}
            ],
        )
        assert stock.stock_quantity == 50
        assert item.category_id == category.id
    finally:
        async with transactional_session() as session:
            await session.execute(
                delete(MenuCategory).where(MenuCategory.id == category.id)
            )


async def test_faq_returns_policies_and_contact():
    info = await FAQService.get_branch_info(1)
    assert info["branch_id"] == 1
    assert "policies" in info
    assert "opening_hours" in info


async def test_faq_missing_raises():
    with pytest.raises(NotFoundError):
        await FAQService.get_branch_info(999999)
