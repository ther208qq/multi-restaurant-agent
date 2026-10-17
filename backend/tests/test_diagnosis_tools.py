"""Task41: the diagnosis tools are thin, read-only and branch-scoped.

The Services are replaced with AsyncMocks: these tests assert *how* the tools
call them (fixed branch, caller's staff and session) and what the model sees
(namespaced evidence refs, bounded results, failures as observations). No
database is touched, and the module itself must contain no SQL.
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.agent import diagnosis_tools
from backend.app.agent.diagnosis_tools import (
    OPERATIONAL_ACTIONS,
    DiagnosisToolContext,
    build_diagnosis_tools,
)
from backend.app.exceptions import ValidationError
from backend.app.models.user import StaffUser
from backend.app.service.audit import AuditService
from backend.app.service.branch_peers import BranchPeerService
from backend.app.service.business_profile import BusinessProfileService
from backend.app.service.diagnostics import DiagnosisService, DishDailyService
from backend.app.service.stats import StatsService


def _staff() -> StaffUser:
    return StaffUser(id=7, username="downtown_staff", role="staff", branch_id=1)


def _diagnosis_payload() -> dict:
    return {
        "branch_id": 1,
        "branch_name": "市中心店",
        "status": "ok",
        "notes": [],
        "current_period": {
            "start_date": date(2026, 10, 10),
            "end_date": date(2026, 10, 16),
            "order_count": 80,
            "cancelled_count": 4,
            "revenue": 4000.0,
            "average_order_value": 50.0,
            "units_sold": 160,
            "active_days": 7,
        },
        "baseline_period": {
            "start_date": date(2026, 10, 3),
            "end_date": date(2026, 10, 9),
            "order_count": 100,
            "cancelled_count": 2,
            "revenue": 5000.0,
            "average_order_value": 50.0,
            "units_sold": 200,
            "active_days": 7,
        },
        "thresholds": {},
        "findings": [
            {
                "metric": "dish_sales",
                "label": "单品销量",
                "direction": "down",
                "severity": "warning",
                "current": 10,
                "previous": 25,
                "change_pct": -60.0,
                "menu_item_id": 3,
                "evidence": [
                    {"ref": "f0.e0", "label": "「牛肉面」本期销量", "value": 10},
                    {"ref": "f0.e1", "label": "「牛肉面」上期销量", "value": 25},
                ],
            }
        ],
    }


def _summary_payload() -> dict:
    return {
        "branch_id": 1,
        "branch_name": "市中心店",
        "start_date": date(2026, 10, 10),
        "end_date": date(2026, 10, 12),
        "order_count": 30,
        "cancelled_count": 1,
        "revenue": 1500.0,
        "average_order_value": 50.0,
        "units_sold": 60,
        "top_items": [],
        "daily": [
            {"date": date(2026, 10, 10), "order_count": 12, "revenue": 600.0, "cancelled_count": 0},
            {"date": date(2026, 10, 11), "order_count": 10, "revenue": 500.0, "cancelled_count": 1},
            {"date": date(2026, 10, 12), "order_count": 8, "revenue": 400.0, "cancelled_count": 0},
        ],
    }


def _dish_payload() -> dict:
    return {
        "branch_id": 1,
        "start_date": date(2026, 10, 10),
        "end_date": date(2026, 10, 12),
        "baseline_start_date": date(2026, 10, 7),
        "baseline_end_date": date(2026, 10, 9),
        "dishes": [
            {
                "menu_item_id": 3,
                "status": "ok",
                "direction": "down",
                "change_pct": -60.0,
                "availability": "unknown",
                "current_period": {
                    "start_date": date(2026, 10, 10),
                    "end_date": date(2026, 10, 12),
                    "quantity": 10,
                    "active_days": 3,
                },
                "baseline_period": {
                    "start_date": date(2026, 10, 7),
                    "end_date": date(2026, 10, 9),
                    "quantity": 25,
                    "active_days": 3,
                },
                "daily": [
                    {"date": date(2026, 10, 10), "quantity": 2, "day_type": "business"},
                    {"date": date(2026, 10, 11), "quantity": 8, "day_type": "business"},
                    {"date": date(2026, 10, 12), "quantity": 0, "day_type": "business"},
                ],
                "shape": {"value": "single_day_up", "basis": "集中在单日"},
            }
        ],
        "notes": [],
    }


def _peer_payload() -> dict:
    return {
        "status": "ok",
        "target": {"branch_id": 1, "branch_name": "市中心店", "status": "ok"},
        "window": {
            "start_date": date(2026, 10, 10),
            "end_date": date(2026, 10, 12),
            "baseline_start_date": date(2026, 10, 7),
            "baseline_end_date": date(2026, 10, 9),
        },
        "peers": [
            {
                "branch_id": 2,
                "branch_name": "城东店",
                "status": "ok",
                "current_active_days": 3,
                "baseline_active_days": 3,
                "change_pct": -2.0,
                "verdict": "基本持平",
            }
        ],
        "comparable_count": 2,
        "incomparable_count": 0,
        "summary": "本次共判定 2 家可见门店：可比 2 家。",
        "notes": [],
    }


def _profile_payload() -> dict:
    return {
        "branch_id": 1,
        "branch_name": "市中心店",
        "start_date": date(2026, 10, 10),
        "end_date": date(2026, 10, 12),
        "timezone": "Asia/Shanghai",
        "summary": {
            "start_date": date(2026, 10, 10),
            "end_date": date(2026, 10, 12),
            "order_count": 30,
            "cancelled_count": 2,
            "revenue": 1500.0,
            "average_order_value": 50.0,
        },
        "order_types": [
            {
                "order_type": "dine_in",
                "label": "堂食",
                "order_count": 24,
                "revenue": 1200.0,
                "cancelled_count": 2,
                "share_pct": 80.0,
            },
            {
                "order_type": "takeaway",
                "label": "外带",
                "order_count": 6,
                "revenue": 300.0,
                "cancelled_count": 0,
                "share_pct": 20.0,
            },
        ],
        "weekday_distribution": [
            {
                "weekday": 6,
                "label": "周六",
                "order_count": 30,
                "revenue": 1500.0,
                "share_pct": 100.0,
                "calendar_days": 1,
                "avg_orders_per_calendar_day": 30.0,
            }
        ],
        "hour_distribution": [
            {
                "hour": 12,
                "order_count": 30,
                "revenue": 1500.0,
                "share_pct": 100.0,
            }
        ],
        "reservations": {
            "booked_count": 10,
            "cancelled_count": 2,
            "cancellation_rate_pct": 20.0,
            "arrived_count": 6,
            "pending_count": 0,
            "confirmed_count": 2,
            "due_count": 8,
            "no_show_count": 2,
            "no_show_party_size": 4,
            "no_show_rate_pct": 25.0,
            "no_show_basis": "suspected",
            "by_status": {},
            "window": {
                "start_date": date(2026, 10, 10),
                "end_date": date(2026, 10, 12),
            },
        },
        "baselines": {
            "last_week_same_weekday": {
                "label": "上周同星期（整体前移 7 天）",
                "start_date": date(2026, 10, 3),
                "end_date": date(2026, 10, 5),
                "order_count": 40,
                "cancelled_count": 1,
                "revenue": 2000.0,
                "average_order_value": 50.0,
                "status": "ok",
                "order_count_change_pct": -25.0,
                "revenue_change_pct": -25.0,
                "notes": [],
            },
            "last_year_same_period": {
                "label": "去年同期（整体前移 1 年）",
                "start_date": date(2025, 10, 10),
                "end_date": date(2025, 10, 12),
                "order_count": 0,
                "cancelled_count": 0,
                "revenue": 0.0,
                "average_order_value": 0.0,
                "status": "zero_baseline",
                "order_count_change_pct": None,
                "revenue_change_pct": None,
                "notes": ["该基线窗口没有成交订单，无法计算变化比例。"],
            },
        },
        "notes": [],
    }


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.setattr(
        DiagnosisService, "diagnose", AsyncMock(return_value=_diagnosis_payload())
    )
    monkeypatch.setattr(
        StatsService, "get_branch_summary", AsyncMock(return_value=_summary_payload())
    )
    monkeypatch.setattr(
        DishDailyService, "get_dish_daily", AsyncMock(return_value=_dish_payload())
    )
    monkeypatch.setattr(
        BranchPeerService, "compare_branch_peers", AsyncMock(return_value=_peer_payload())
    )
    monkeypatch.setattr(
        BusinessProfileService,
        "get_business_profile",
        AsyncMock(return_value=_profile_payload()),
    )
    monkeypatch.setattr(
        AuditService,
        "list_logs",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    id=11,
                    created_at=datetime(2026, 10, 11, 9, tzinfo=timezone.utc),
                    actor_type="staff",
                    actor_id=7,
                    actor_name="downtown_staff",
                    actor_role="staff",
                    branch_id=1,
                    action="menu_item.updated",
                    entity_type="menu_item",
                    entity_id=3,
                    details={"from": {"is_available": True}, "to": {"is_available": False}},
                )
            ]
        ),
    )
    context = DiagnosisToolContext(staff=_staff(), session=object(), branch_id=1)
    return {tool.name: tool for tool in build_diagnosis_tools(context)}, context


async def test_diagnosis_tool_calls_the_service_with_the_context_branch(tools):
    by_name, context = tools
    raw = await by_name["get_diagnosis"].ainvoke(
        {"start_date": "2026-10-10", "end_date": "2026-10-16"}
    )
    body = json.loads(raw)

    assert body["findings"][0]["evidence"][0]["ref"] == "f0.e0"
    assert body["branch_id"] == 1
    call = DiagnosisService.diagnose.await_args
    assert call.args == (1, date(2026, 10, 10), date(2026, 10, 16))
    assert call.kwargs["staff"].branch_id == 1
    assert context.steps[0]["tool"] == "get_diagnosis"
    assert context.steps[0]["evidence_refs"] == ["f0.e0", "f0.e1"]


async def test_daily_dish_and_peer_tools_emit_namespaced_evidence(tools):
    by_name, context = tools

    daily = json.loads(
        await by_name["get_daily_series"].ainvoke(
            {"start_date": "2026-10-10", "end_date": "2026-10-12"}
        )
    )
    dish = json.loads(
        await by_name["get_dish_daily"].ainvoke(
            {
                "menu_item_ids": [3],
                "start_date": "2026-10-10",
                "end_date": "2026-10-12",
                "include_daily": True,
            }
        )
    )
    peers = json.loads(
        await by_name["compare_peers"].ainvoke(
            {"start_date": "2026-10-10", "end_date": "2026-10-12"}
        )
    )

    assert daily["daily"][0]["ref"].startswith("daily")
    assert daily["daily"][0]["ref"].endswith(".e1")
    assert dish["dishes"][0]["daily"][0]["ref"].startswith("dish")
    assert peers["peers"][0]["ref"].startswith("peers")
    for body in (daily, dish, peers):
        assert body["meta"]["chars"] > 0
        assert body["meta"]["tokens"] > 0
        assert body["meta"]["budget_chars"] == diagnosis_tools.MAX_TOOL_OUTPUT_CHARS
        assert body["meta"]["budget_tokens"] == diagnosis_tools.MAX_TOOL_OUTPUT_TOKENS
    refs = [item["ref"] for item in context.evidence]
    assert len(refs) == len(set(refs)), "evidence refs must be unique across calls"
    assert [step["tool"] for step in context.steps] == [
        "get_daily_series",
        "get_dish_daily",
        "compare_peers",
    ]


async def test_time_range_tool_sets_the_window_the_response_will_report(tools):
    by_name, context = tools
    raw = await by_name["resolve_time_range"].ainvoke({"expression": "上周"})
    body = json.loads(raw)

    assert body["source"] == "question"
    assert context.resolved_window["start_date"] == date.fromisoformat(body["start_date"])
    assert context.resolved_window["baseline_end_date"] < date.fromisoformat(
        body["start_date"]
    )
    assert context.steps[0]["tool"] == "resolve_time_range"


async def test_time_range_anchors_relative_phrases_to_available_data():
    """A history-only dataset must not resolve "recent" to an empty 2026 window."""
    context = DiagnosisToolContext(
        staff=_staff(),
        session=object(),
        branch_id=1,
        coverage={
            "branch_id": 1,
            "branch_name": "市中心店",
            "start_date": date(2023, 1, 1),
            "end_date": date(2023, 6, 30),
            "has_data": True,
        },
    )
    by_name = {tool.name: tool for tool in build_diagnosis_tools(context)}

    default = json.loads(
        await by_name["resolve_time_range"].ainvoke({"expression": ""})
    )
    assert default["source"] == "coverage"
    assert default["start_date"] == "2023-06-24"
    assert default["end_date"] == "2023-06-30"
    assert default["partial"] is False
    assert any("2023-06-30" in note for note in default["notes"])
    assert not any("尚未过完" in note for note in default["notes"])

    last_month = json.loads(
        await by_name["resolve_time_range"].ainvoke({"expression": "上个月"})
    )
    assert last_month["source"] == "coverage"
    assert last_month["start_date"] == "2023-05-01"
    assert last_month["end_date"] == "2023-05-31"
    assert last_month["baseline_end_date"] == "2023-04-30"

    explicit = json.loads(
        await by_name["resolve_time_range"].ainvoke(
            {"expression": "2026-10-01 到 2026-10-07"}
        )
    )
    assert explicit["source"] == "question"
    assert explicit["start_date"] == "2026-10-01"
    assert any("不在数据库可用数据范围内" in note for note in explicit["notes"])


async def test_operational_changes_are_windowed_scoped_and_citable(tools):
    by_name, context = tools
    raw = await by_name["get_operational_changes"].ainvoke(
        {"start_date": "2026-10-10", "end_date": "2026-10-12"}
    )
    body = json.loads(raw)

    call = AuditService.list_logs.await_args
    assert call.kwargs["branch_id"] == 1
    assert call.kwargs["actions"] == list(OPERATIONAL_ACTIONS)
    assert call.kwargs["start"] == datetime(2026, 10, 10, tzinfo=timezone.utc)
    assert call.kwargs["end"] == datetime(2026, 10, 13, tzinfo=timezone.utc)
    assert call.kwargs["limit"] == diagnosis_tools.MAX_OPERATIONAL_CHANGES + 1
    assert call.args[0].branch_id == 1

    assert body["changes"][0]["action_label"] == "修改菜品"
    assert body["changes"][0]["ref"].startswith("ops")
    assert context.evidence[-1]["ref"] == body["changes"][0]["ref"]
    assert context.steps[-1]["tool"] == "get_operational_changes"


async def test_tool_failure_is_an_observation_not_an_exception(tools):
    by_name, context = tools
    DiagnosisService.diagnose.side_effect = ValidationError("日期区间无效。")

    raw = await by_name["get_diagnosis"].ainvoke(
        {"start_date": "2026-10-10", "end_date": "2026-10-16"}
    )
    body = json.loads(raw)

    assert body["status"] == "error"
    assert body["message"] == "日期区间无效。"
    assert body["meta"]["truncated"] is False
    assert body["meta"]["omitted"] == {}
    assert context.steps[0]["status"] == "error"
    assert "日期区间无效" in context.steps[0]["summary"]


def test_tools_accept_no_branch_id_and_contain_no_sql(tools):
    by_name, _ = tools
    for tool in by_name.values():
        assert "branch_id" not in tool.args, tool.name

    source = Path(diagnosis_tools.__file__).read_text(encoding="utf-8")
    for forbidden in ("select(", ".execute(", ".commit(", "text("):
        assert forbidden not in source


async def test_dated_tool_rejects_a_window_that_contradicts_the_resolved_one(tools):
    by_name, context = tools
    resolved = json.loads(
        await by_name["resolve_time_range"].ainvoke({"expression": "上周"})
    )

    raw = await by_name["get_diagnosis"].ainvoke(
        {"start_date": "2020-01-01", "end_date": "2020-01-07"}
    )
    body = json.loads(raw)

    assert body["status"] == "error"
    assert body["reason"] == "window_mismatch"
    assert body["requested_window"] == {
        "start_date": "2020-01-01",
        "end_date": "2020-01-07",
    }
    assert body["resolved_window"] == {
        "start_date": resolved["start_date"],
        "end_date": resolved["end_date"],
    }
    # The rejected call never reached the Service: no second window was used.
    assert DiagnosisService.diagnose.await_count == 0
    step = context.steps[-1]
    assert step["tool"] == "get_diagnosis"
    assert step["status"] == "error"
    assert step["window"] == {
        "start_date": "2020-01-01",
        "end_date": "2020-01-07",
    }
    assert step["expected_window"] == body["resolved_window"]
    assert context.resolved_window["start_date"] == date.fromisoformat(
        resolved["start_date"]
    )

    retry = json.loads(
        await by_name["get_diagnosis"].ainvoke(
            {
                "start_date": resolved["start_date"],
                "end_date": resolved["end_date"],
            }
        )
    )
    assert retry["status"] == "ok"
    assert DiagnosisService.diagnose.await_count == 1
    assert context.steps[-1]["window"] == {
        "start_date": resolved["start_date"],
        "end_date": resolved["end_date"],
    }


async def test_first_dated_tool_defines_the_window_when_no_resolver_ran(tools):
    by_name, context = tools

    daily = json.loads(
        await by_name["get_daily_series"].ainvoke(
            {"start_date": "2026-10-10", "end_date": "2026-10-12"}
        )
    )

    assert "daily" in daily
    assert context.resolved_window["source"] == "agent"
    assert context.resolved_window["start_date"] == date(2026, 10, 10)
    assert context.resolved_window["end_date"] == date(2026, 10, 12)
    assert context.steps[0]["window"] == {
        "start_date": "2026-10-10",
        "end_date": "2026-10-12",
    }

    mismatch = json.loads(
        await by_name["get_diagnosis"].ainvoke(
            {"start_date": "2026-10-10", "end_date": "2026-10-16"}
        )
    )
    assert mismatch["reason"] == "window_mismatch"
    assert mismatch["resolved_window"] == {
        "start_date": "2026-10-10",
        "end_date": "2026-10-12",
    }


async def test_explicit_request_dates_win_and_lock_the_tool_window(tools):
    by_name, context = tools
    context.requested_start = date(2026, 9, 1)
    context.requested_end = date(2026, 9, 7)

    resolved = json.loads(
        await by_name["resolve_time_range"].ainvoke({"expression": "上个月"})
    )

    assert resolved["source"] == "explicit"
    assert resolved["start_date"] == "2026-09-01"
    assert resolved["end_date"] == "2026-09-07"
    assert any("优先于问题" in note for note in resolved["notes"])

    mismatch = json.loads(
        await by_name["get_daily_series"].ainvoke(
            {"start_date": "2026-10-10", "end_date": "2026-10-16"}
        )
    )
    assert mismatch["reason"] == "window_mismatch"

    ok = json.loads(
        await by_name["get_diagnosis"].ainvoke(
            {"start_date": "2026-09-01", "end_date": "2026-09-07"}
        )
    )
    assert ok["status"] == "ok"
    assert context.steps[-1]["window"] == {
        "start_date": "2026-09-01",
        "end_date": "2026-09-07",
    }


async def test_coverage_anchored_window_is_the_one_tools_must_use(tools):
    by_name, context = tools
    context.coverage = {
        "branch_id": 1,
        "branch_name": "市中心店",
        "start_date": date(2023, 1, 1),
        "end_date": date(2023, 6, 30),
        "has_data": True,
    }

    resolved = json.loads(
        await by_name["resolve_time_range"].ainvoke({"expression": ""})
    )

    assert resolved["source"] == "coverage"
    assert resolved["start_date"] == "2023-06-24"

    mismatch = json.loads(
        await by_name["get_daily_series"].ainvoke(
            {"start_date": "2026-10-10", "end_date": "2026-10-16"}
        )
    )
    assert mismatch["reason"] == "window_mismatch"
    assert mismatch["resolved_window"] == {
        "start_date": "2023-06-24",
        "end_date": "2023-06-30",
    }

    ok = json.loads(
        await by_name["get_daily_series"].ainvoke(
            {"start_date": "2023-06-24", "end_date": "2023-06-30"}
        )
    )
    assert "daily" in ok
    assert context.steps[-1]["window"] == {
        "start_date": "2023-06-24",
        "end_date": "2023-06-30",
    }



async def test_dish_daily_defaults_to_a_summary_with_anomaly_days(tools):
    by_name, context = tools

    body = json.loads(
        await by_name["get_dish_daily"].ainvoke(
            {
                "menu_item_ids": [3],
                "start_date": "2026-10-10",
                "end_date": "2026-10-12",
            }
        )
    )

    dish = body["dishes"][0]
    assert "daily" not in dish
    assert dish["daily_summary"] == {
        "days": 3,
        "zero_sales_days": 1,
        "zero_sales_dates": ["2026-10-12"],
        "peak": {"date": "2026-10-11", "quantity": 8},
        "trough": {"date": "2026-10-12", "quantity": 0},
    }
    notable = {item["date"]: item for item in dish["notable_days"]}
    assert notable["2026-10-12"] == {
        "date": "2026-10-12",
        "quantity": 0,
        "day_type": "business",
        "reason": "当天销量为 0",
        "ref": "dish1.3.d3",
    }
    assert notable["2026-10-11"]["ref"] == "dish1.3.d2"
    assert notable["2026-10-11"]["reason"] == "窗口内最高单日销量"
    refs = [item["ref"] for item in context.evidence]
    # Only the anomaly days were returned, so only they need evidence refs.
    assert "dish1.3.d1" not in refs
    assert "dish1.3.d2" in refs and "dish1.3.d3" in refs
    assert any("include_daily=true" in note for note in body["notes"])
    assert body["meta"]["truncated"] is False


async def test_dish_daily_returns_the_full_series_on_request(tools):
    by_name, context = tools

    body = json.loads(
        await by_name["get_dish_daily"].ainvoke(
            {
                "menu_item_ids": [3],
                "start_date": "2026-10-10",
                "end_date": "2026-10-12",
                "include_daily": True,
            }
        )
    )

    dish = body["dishes"][0]
    assert [point["quantity"] for point in dish["daily"]] == [2, 8, 0]
    assert [point["ref"] for point in dish["daily"]] == [
        "dish1.3.d1",
        "dish1.3.d2",
        "dish1.3.d3",
    ]
    assert "notable_days" not in dish
    assert dish["daily_summary"]["days"] == 3
    assert len(context.evidence) == 6  # e1/e2/e3 plus three daily points
    assert body["meta"]["truncated"] is False


async def test_default_dish_summary_is_much_smaller_than_the_series(tools, monkeypatch):
    """The default summary is what makes a 62-day dish lookup affordable."""
    by_name, _ = tools
    start, end = date(2026, 7, 1), date(2026, 8, 31)
    total_days = (end - start).days + 1
    daily = [
        {
            "date": start + timedelta(days=index),
            "quantity": 0 if index % 7 == 0 else 5,
            "day_type": "business",
        }
        for index in range(total_days)
    ]
    base = _dish_payload()
    base.update(
        {
            "start_date": start,
            "end_date": end,
            "baseline_start_date": start - timedelta(days=total_days),
            "baseline_end_date": start - timedelta(days=1),
        }
    )
    base["dishes"] = [{**base["dishes"][0], "daily": daily}]
    monkeypatch.setattr(
        DishDailyService, "get_dish_daily", AsyncMock(return_value=base)
    )
    arguments = {
        "menu_item_ids": [3],
        "start_date": str(start),
        "end_date": str(end),
    }

    summary_raw = await by_name["get_dish_daily"].ainvoke(arguments)
    detail_raw = await by_name["get_dish_daily"].ainvoke(
        {**arguments, "include_daily": True}
    )

    summary_body = json.loads(summary_raw)
    detail_body = json.loads(detail_raw)
    assert "daily" not in summary_body["dishes"][0]
    assert summary_body["dishes"][0]["daily_summary"]["days"] == total_days
    detail_dish = detail_body["dishes"][0]
    assert 0 < len(detail_dish["daily"]) <= total_days
    if len(detail_dish["daily"]) < total_days:
        assert detail_dish["daily_truncated"] is True
        assert detail_body["meta"]["omitted"]["daily"] >= 1
    assert len(summary_raw) * 2 < len(detail_raw)
    assert summary_body["meta"]["chars"] == len(summary_raw)


async def test_tool_output_budget_trims_lists_and_reports_metadata(tools, monkeypatch):
    by_name, _ = tools
    start = date(2026, 1, 1)
    end = date(2026, 4, 10)
    points = [
        {
            "date": start + timedelta(days=index),
            "order_count": 10,
            "revenue": 500.0,
            "cancelled_count": 0,
        }
        for index in range((end - start).days + 1)
    ]
    summary = {
        **_summary_payload(),
        "start_date": start,
        "end_date": end,
        "daily": points,
    }
    monkeypatch.setattr(
        StatsService, "get_branch_summary", AsyncMock(return_value=summary)
    )

    body = json.loads(
        await by_name["get_daily_series"].ainvoke(
            {"start_date": str(start), "end_date": str(end)}
        )
    )

    assert len(body["daily"]) < diagnosis_tools.MAX_SERIES_POINTS
    assert body["meta"]["truncated"] is True
    assert body["meta"]["omitted"]["daily"] >= 1
    assert body["meta"]["chars"] <= diagnosis_tools.MAX_TOOL_OUTPUT_CHARS
    assert body["meta"]["chars"] > 0 and body["meta"]["tokens"] > 0
    assert any("预算" in note for note in body["notes"])
    # What remains is exact rows with stable refs, not a cut string.
    assert body["daily"][0]["order_count"] == 10
    assert body["daily"][0]["ref"] == "daily1.e1"
    assert body["daily"][-1]["ref"].startswith("daily")


async def test_business_profile_tool_emits_section_refs_and_calls_the_service(tools):
    by_name, context = tools

    body = json.loads(
        await by_name["get_business_profile"].ainvoke(
            {"start_date": "2026-10-10", "end_date": "2026-10-12"}
        )
    )

    call = BusinessProfileService.get_business_profile.await_args
    assert call.args == (1, date(2026, 10, 10), date(2026, 10, 12))
    assert call.kwargs["staff"].branch_id == 1
    assert call.kwargs["session"] is context.session
    assert body["refs"] == {
        "order_types": "profile1.t1",
        "weekday_distribution": "profile1.w1",
        "hour_distribution": "profile1.h1",
        "reservations": "profile1.r1",
        "last_week_same_weekday": "profile1.b1",
        "last_year_same_period": "profile1.b2",
    }
    # The thread registry is rebuilt from ref-bearing dicts in the tool message,
    # so the six refs must also travel as evidence records.
    assert [item["ref"] for item in body["evidence"]] == [
        "profile1.t1",
        "profile1.w1",
        "profile1.h1",
        "profile1.r1",
        "profile1.b1",
        "profile1.b2",
    ]
    assert body["evidence"][3]["value"]["no_show_count"] == 2
    assert body["evidence"][4]["value"]["status"] == "ok"
    assert [item["ref"] for item in context.evidence] == [
        "profile1.t1",
        "profile1.w1",
        "profile1.h1",
        "profile1.r1",
        "profile1.b1",
        "profile1.b2",
    ]
    assert context.steps[-1]["tool"] == "get_business_profile"
    assert context.steps[-1]["evidence_refs"] == [
        "profile1.t1",
        "profile1.w1",
        "profile1.h1",
        "profile1.r1",
        "profile1.b1",
        "profile1.b2",
    ]
    assert body["meta"]["chars"] > 0
    assert body["meta"]["truncated"] is False


async def test_business_profile_tool_rejects_a_contradicting_window(tools):
    by_name, context = tools
    resolved = json.loads(
        await by_name["resolve_time_range"].ainvoke({"expression": "上周"})
    )

    body = json.loads(
        await by_name["get_business_profile"].ainvoke(
            {"start_date": "2020-01-01", "end_date": "2020-01-07"}
        )
    )

    assert body["status"] == "error"
    assert body["reason"] == "window_mismatch"
    assert body["resolved_window"]["start_date"] == resolved["start_date"]
    assert BusinessProfileService.get_business_profile.await_count == 0
    assert context.steps[-1]["status"] == "error"


def test_every_tool_schema_is_bindable(tools):
    by_name, _ = tools
    assert set(by_name) == {
        "resolve_time_range",
        "get_diagnosis",
        "get_daily_series",
        "get_business_profile",
        "get_dish_daily",
        "compare_peers",
        "get_operational_changes",
    }
    assert set(by_name["get_dish_daily"].args) == {
        "menu_item_ids",
        "start_date",
        "end_date",
        "include_daily",
    }
    assert set(by_name["get_business_profile"].args) == {
        "start_date",
        "end_date",
    }
