"""Unit tests for the reusable diagnosis helpers extracted in Task20.

These are pure functions: no database and no model. The golden-payload check
that pins the surrounding behaviour lives with the diagnostics integration tests.
"""

from datetime import date, timedelta

import pytest

from backend.app.agent.diagnosis import rule_causes, rule_checks
from backend.app.exceptions import ValidationError
from backend.app.service.diagnostics import (
    TREND_DIFFUSE,
    TREND_GRADUAL,
    TREND_GRADUAL_UP,
    TREND_NONE,
    TREND_SINGLE_DAY,
    TREND_SINGLE_DAY_UP,
    active_days,
    classify_shape,
    daily_series,
    resolve_window,
)


def _summary(start: date, end: date, daily):
    return {"start_date": start, "end_date": end, "daily": daily}


def test_resolve_window_derives_the_equal_length_previous_window():
    start, end, baseline_start, baseline_end = resolve_window(
        date(2026, 10, 10), date(2026, 10, 16)
    )

    assert (start, end) == (date(2026, 10, 10), date(2026, 10, 16))
    assert (baseline_start, baseline_end) == (date(2026, 10, 3), date(2026, 10, 9))


def test_resolve_window_rejects_reversed_and_overlong_windows():
    with pytest.raises(ValidationError):
        resolve_window(date(2026, 10, 10), date(2026, 10, 9))

    with pytest.raises(ValidationError):
        resolve_window(date(2025, 1, 1), date(2026, 10, 16))


def test_active_days_counts_only_days_that_sold_something():
    summary = _summary(
        date(2026, 10, 10),
        date(2026, 10, 13),
        [
            {"date": date(2026, 10, 10), "order_count": 1, "cancelled_count": 0},
            {"date": date(2026, 10, 11), "order_count": 0, "cancelled_count": 2},
            {"date": date(2026, 10, 12), "order_count": 3, "cancelled_count": 0},
            {"date": date(2026, 10, 13), "order_count": 0, "cancelled_count": 0},
        ],
    )

    assert active_days(summary) == 2


def test_daily_series_aligns_the_calendar_and_distinguishes_undefined_ratios():
    summary = _summary(
        date(2026, 10, 10),
        date(2026, 10, 12),
        [
            {
                "date": date(2026, 10, 10),
                "order_count": 2,
                "revenue": 20.0,
                "cancelled_count": 0,
            },
            # 10-11 has no row at all: no orders were placed that day.
            {
                "date": date(2026, 10, 12),
                "order_count": 0,
                "revenue": 0.0,
                "cancelled_count": 1,
            },
        ],
    )

    # A totals metric on a day with no rows is a genuine zero.
    assert daily_series(summary, "order_count") == [2.0, 0.0, 0.0]
    # A ratio has no denominator on a day with no sales.
    assert daily_series(summary, "average_order_value") == [10.0, None, None]
    assert daily_series(summary, "cancelled_rate") == [0.0, None, 100.0]


def test_classify_shape_single_bad_day():
    assert (
        classify_shape([0.0, 10.0, 10.0], [10.0, 10.0, 10.0])
        == TREND_SINGLE_DAY
    )


def test_classify_shape_sustained_decline():
    assert (
        classify_shape([5.0, 5.0, 5.0], [10.0, 10.0, 10.0])
        == TREND_GRADUAL
    )


def test_classify_shape_diffuse_deviation():
    # One shortfall large enough to matter is spread over a minority of days,
    # so it is neither a single collapse nor a sustained every-day decline.
    assert (
        classify_shape(
            [0.0, 0.0, 0.0, 10.0, 10.0, 10.0, 10.0],
            [10.0] * 7,
        )
        == TREND_DIFFUSE
    )


def test_classify_shape_reports_none_without_a_deviation():
    assert classify_shape([10.0, 10.0], [10.0, 10.0]) == TREND_NONE
    assert classify_shape([20.0, 20.0], [10.0, 10.0]) == TREND_NONE


def test_classify_shape_direction_up_uses_the_same_arithmetic():
    # Task21 maps the up direction onto the up-facing labels.
    assert (
        classify_shape([20.0, 10.0, 10.0], [10.0, 10.0, 10.0], direction="up")
        == TREND_SINGLE_DAY_UP
    )
    assert (
        classify_shape([15.0, 15.0, 15.0], [10.0, 10.0, 10.0], direction="up")
        == TREND_GRADUAL_UP
    )
    assert (
        classify_shape([0.0, 0.0], [10.0, 10.0], direction="up") == TREND_NONE
    )


def test_classify_shape_rejects_an_unknown_direction():
    with pytest.raises(ValueError):
        classify_shape([1.0], [1.0], direction="sideways")


def _dish_finding(direction: str):
    return {
        "metric": "dish_sales",
        "label": "菜品销量",
        "direction": direction,
        "severity": "warning",
        "current": 12.0,
        "previous": 4.0,
        "change_pct": 200.0,
        "evidence": [
            {"label": "「A」本期销量", "value": 12, "ref": "f0.e0"},
            {"label": "变化幅度", "value": 200.0, "ref": "f0.e2"},
        ],
    }


@pytest.mark.parametrize(
    ("direction", "cause_phrase", "check_phrase"),
    [
        ("up", "明显上升", "单日冲高还是整期上升"),
        ("down", "明显下滑", "单日塌方还是整期缓降"),
    ],
)
def test_rule_wording_follows_the_dish_direction(
    direction, cause_phrase, check_phrase
):
    diagnosis = {"findings": [_dish_finding(direction)]}

    causes = rule_causes(diagnosis)
    checks = rule_checks(diagnosis)

    assert any(cause_phrase in cause["text"] for cause in causes)
    assert all(cause["evidence_refs"] for cause in causes)
    assert any(check_phrase in check["text"] for check in checks)
    assert all(check["evidence_refs"] for check in checks)


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        (TREND_SINGLE_DAY, "集中在单日"),
        (TREND_SINGLE_DAY_UP, "上升集中在单日"),
        (TREND_GRADUAL, "多数天数低于上期"),
        (TREND_GRADUAL_UP, "多数天数高于上期"),
        (TREND_DIFFUSE, "分散在多日"),
    ],
)
def test_dish_checks_state_the_classified_shape(shape, expected):
    """Once the Service classified the dish shape, the check cites it (Task25)."""
    direction = "up" if shape in (TREND_SINGLE_DAY_UP, TREND_GRADUAL_UP) else "down"
    finding = _dish_finding(direction)
    finding["evidence"].append(
        {"label": "「A」逐日走势", "value": shape, "detail": "", "ref": "f0.e3"}
    )

    checks = rule_checks({"findings": [finding]})

    assert any(expected in check["text"] for check in checks)
    assert any("f0.e3" in check["evidence_refs"] for check in checks)
