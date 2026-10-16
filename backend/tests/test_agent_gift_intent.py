"""Task B (AgentFeatures §5): the gift-intent classifier and the inventory-based
gift suggestion the approval gate is built on.

Covers the two halves of the agent's job here: reading an order note for a
freebie request (LLM-first, deterministic rules as the offline fallback, with no
false positives on delivery wording), and proposing a gift from the branch's real
surplus inventory (reusing feature 2's tool).
"""

from uuid import uuid4

from sqlalchemy import delete

from backend.app.agent.approvals import (
    GIFT_REQUEST,
    NO_GIFT,
    assess_order_note,
    build_gift_suggestion,
    classify_gift_intent,
    rule_based_gift_intent,
)
from backend.app.database import transactional_session
from backend.app.models.branch import Branch
from backend.app.models.menu import MenuCategory, MenuItem, MenuItemStock


# --------------------------------------------------------------------------- #
# Rule-based classification (the offline verdict)
# --------------------------------------------------------------------------- #
def test_rule_based_flags_explicit_freebie_cues():
    for note in (
        "送我一个甜品呗",
        "能免费送一份沙拉吗",
        "想要赠品",
        "有没有优惠券",
        "有没有券",
        "不要钱的那种",
        "赠送一份小食",
    ):
        assert rule_based_gift_intent(note) == GIFT_REQUEST, note


def test_rule_based_ignores_delivery_wording():
    """A bare "送" about delivery must not read as a freebie request."""
    for note in (
        "送餐快一点",
        "麻烦送达门口",
        "尽快送到",
        "需要外卖配送",
        "送过来就行",
        "送货上门",
    ):
        assert rule_based_gift_intent(note) == NO_GIFT, note


def test_rule_based_plain_note_is_no_gift():
    assert rule_based_gift_intent("少辣，不要香菜") == NO_GIFT
    assert rule_based_gift_intent("") == NO_GIFT
    assert rule_based_gift_intent("   ") == NO_GIFT


# --------------------------------------------------------------------------- #
# LLM-first classification
# --------------------------------------------------------------------------- #
async def test_empty_note_never_reaches_the_llm(monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise AssertionError("the LLM must not be consulted for an empty note")

    monkeypatch.setattr("backend.app.agent.approvals.llm_client.generate", _boom)
    assert await classify_gift_intent("") == NO_GIFT
    assert await classify_gift_intent("   ") == NO_GIFT


async def test_llm_verdict_wins_over_the_rules(monkeypatch):
    async def _says_yes(*_args, **_kwargs):
        return '{"gift_request": true}'

    # The rules would say NO_GIFT (delivery wording), but the LLM is authoritative.
    monkeypatch.setattr("backend.app.agent.approvals.llm_client.generate", _says_yes)
    assert await classify_gift_intent("送餐快一点") == GIFT_REQUEST

    async def _says_no(*_args, **_kwargs):
        return '{"gift_request": false}'

    # And a strong cue the LLM reads as "no" is still "no".
    monkeypatch.setattr("backend.app.agent.approvals.llm_client.generate", _says_no)
    assert await classify_gift_intent("送我一个甜品") == NO_GIFT


async def test_unreachable_llm_falls_back_to_rules(monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise TimeoutError("no endpoint")

    monkeypatch.setattr("backend.app.agent.approvals.llm_client.generate", _boom)
    assert await classify_gift_intent("送我一个甜品") == GIFT_REQUEST
    assert await classify_gift_intent("送餐快一点") == NO_GIFT


async def test_unparseable_llm_falls_back_to_rules(monkeypatch):
    async def _garbage(*_args, **_kwargs):
        return "抱歉，我不太确定。"

    monkeypatch.setattr("backend.app.agent.approvals.llm_client.generate", _garbage)
    assert await classify_gift_intent("有没有优惠券") == GIFT_REQUEST
    assert await classify_gift_intent("少辣") == NO_GIFT


# --------------------------------------------------------------------------- #
# The inventory-based suggestion
# --------------------------------------------------------------------------- #
async def _make_branch(*, with_surplus: bool = True):
    """A branch optionally holding a giftable (dessert, well-stocked) item."""
    async with transactional_session() as s:
        branch = Branch(
            name=f"__ap{uuid4().hex[:8]}",
            address="x",
            phone="x",
            opening_hours="10:00 - 23:59",
            is_active=True,
        )
        s.add(branch)
        await s.flush()
        if with_surplus:
            category = MenuCategory(branch_id=branch.id, name="甜点")
            s.add(category)
            await s.flush()
            plentiful = MenuItem(
                category_id=category.id, name=f"__蛋糕{uuid4().hex[:6]}",
                base_price=8.0, is_available=True,
            )
            scarcer = MenuItem(
                category_id=category.id, name=f"__布丁{uuid4().hex[:6]}",
                base_price=6.0, is_available=True,
            )
            s.add_all([plentiful, scarcer])
            await s.flush()
            s.add_all([
                MenuItemStock(menu_item_id=plentiful.id, branch_id=branch.id, stock_quantity=9),
                MenuItemStock(menu_item_id=scarcer.id, branch_id=branch.id, stock_quantity=6),
            ])
        else:
            category = MenuCategory(branch_id=branch.id, name="主食")
            s.add(category)
            await s.flush()
            item = MenuItem(
                category_id=category.id, name=f"__饭{uuid4().hex[:6]}",
                base_price=12.0, is_available=True,
            )
            s.add(item)
            await s.flush()
            s.add(
                MenuItemStock(menu_item_id=item.id, branch_id=branch.id, stock_quantity=50)
            )
        return branch.id


async def _cleanup(branch_id: int):
    async with transactional_session() as s:
        await s.execute(delete(Branch).where(Branch.id == branch_id))


async def test_build_gift_suggestion_recommends_the_most_abundant():
    branch_id = await _make_branch(with_surplus=True)
    try:
        suggestion = await build_gift_suggestion(branch_id)
        assert suggestion is not None
        # Most abundant first -- the recommended pick.
        assert suggestion["recommended_item"]["stock_quantity"] == 9
        assert suggestion["recommended_item"]["item_id"] == suggestion["candidates"][0]["item_id"]
        assert len(suggestion["candidates"]) == 2
        assert suggestion["recommended_item"]["name"] in suggestion["message"]
    finally:
        await _cleanup(branch_id)


async def test_build_gift_suggestion_none_without_surplus():
    branch_id = await _make_branch(with_surplus=False)
    try:
        assert await build_gift_suggestion(branch_id) is None
    finally:
        await _cleanup(branch_id)


async def test_assess_order_note_gift_request_carries_a_suggestion(monkeypatch):
    async def _unreachable(*_args, **_kwargs):
        raise TimeoutError("offline")

    monkeypatch.setattr("backend.app.agent.approvals.llm_client.generate", _unreachable)
    branch_id = await _make_branch(with_surplus=True)
    try:
        verdict = await assess_order_note(branch_id, "送我一个甜品呗")
        assert verdict["intent"] == GIFT_REQUEST
        assert verdict["suggestion"] is not None
        assert verdict["suggestion"]["candidates"]
    finally:
        await _cleanup(branch_id)


async def test_assess_order_note_plain_note_has_no_suggestion(monkeypatch):
    async def _unreachable(*_args, **_kwargs):
        raise TimeoutError("offline")

    monkeypatch.setattr("backend.app.agent.approvals.llm_client.generate", _unreachable)
    branch_id = await _make_branch(with_surplus=True)
    try:
        verdict = await assess_order_note(branch_id, "少辣，不要香菜")
        assert verdict["intent"] == NO_GIFT
        assert verdict["suggestion"] is None
    finally:
        await _cleanup(branch_id)
