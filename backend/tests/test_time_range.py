"""Task42: the Agent chooses the window, including its baseline.

Dates are injected, so these tests state the calendar rule rather than depend on
the day the suite runs. The invariants under test are the ones the diagnosis
engine relies on: inclusive UTC days, a baseline that ends the day before the
window starts and has the same length, and an explicit ``partial`` mark for any
window that contains an unfinished day.
"""

from datetime import date, timedelta

import pytest

from backend.app.agent.time_range import (
    annotate_coverage,
    coverage_anchor,
    expression_uses_explicit_dates,
    resolve_explicit_range,
    resolve_time_range,
)
from backend.app.exceptions import ValidationError


def _span(window: dict) -> int:
    return (window["end_date"] - window["start_date"]).days + 1


def _assert_baseline_is_adjacent_and_equal(window: dict) -> None:
    assert window["baseline_end_date"] == window["start_date"] - timedelta(days=1)
    baseline_span = (
        window["baseline_end_date"] - window["baseline_start_date"]
    ).days + 1
    assert baseline_span == _span(window)


def test_no_expression_uses_default_seven_days_and_labels_the_source():
    window = resolve_time_range(None, today=date(2026, 10, 17))

    assert window["start_date"] == date(2026, 10, 11)
    assert window["end_date"] == date(2026, 10, 17)
    assert window["source"] == "default"
    assert window["partial"] is True
    _assert_baseline_is_adjacent_and_equal(window)


def test_this_week_on_a_monday_is_one_day_and_partial():
    window = resolve_time_range("本周", today=date(2026, 10, 12))

    assert window["start_date"] == date(2026, 10, 12)
    assert window["end_date"] == date(2026, 10, 12)
    assert window["baseline_start_date"] == date(2026, 10, 11)
    assert window["baseline_end_date"] == date(2026, 10, 11)
    assert window["source"] == "question"
    assert window["partial"] is True
    assert any("尚未过完" in note for note in window["notes"])


def test_last_week_is_a_full_previous_week_regardless_of_today():
    window = resolve_time_range("上周", today=date(2026, 10, 17))

    assert window["start_date"] == date(2026, 10, 5)
    assert window["end_date"] == date(2026, 10, 11)
    assert window["partial"] is False
    assert window["baseline_start_date"] == date(2026, 9, 28)
    _assert_baseline_is_adjacent_and_equal(window)


def test_month_end_window_crosses_into_the_previous_month():
    window = resolve_time_range("本月", today=date(2026, 10, 31))

    assert window["start_date"] == date(2026, 10, 1)
    assert window["end_date"] == date(2026, 10, 31)
    assert window["baseline_start_date"] == date(2026, 8, 31)
    assert window["baseline_end_date"] == date(2026, 9, 30)
    assert _span(window) == 31
    _assert_baseline_is_adjacent_and_equal(window)


def test_cross_year_last_month_and_last_week():
    month = resolve_time_range("上个月", today=date(2026, 1, 2))
    assert month["start_date"] == date(2025, 12, 1)
    assert month["end_date"] == date(2025, 12, 31)
    assert month["baseline_start_date"] == date(2025, 10, 31)

    week = resolve_time_range("last week", today=date(2026, 1, 2))
    assert week["start_date"] == date(2025, 12, 22)
    assert week["end_date"] == date(2025, 12, 28)
    assert week["baseline_start_date"] == date(2025, 12, 15)


def test_recent_days_accept_arabic_and_chinese_numbers():
    seven = resolve_time_range("最近7天", today=date(2026, 10, 17))
    assert seven["start_date"] == date(2026, 10, 11)
    assert seven["end_date"] == date(2026, 10, 17)

    fourteen = resolve_time_range("最近十四天", today=date(2026, 10, 17))
    assert fourteen["start_date"] == date(2026, 10, 4)

    two_weeks = resolve_time_range("过去两周", today=date(2026, 10, 17))
    assert two_weeks["start_date"] == date(2026, 10, 4)
    assert _span(two_weeks) == 14


def test_explicit_dates_are_used_as_given_and_are_not_partial_when_in_the_past():
    window = resolve_time_range(
        "2026-10-01 到 2026-10-07", today=date(2026, 10, 17)
    )

    assert window["start_date"] == date(2026, 10, 1)
    assert window["end_date"] == date(2026, 10, 7)
    assert window["source"] == "question"
    assert window["partial"] is False
    assert window["baseline_start_date"] == date(2026, 9, 24)
    _assert_baseline_is_adjacent_and_equal(window)


def test_reversed_explicit_dates_are_rejected():
    with pytest.raises(ValidationError):
        resolve_time_range("2026-10-07 到 2026-10-01", today=date(2026, 10, 17))


def test_ambiguous_expression_uses_one_rule_and_explains_which():
    window = resolve_time_range("上周和本月哪个更差", today=date(2026, 10, 17))

    assert window["start_date"] == date(2026, 10, 5)
    assert any("已按“上周”解析" in note for note in window["notes"])


def test_unrecognised_expression_degrades_to_default_with_a_note():
    window = resolve_time_range("旺季的时候", today=date(2026, 10, 17))

    assert window["source"] == "default"
    assert window["start_date"] == date(2026, 10, 11)
    assert any("无法从" in note for note in window["notes"])


def test_weekend_follow_up_uses_the_most_recent_completed_weekend():
    # Friday 2026-10-16: the weekend just gone is 10-10 / 10-11.
    window = resolve_time_range("那周末呢", today=date(2026, 10, 16))

    assert window["start_date"] == date(2026, 10, 10)
    assert window["end_date"] == date(2026, 10, 11)
    assert window["source"] == "question"
    assert window["partial"] is False
    assert window["baseline_start_date"] == date(2026, 10, 8)
    assert window["baseline_end_date"] == date(2026, 10, 9)
    assert not any("多个时间词" in note for note in window["notes"])
    _assert_baseline_is_adjacent_and_equal(window)


def test_last_weekend_on_a_sunday_is_the_previous_weekend():
    window = resolve_time_range("上周末", today=date(2026, 10, 18))

    assert window["start_date"] == date(2026, 10, 10)
    assert window["end_date"] == date(2026, 10, 11)
    assert window["partial"] is False
    _assert_baseline_is_adjacent_and_equal(window)


def test_bare_weekend_during_the_weekend_is_the_ongoing_one():
    window = resolve_time_range("周末", today=date(2026, 10, 17))

    assert window["start_date"] == date(2026, 10, 17)
    assert window["end_date"] == date(2026, 10, 18)
    assert window["partial"] is True
    assert any("未来" in note for note in window["notes"])


def test_this_weekend_marks_unfinished_days():
    window = resolve_time_range("本周末", today=date(2026, 10, 16))

    assert window["start_date"] == date(2026, 10, 17)
    assert window["end_date"] == date(2026, 10, 18)
    assert window["partial"] is True
    assert any("未来" in note for note in window["notes"])


def test_explicit_request_range_keeps_the_same_baseline_rule():
    window = resolve_explicit_range(
        date(2026, 10, 10), date(2026, 10, 16), today=date(2026, 10, 17)
    )

    assert window["source"] == "explicit"
    assert window["partial"] is False
    assert window["baseline_start_date"] == date(2026, 10, 3)
    _assert_baseline_is_adjacent_and_equal(window)


def test_coverage_anchor_moves_only_when_data_is_older_than_the_recent_window():
    today = date(2026, 10, 17)

    # Newest data is still inside the default 7-day window: relative phrases
    # keep their normal meaning.
    assert coverage_anchor(date(2026, 10, 11), today=today) == today
    # The dataset ended long ago: "recent" now means the last covered days.
    assert coverage_anchor(date(2023, 6, 30), today=today) == date(2023, 6, 30)
    assert coverage_anchor(None, today=today) == today


def test_annotate_coverage_labels_an_anchored_window_and_states_the_range():
    window = resolve_time_range(None, today=date(2023, 6, 30))
    annotated = annotate_coverage(
        window,
        {"start_date": date(2023, 1, 1), "end_date": date(2023, 6, 30)},
        anchored=True,
        today=date(2026, 10, 17),
    )

    assert annotated["source"] == "coverage"
    assert annotated["partial"] is False
    assert not any("尚未过完" in note for note in annotated["notes"])
    assert any("2023-01-01 至 2023-06-30" in note for note in annotated["notes"])
    assert any("2023-06-30" in note for note in annotated["notes"])


def test_annotate_coverage_does_not_relabel_explicit_outer_windows():
    window = resolve_explicit_range(
        date(2026, 10, 1), date(2026, 10, 7), today=date(2026, 10, 17)
    )
    annotated = annotate_coverage(
        window,
        {"start_date": date(2023, 1, 1), "end_date": date(2023, 6, 30)},
        anchored=False,
        today=date(2026, 10, 17),
    )

    assert annotated["source"] == "explicit"
    assert any("不在数据库可用数据范围内" in note for note in annotated["notes"])


def test_expression_uses_explicit_dates_detects_pinned_calendar_days():
    assert expression_uses_explicit_dates("2026-10-01 到 2026-10-07")
    assert not expression_uses_explicit_dates("上个月")
    assert not expression_uses_explicit_dates(None)
