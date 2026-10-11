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
