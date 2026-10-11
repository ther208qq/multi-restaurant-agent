"""Tests for draft lifecycle: a half-filled flow must not hijack the thread.

Reproduces the reported trap: once an incomplete reservation draft existed, every
later message -- "What's on the menu?", "Where is my order?" -- was answered with
the same "how many guests?" prompt, across sessions, with no way out.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from backend.app.agent.flows import (
    DRAFT_TTL_MINUTES,
    is_cancel,
    is_stale,
    pending_flow,
    stamp,
)
from backend.app.agent.nodes import intent_router as router_module
from backend.app.agent.nodes import order_node as order_node_module
from backend.app.agent.nodes import reservation_node as reservation_node_module
from backend.app.agent.nodes.intent_router import intent_router_node
from backend.app.agent.nodes.order_node import order_node
from backend.app.agent.nodes.reservation_node import reservation_node


def _fresh_draft(**fields):
    return stamp({"party_size": 4, **fields})


def _draft_aged(minutes):
    """A draft whose stamp is ``minutes`` old."""
    old = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return {**stamp({"party_size": 4}), "updated_at": old.isoformat()}


def _state(message, **extra):
    return {
        "branch_id": 1,
        "customer_id": 1,
        "active_flow": "reservation",
        "messages": [HumanMessage(content=message)],
        **extra,
    }


def _no_llm(monkeypatch):
    """Fail loudly if the router consults the LLM when it should not."""

    def _boom(*args, **kwargs):
        raise AssertionError("the LLM must not be consulted here")

    monkeypatch.setattr(router_module, "llm_client", SimpleNamespace(generate=_boom))


# --------------------------------------------------------------------------- #
# flows helpers
# --------------------------------------------------------------------------- #


def test_stamp_adds_a_fresh_timestamp():
    draft = stamp({"party_size": 4})
    assert draft["party_size"] == 4
    age = datetime.now(timezone.utc) - datetime.fromisoformat(draft["updated_at"])
    assert age < timedelta(minutes=1)
    # Idempotent-ish: stamping an empty/absent draft is a no-op, not a crash.
    assert stamp(None) is None
    assert stamp({}) == {}


def test_is_stale_boundaries():
    assert is_stale(_draft_aged(0)) is False
    assert is_stale(_draft_aged(DRAFT_TTL_MINUTES - 1)) is False
    assert is_stale(_draft_aged(DRAFT_TTL_MINUTES + 1)) is True
    # Undated drafts live on in persisted checkpoints; they must age out too.
    assert is_stale({"party_size": 4}) is True
    assert is_stale({"party_size": 4, "updated_at": "not-a-date"}) is True
    # An absent draft is absent, not stale.
    assert is_stale(None) is False


def test_pending_flow_ignores_stale_and_absent_drafts():
    assert pending_flow({"reservation_draft": _fresh_draft()}) == "reservation"
    assert pending_flow({"order_draft": stamp({"items": []})}) == "order"
    assert pending_flow({"reservation_draft": _draft_aged(60)}) is None
    assert pending_flow({"reservation_draft": {"party_size": 4}}) is None
    assert pending_flow({}) is None


def test_is_cancel_matches_explicit_abandons_only():
    for text in ("cancel", "never mind", "forget it", "start over", "reset", "abort"):
        assert is_cancel(text), text
    # A bare "no"/"yes" answers a confirmation prompt; it is not an abandon.
    for text in ("no", "yes", "no thanks", "4 people tomorrow"):
        assert not is_cancel(text), text


# --------------------------------------------------------------------------- #
# router behaviour around a pending draft
# --------------------------------------------------------------------------- #


async def test_pending_draft_still_continues_a_slot_filling_answer(monkeypatch):
    _no_llm(monkeypatch)
    state = _state("4 people tomorrow at 7pm", reservation_draft=_fresh_draft())
    out = await intent_router_node(state)
    assert out["next_node"] == "reservation"


async def test_pending_draft_does_not_swallow_a_menu_question(monkeypatch):
    """The reported bug: this used to re-ask for party size, forever."""
    _no_llm(monkeypatch)
    state = _state("What's on the menu?", reservation_draft=_fresh_draft())
    out = await intent_router_node(state)
    assert out["next_node"] == "order"
    # The booking is not destroyed, just no longer in the way.
    assert out["reservation_draft"]["party_size"] == 4


async def test_pending_draft_does_not_swallow_a_status_question(monkeypatch):
    _no_llm(monkeypatch)
    state = _state("Where is my order?", reservation_draft=_fresh_draft())
    assert (await intent_router_node(state))["next_node"] == "status"


async def test_cancel_clears_the_drafts(monkeypatch):
    _no_llm(monkeypatch)
    state = _state("never mind", reservation_draft=_fresh_draft())
    out = await intent_router_node(state)
    assert out["next_node"] == "end"
    assert out["active_flow"] == "none"
    assert out["order_draft"] is None
    assert out["reservation_draft"] is None
    assert "cleared" in out["last_reply"]


async def test_stale_draft_neither_steers_nor_is_reported(monkeypatch):
    async def generate(prompt, system_prompt=None, temperature=0.1):
        return '{"intent": "general"}'

    monkeypatch.setattr(router_module, "llm_client", SimpleNamespace(generate=generate))
    state = _state("hi", reservation_draft=_draft_aged(DRAFT_TTL_MINUTES + 5))
    out = await intent_router_node(state)
    assert out["next_node"] == "end"  # not "reservation"
    assert out["reservation_draft"] is None
    assert out["active_flow"] == "none"


async def test_legacy_undated_draft_is_released(monkeypatch):
    """Drafts persisted before stamping existed must not trap anyone."""

    async def generate(prompt, system_prompt=None, temperature=0.1):
        return '{"intent": "general"}'

    monkeypatch.setattr(router_module, "llm_client", SimpleNamespace(generate=generate))
    state = _state("What's on the menu?", reservation_draft={"party_size": 4})
    assert (await intent_router_node(state))["next_node"] == "order"


# --------------------------------------------------------------------------- #
# the nodes must date what they store
# --------------------------------------------------------------------------- #


async def test_reservation_node_stamps_the_draft_it_stores(monkeypatch):
    async def generate(prompt, system_prompt=None, temperature=0.1):
        return "{}"  # nothing extractable -> "tell me the party size" draft

    monkeypatch.setattr(
        reservation_node_module, "llm_client", SimpleNamespace(generate=generate)
    )
    out = await reservation_node(
        {"branch_id": 1, "customer_id": 1, "messages": [HumanMessage(content="a table")]}
    )
    assert not is_stale(out["reservation_draft"])


async def test_order_node_stamps_the_draft_it_stores(monkeypatch):
    import json

    async def generate(prompt, system_prompt=None, temperature=0.1):
        return json.dumps(
            {
                "items": [{"menu_item_id": 1, "name": "Margherita Pizza", "quantity": 1,
                           "selected_variants": [], "item_notes": None}],
                "order_type": "delivery",
                "delivery_address": None,
            }
        )

    monkeypatch.setattr(order_node_module, "llm_client", SimpleNamespace(generate=generate))
    out = await order_node(
        {
            "branch_id": 1,
            "customer_id": 1,
            "branch_name": "B",
            "messages": [HumanMessage(content="deliver please")],
        }
    )
    assert out["order_draft"]["awaiting_address"] is True
    assert not is_stale(out["order_draft"])
