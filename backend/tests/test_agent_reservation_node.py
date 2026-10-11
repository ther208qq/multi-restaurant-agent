"""Regression tests for the reservation node's date handling.

The extractor prompt used to hardcode a development-era reference date
(``2026-09-16``), so every relative date ("tomorrow at 7pm") resolved to a day in
the past while still replying "Reservation Confirmed". These tests pin the
reference date to *today* and check the resolved booking instant.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from backend.app.agent.nodes import reservation_node as node_module
from backend.app.agent.nodes.reservation_node import reservation_node


def _extractor(payload):
    """Stand-in for ``llm_client`` whose only job is to return fixed JSON."""

    async def generate(prompt, system_prompt=None, temperature=0.1):
        return json.dumps(payload)

    return SimpleNamespace(generate=generate)


def _state(text):
    return {"branch_id": 1, "customer_id": 1, "messages": [HumanMessage(content=text)]}


async def test_extraction_prompt_carries_todays_date(monkeypatch):
    """The reference date handed to the extractor must be today, not a constant."""
    captured = {}

    async def generate(prompt, system_prompt=None, temperature=0.1):
        captured["system_prompt"] = system_prompt
        return "{}"

    monkeypatch.setattr(node_module, "llm_client", SimpleNamespace(generate=generate))
    await reservation_node(_state("I'd like a table"))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert f"Current date reference: {today}." in captured["system_prompt"]
    # The stale development constant must never come back.
    assert "2026-09-16" not in captured["system_prompt"]


async def test_tomorrow_resolves_to_the_actual_next_day(monkeypatch):
    """'tomorrow at 7pm' must reach the booking tool on tomorrow's date."""
    booked = {}

    async def fake_book(**kwargs):
        booked.update(kwargs)
        return {
            "success": True,
            "reservation_id": 1,
            "table_label": "T1 (Indoor)",
            "seating_area": "indoor",
            "date": "ignored",
            "time": "07:00 PM",
            "branch_name": "Downtown Bistro",
        }

    # No date from the extractor, so the "tomorrow" regex fallback has to resolve it.
    monkeypatch.setattr(
        node_module,
        "llm_client",
        _extractor({"party_size": 4, "date": None, "time": "19:00", "seating_area": "indoor"}),
    )
    monkeypatch.setattr(node_module, "check_table_availability_and_book", fake_book)

    out = await reservation_node(_state("book a table for 4 tomorrow at 7pm"))

    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    assert booked["start_time"].strftime("%Y-%m-%d") == tomorrow.strftime("%Y-%m-%d")
    assert booked["start_time"].hour == 19
    assert booked["party_size"] == 4
    assert out["reservation_id"] == 1


async def test_today_resolves_to_today(monkeypatch):
    """`tonight` must not drift a day either."""
    booked = {}

    async def fake_book(**kwargs):
        booked.update(kwargs)
        return {
            "success": True,
            "reservation_id": 2,
            "table_label": "R1 (Roof)",
            "seating_area": "rooftop",
            "date": "ignored",
            "time": "08:00 PM",
            "branch_name": "Downtown Bistro",
        }

    monkeypatch.setattr(
        node_module,
        "llm_client",
        _extractor({"party_size": 2, "date": None, "time": "20:00", "seating_area": "rooftop"}),
    )
    monkeypatch.setattr(node_module, "check_table_availability_and_book", fake_book)

    await reservation_node(_state("table for 2 tonight at 8pm on the rooftop"))

    assert booked["start_time"].strftime("%Y-%m-%d") == datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert booked["seating_area"] == "rooftop"
