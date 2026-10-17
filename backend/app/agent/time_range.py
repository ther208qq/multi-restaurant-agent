"""Agent-chosen diagnosis windows (Task42).

The staff member asks a question; the Agent decides *when* to look. This module
turns the time expression it found in that question into an inclusive date
window plus the adjacent equal-length baseline the deterministic diagnosis
already requires.

Design rules:

* **Adjacent and equal-length.** ``baseline_end`` is always the day before
  ``start`` and the two windows have the same number of days. That is the
  invariant ``DiagnosisService.diagnose`` relies on; a "last week vs this
  month" comparison would not be one.
* **Partial windows say so.** A window containing today (or a future day) has
  not finished collecting orders. It is still returned -- refusing would make
  "this week" unusable -- but ``partial`` and ``notes`` make the incompleteness
  explicit so the Agent cannot present an unfinished day as a completed one.
* **Unrecognised input degrades, it does not guess.** With no usable time
  expression the default is the last seven days (today included) and
  ``source="default"``; with an expression that matches nothing, the same
  default applies with a note explaining it. The caller can always tell an
  inferred window from a requested one.
* **UTC days.** The statistics and diagnosis services aggregate UTC days, so
  "today" is the UTC date here too. Week boundaries are Monday-based.
"""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

from backend.app.exceptions import ValidationError

DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 366

_WEEKDAYS_MONDAY = 0

_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

_NUMBER = r"(\d+|[零一二两三四五六七八九十]+)"

# Labels that are widened by a more specific phrase in the same sentence.
_GENERIC_LABELS = {"最近", "周末"}
# A weekend phrase contains the whole-week wording; only the narrower one counts.
_BROADER_LABELS = {"上周末": "上周", "本周末": "本周"}

# (pattern, label, kind). Order is precedence: the first match wins. An
# expression containing several time words gets a note naming the one used.
_PATTERNS = [
    (re.compile(r"今天|今日|\btoday\b", re.I), "今天", "today"),
    (re.compile(r"昨天|昨日|\byesterday\b", re.I), "昨天", "yesterday"),
    (re.compile(r"前天|前日", re.I), "前天", "day_before_yesterday"),
    (
        re.compile(rf"(?:最近|近|过去)\s*{_NUMBER}\s*(?:天|日)|\blast\s+{_NUMBER}\s*days?\b", re.I),
        "最近 N 天",
        "last_days",
    ),
    (
        re.compile(rf"(?:最近|近|过去)\s*{_NUMBER}\s*(?:周|星期)|\blast\s+{_NUMBER}\s*weeks?\b", re.I),
        "最近 N 周",
        "last_weeks",
    ),
    (
        re.compile(r"最近\s*(?:一个月|1\s*个月)|近\s*(?:一个月|1\s*个月)|过去\s*(?:一个月|1\s*个月)|\bpast\s+month\b", re.I),
        "最近一个月",
        "last_30_days",
    ),
    # Weekend phrases come before the whole-week rules: "上周末" contains
    # "上周" and "本周末" contains "本周", but names a two-day window.
    (
        re.compile(r"上周末|上个周末|\blast\s+weekend\b", re.I),
        "上周末",
        "last_weekend",
    ),
    (
        re.compile(r"本周末|这周末|这个周末|\bthis\s+weekend\b", re.I),
        "本周末",
        "this_weekend",
    ),
    (
        re.compile(r"周末|\bweekend\b", re.I),
        "周末",
        "weekend",
    ),
    (
        re.compile(r"本周|这周|本星期|这个星期|\bthis\s+week\b", re.I),
        "本周",
        "this_week",
    ),
    (
        re.compile(r"上周|上星期|上个星期|\blast\s+week\b", re.I),
        "上周",
        "last_week",
    ),
    (
        re.compile(r"本月|这个月|当月|\bthis\s+month\b", re.I),
        "本月",
        "this_month",
    ),
    (
        re.compile(r"上月|上个月|\blast\s+month\b", re.I),
        "上月",
        "last_month",
    ),
    (
        re.compile(r"(?:最近|近期|近来)\s*(?:几天)?|\brecently\b", re.I),
        "最近",
        "last_days_default",
    ),
]


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _parse_number(raw: str) -> int:
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    if raw == "十":
        return 10
    if "十" in raw:
        tens, _, ones = raw.partition("十")
        value = (_CN_DIGITS.get(tens, 1) if tens else 1) * 10
        value += _CN_DIGITS.get(ones, 0) if ones else 0
        return value
    if len(raw) == 1 and raw in _CN_DIGITS:
        return _CN_DIGITS[raw]
    raise ValueError(f"无法解析数量：{raw}")


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _window(
    start: date,
    end: date,
    *,
    source: str,
    expression: Optional[str],
    today: date,
    notes: Optional[list] = None,
) -> Dict[str, Any]:
    if start > end:
        raise ValidationError("开始日期不能晚于结束日期。")
    span = (end - start).days + 1
    if span > MAX_WINDOW_DAYS:
        raise ValidationError(f"诊断区间最长 {MAX_WINDOW_DAYS} 天。")

    baseline_end = start - timedelta(days=1)
    baseline_start = baseline_end - timedelta(days=span - 1)

    messages = list(notes or [])
    partial = end >= today
    if end > today:
        messages.append(f"结束日 {end} 在未来，该日期没有完整数据。")
    elif end == today:
        messages.append(f"窗口包含尚未过完的今天（{today}），当天数据不完整。")

    return {
        "start_date": start,
        "end_date": end,
        "baseline_start_date": baseline_start,
        "baseline_end_date": baseline_end,
        "source": source,
        "expression": expression,
        "partial": partial,
        "notes": messages,
    }


def _weekend(today: date, *, weeks_back: int) -> tuple:
    """The Saturday–Sunday pair of a weeks-ago week (0 = this week)."""
    monday = today - timedelta(days=today.weekday())
    saturday = monday - timedelta(weeks=weeks_back) + timedelta(days=5)
    return saturday, saturday + timedelta(days=1)


def _range_for(kind: str, match: re.Match, today: date) -> tuple:
    """Map one matched kind to ``(start, end)``."""
    if kind == "today":
        return today, today
    if kind == "yesterday":
        day = today - timedelta(days=1)
        return day, day
    if kind == "day_before_yesterday":
        day = today - timedelta(days=2)
        return day, day

    if kind == "last_days":
        raw = next(group for group in match.groups() if group)
        days = max(1, _parse_number(raw))
        return today - timedelta(days=days - 1), today
    if kind == "last_weeks":
        raw = next(group for group in match.groups() if group)
        days = max(1, _parse_number(raw)) * 7
        return today - timedelta(days=days - 1), today
    if kind == "last_30_days":
        return today - timedelta(days=29), today
    if kind == "last_days_default":
        return today - timedelta(days=DEFAULT_WINDOW_DAYS - 1), today

    if kind == "this_week":
        return today - timedelta(days=today.weekday()), today
    if kind == "last_week":
        monday = today - timedelta(days=today.weekday())
        return monday - timedelta(days=7), monday - timedelta(days=1)
    if kind == "last_weekend":
        return _weekend(today, weeks_back=1)
    if kind == "this_weekend":
        return _weekend(today, weeks_back=0)
    if kind == "weekend":
        # A bare "weekend" in a follow-up means the one just gone, except while
        # that weekend is still current: then it is the ongoing (partial) one.
        return _weekend(today, weeks_back=0 if today.weekday() >= 5 else 1)

    if kind == "this_month":
        return _month_start(today), today
    if kind == "last_month":
        last_end = _month_start(today) - timedelta(days=1)
        return _month_start(last_end), last_end

    raise ValidationError(f"不支持的时间表达类型：{kind}")


_DATE_PATTERN = re.compile(
    r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?"
)


def _explicit_dates(expression: str) -> Optional[tuple]:
    found = []
    for match in _DATE_PATTERN.finditer(expression):
        try:
            found.append(
                date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            )
        except ValueError:
            raise ValidationError(
                f"日期 “{match.group(0)}” 无效，请使用 YYYY-MM-DD 格式。"
            )
    if not found:
        return None
    if len(found) == 1:
        return found[0], found[0]
    return found[0], found[-1]


def _matched_labels(expression: str) -> list:
    labels = [
        label
        for pattern, label, _kind in _PATTERNS
        if pattern.search(expression)
    ]
    for narrower, broader in _BROADER_LABELS.items():
        if narrower in labels and broader in labels:
            labels.remove(broader)
    # "最近7天" also matches the generic "最近" rule, and "上周末" also matches
    # "周末"; the specific phrase is not an ambiguity, so only the specific
    # labels are reported there.
    specific = [label for label in labels if label not in _GENERIC_LABELS]
    return specific if specific else labels


def resolve_time_range(
    expression: Optional[str],
    *,
    today: Optional[date] = None,
    source: str = "question",
) -> Dict[str, Any]:
    """Resolve a natural-language time expression into window + baseline.

    Returns the shape of :class:`~backend.app.schemas.diagnostics.InvestigationWindow`
    fields. ``expression`` may be ``None`` or empty, which yields the default
    seven-day window with ``source="default"``.
    """
    today = today or _today_utc()
    original = (expression or "").strip()
    if not original:
        start = today - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
        return _window(
            start,
            today,
            source="default",
            expression=None,
            today=today,
        )

    explicit = _explicit_dates(original)
    if explicit is not None:
        labels = _matched_labels(original)
        notes = []
        if labels:
            notes.append(
                f"表达同时包含日期与“{labels[0]}”，已按显式日期解析。"
            )
        return _window(
            explicit[0],
            explicit[1],
            source=source,
            expression=original,
            today=today,
            notes=notes,
        )

    labels = _matched_labels(original)
    for pattern, label, kind in _PATTERNS:
        match = pattern.search(original)
        if not match:
            continue
        try:
            start, end = _range_for(kind, match, today)
        except ValueError:
            # A malformed quantity is an unrecognised expression, not a crash.
            break
        notes = []
        others = [other for other in labels if other != label]
        if others:
            notes.append(
                f"表达同时包含多个时间词（{label}、{'、'.join(others)}），"
                f"已按“{label}”解析。"
            )
        return _window(
            start,
            end,
            source=source,
            expression=original,
            today=today,
            notes=notes,
        )

    start = today - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
    return _window(
        start,
        today,
        source="default",
        expression=original,
        today=today,
        notes=[f"无法从“{original}”确定时间区间，已使用默认最近 7 天。"],
    )


def resolve_explicit_range(
    start_date: date,
    end_date: date,
    *,
    today: Optional[date] = None,
    source: str = "explicit",
) -> Dict[str, Any]:
    """Window for dates the caller supplied, keeping the same baseline rule."""
    today = today or _today_utc()
    return _window(
        start_date,
        end_date,
        source=source,
        expression=None,
        today=today,
    )


def expression_uses_explicit_dates(expression: Optional[str]) -> bool:
    """Whether the question pinned real dates (rather than a relative phrase)."""
    return bool(expression and _DATE_PATTERN.search(expression))


def coverage_anchor(
    data_end: Optional[date], *, today: Optional[date] = None
) -> date:
    """The date relative expressions should resolve against.

    Normally that is today. But when the newest available data is older than
    the default recent window (an imported historical dataset, for example),
    "最近 7 天" would resolve to a window the database cannot answer. In that
    case the Agent anchors relative phrases to the last day that has data --
    "the last seven days of available data" -- instead of returning an empty
    period. A dataset that ended within the last week is left alone: recent
    really does include those days.
    """
    today = today or _today_utc()
    if data_end and data_end < today - timedelta(days=DEFAULT_WINDOW_DAYS - 1):
        return data_end
    return today


def annotate_coverage(
    window: Dict[str, Any],
    coverage: Optional[Dict[str, Any]],
    *,
    anchored: bool = False,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """State the observed data range on a window, and label coverage anchoring.

    ``anchored`` is set by the caller when :func:`coverage_anchor` moved the
    reference date. Only then is the source changed to ``"coverage"``: an
    explicit date window keeps its own source even if it sits outside the data.
    """
    if not coverage:
        return window
    data_start = coverage.get("start_date")
    data_end = coverage.get("end_date")
    if data_start is None or data_end is None:
        return window

    today = today or _today_utc()
    adjusted = dict(window)
    notes = list(window.get("notes") or [])
    notes.append(f"数据库可用数据覆盖 {data_start} 至 {data_end}。")

    if anchored:
        adjusted["source"] = "coverage"
        # The anchor is the last covered day, not the real today. The window is
        # therefore history: it is not "partial", and the resolver's note about
        # an unfinished day refers to the anchor, not to the calendar.
        adjusted["partial"] = False
        notes = [
            note
            for note in notes
            if "尚未过完的今天" not in note and "在未来" not in note
        ]
        notes.append(
            f"最近可用数据截止 {data_end}，相对时间已按该日期解析"
            f"（今天为 {today}）。"
        )
        if window.get("end_date") is not None and window["end_date"] > data_end:
            notes.append(
                f"该窗口的结束日 {window['end_date']} 晚于最近可用数据日 "
                f"{data_end}，超出部分没有数据。"
            )
    elif (
        window.get("start_date") is not None
        and window.get("end_date") is not None
        and (
            window["end_date"] < data_start
            or window["start_date"] > data_end
        )
    ):
        notes.append("该查询窗口不在数据库可用数据范围内。")

    adjusted["notes"] = notes
    return adjusted


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "MAX_WINDOW_DAYS",
    "annotate_coverage",
    "coverage_anchor",
    "expression_uses_explicit_dates",
    "resolve_explicit_range",
    "resolve_time_range",
]
