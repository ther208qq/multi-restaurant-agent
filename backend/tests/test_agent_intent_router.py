"""Regression tests for intent routing keywords.

The keyword fast path used to test bare substrings in the order
order -> reservation -> status -> faq, so "Where is my order?" matched "order"
first and was answered with a menu dump ("I didn't quite catch which items you'd
like to order"). These tests pin both the precedence and the word-boundary
matching, and confirm the LLM is only consulted when no keyword matches.
"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from backend.app.agent.nodes import intent_router as node_module
from backend.app.agent.nodes.intent_router import intent_router_node, keyword_route


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # Asking about an existing order/booking must not be read as placing one.
        ("Where is my order?", "status"),
        ("where's my order", "status"),
        ("What is the status of my reservation?", "status"),
        ("Track my order please", "status"),
        ("Can you check my booking?", "status"),
        ("Did my order go through?", "status"),
        ("Where is my table?", "status"),
        # Placing an order / browsing the menu.
        ("I'll take 2 Margherita Pizzas and 1 Artisan Lemonade", "order"),
        ("What's on the menu?", "order"),
        ("I would like to order food", "order"),
        ("Do you have tiramisu?", "order"),
        # Booking a table.
        ("I'd like to book a table for 4 people tomorrow at 7pm", "reservation"),
        ("can I reserve a spot", "reservation"),
        ("Do you have outdoor seating?", "reservation"),
        # Branch FAQ.
        ("What are your opening hours?", "faq"),
        ("Where are you located?", "faq"),
        ("What is your phone number?", "faq"),
        ("Is there parking?", "faq"),
    ],
)
def test_keyword_routes(message, expected):
    assert keyword_route(message.lower()) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # Asking about an existing order/booking.
        ("查一下我最近订单的状态", "status"),
        ("我的订单到哪了", "status"),
        ("我的预订确认了吗", "status"),
        ("订单号是多少", "status"),
        # Placing an order / browsing the menu (the frontend's quick chips).
        ("菜单上有什么？", "order"),
        ("点 2 份玛格丽特披萨", "order"),
        ("我想点一份提拉米苏", "order"),
        # Booking a table — note "7 点" (o'clock) must NOT be read as ordering.
        ("今晚 7 点订一张 4 人桌", "reservation"),
        ("帮我订个座位", "reservation"),
        ("可以预订桌子吗", "reservation"),
        # Branch FAQ.
        ("你们在哪里？营业时间是？", "faq"),
        ("你们几点开门", "faq"),
        ("停车方便吗", "faq"),
    ],
)
def test_chinese_keyword_routes(message, expected):
    assert keyword_route(message) == expected


def test_chinese_greeting_has_no_keyword_route():
    """A plain greeting must fall through to the LLM, not a keyword route."""
    assert keyword_route("你好") is None


def test_chinese_oclock_is_not_an_order_keyword():
    """Bare "点" means o'clock as well as order; only 点单/点餐/点菜/点个 count."""
    assert keyword_route("现在几点了") is None
    assert keyword_route("晚上 7 点见") is None
    assert keyword_route("点单") == "order"


def test_where_is_my_order_is_not_an_order_keyword_hit():
    """The exact regression: 'order' alone must not shadow the status phrase."""
    assert keyword_route("where is my order?") == "status"
    # ...while a genuine order still routes to the order node.
    assert keyword_route("i want to order a pizza") == "order"


def test_bare_words_do_not_leak_through_substrings():
    """Word boundaries: these contain no route keyword once boundaries apply."""
    assert keyword_route("hi there") is None
    assert keyword_route("my border collie") is None
    assert keyword_route("reordering is hard") == "order"  # 'reorder' is an order


async def test_node_returns_keyword_route_without_calling_the_llm(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("the LLM must not be consulted for keyword hits")

    monkeypatch.setattr(node_module, "llm_client", SimpleNamespace(generate=_boom))
    state = {
        "branch_id": 1,
        "active_flow": "none",
        "messages": [HumanMessage(content="Where is my order?")],
    }
    assert (await intent_router_node(state))["next_node"] == "status"


async def test_node_falls_back_to_llm_for_unknown_text(monkeypatch):
    captured = {}

    async def generate(prompt, system_prompt=None, temperature=0.1):
        captured["prompt"] = prompt
        return '{"intent": "faq"}'

    monkeypatch.setattr(node_module, "llm_client", SimpleNamespace(generate=generate))
    state = {
        "branch_id": 1,
        "active_flow": "none",
        "messages": [HumanMessage(content="is the chef any good these days")],
    }
    out = await intent_router_node(state)
    assert out["next_node"] == "faq"
    assert captured["prompt"] == "is the chef any good these days"
