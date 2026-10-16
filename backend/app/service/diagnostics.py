"""Store-anomaly diagnosis: deterministic detection only.

``sdd/经营异常诊断Agent审计与Tasks.md`` §T1. This module decides **whether**
something is anomalous and **what the supporting facts are**. It never calls an
LLM and never changes business state; the Agent layer
(:mod:`backend.app.agent.diagnosis`) takes over afterwards to propose *why*, and
may only cite the evidence assembled here.

Separating "cannot tell" from "really dropped" is the point of the whole module,
not a nicety. ``backend/app/seed.py`` ships **no orders at all**, so on a freshly
seeded database every baseline window is empty -- a naive comparison would report
a -100% collapse on order volume, revenue, average order value and every dish at
once. The rules below refuse to judge in that situation instead:

* a window with no sales cannot be a baseline;
* a baseline spread over fewer than :data:`MIN_BASELINE_DAYS` active days is too
  thin to call a trend;
* a dish absent from the baseline ranking is *unjudged*, never a 100% fall --
  the ranking is capped by ``top_limit``, so absence proves nothing;
* a current window that still includes today is annotated as incomplete, because
  its totals are only partial.

Statistics are never recomputed here. Every number comes from
:class:`~backend.app.service.stats.StatsService`, which also owns the branch
permission rule and raises ``ForbiddenError`` / ``NotFoundError`` before this
module sees anything -- so the scope check is inherited, not reimplemented.
"""

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.exceptions import ValidationError
from backend.app.models.user import StaffUser
from backend.app.service.audit import MAX_AUDIT_LIMIT, AuditService
# ``MAX_TOP_ITEMS`` is the statistics service's cap on the dish ranking; asking
# for it keeps the widest comparison window the statistics layer will serve.
from backend.app.service.stats import MAX_RANGE_DAYS, MAX_TOP_ITEMS, StatsService

# Length of the judged window when the caller does not pick one (matches the
# statistics page's default, so the two views describe the same period).
DEFAULT_WINDOW_DAYS = 7

# A baseline must span at least this many UTC days that actually sold something.
# One busy evening is not a trend, and with sparse data it is usually the *only*
# day with rows -- the exact shape that produces phantom anomalies.
MIN_BASELINE_DAYS = 3

# A move of this size (percent, either metric) is worth reporting; the larger one
# is severe enough to lead the page.
CHANGE_THRESHOLD_PCT = 30.0
CRITICAL_CHANGE_PCT = 60.0

# Cancellation is judged in *percentage points of the cancellation rate*, not as
# a relative change: the baseline rate can legitimately be 0, where a relative
# change is undefined -- yet "0 today, 5% last week" is precisely the escalation
# a manager needs to see.
CANCELLATION_RATE_DELTA_PP = 5.0
CRITICAL_CANCELLATION_RATE_PP = 15.0

# How many unjudged dish names a note lists before it summarises the rest.
MAX_SKIPPED_ITEMS_IN_NOTE = 5

# When this share of a window's total shortfall lands on one day, the fall is a
# single bad day rather than a trend -- the two need opposite responses, so the
# Service says which one it saw instead of leaving it to prose.
SHORTFALL_CONCENTRATION = 0.6

TREND_NONE = "无明显缺口"
TREND_SINGLE_DAY = "单日塌方"
TREND_GRADUAL = "整期缓降"
TREND_DIFFUSE = "分散波动"

# The five metrics of the MVP, in the order they are reported. Every one is a
# *fall* except cancellations, so the direction is part of the metric's meaning
# rather than a per-call argument.
VOLUME_METRICS = ("order_count", "revenue", "average_order_value")

# Metrics that are real per-day totals, so a day with no orders is a true zero.
# The ratios are deliberately excluded: an empty day has no average order value,
# and writing 0 there would manufacture a collapse out of missing data.
TOTAL_METRICS = ("order_count", "revenue")

METRIC_LABELS = {
    "order_count": "订单量",
    "revenue": "销售额",
    "average_order_value": "客单价",
    "cancelled_rate": "订单取消率",
    "dish_sales": "菜品销量",
}


def _to_date(value: Optional[Any], field: str) -> Optional[date]:
    """Coerce a query/test value into a ``date``, or reject it clearly.

    Deliberately mirrors the private helper in :mod:`backend.app.service.stats`
    rather than importing it: that name is module-private, and reaching across
    modules for it would make an internal detail load-bearing.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValidationError(f"{field} 的日期格式无效，请使用 YYYY-MM-DD。")


def _today() -> date:
    """Today in UTC -- the same day boundary the statistics aggregates use."""
    return datetime.now(timezone.utc).date()


def _active_days(summary: Dict[str, Any]) -> int:
    """Days inside the window that sold at least one order.

    A day whose orders were all cancelled does not count: it cannot stand in as
    a baseline for what a normal day looks like.
    """
    return sum(1 for point in summary.get("daily") or [] if point["order_count"] > 0)


def _has_rows(summary: Dict[str, Any]) -> bool:
    """Whether the window recorded anything at all, sales or cancellations."""
    return bool(summary.get("order_count") or summary.get("cancelled_count"))


def _change_pct(current: float, previous: float) -> Optional[float]:
    """Relative change, or ``None`` when the baseline is zero (undefined)."""
    if previous <= 0:
        return None
    return (float(current) - float(previous)) / float(previous) * 100.0


def _rate(cancelled: int, orders: int) -> float:
    """Cancellations as a percentage of everything placed, sold or not."""
    total = cancelled + orders
    if total <= 0:
        return 0.0
    return cancelled / total * 100.0


def _window_bounds(summary: Dict[str, Any]) -> Tuple[datetime, datetime]:
    """The window as a half-open ``[start, end)`` pair of UTC instants.

    The same bounds the statistics aggregates use, so an audit row and an order
    are placed inside or outside a window by one shared rule.
    """
    start = datetime.combine(summary["start_date"], time.min, tzinfo=timezone.utc)
    end = datetime.combine(
        summary["end_date"] + timedelta(days=1), time.min, tzinfo=timezone.utc
    )
    return start, end


def _daily_series(summary: Dict[str, Any], metric: str) -> List[Optional[float]]:
    """``metric`` for every UTC day of the window, oldest day first.

    ``StatsRepo.daily_totals`` groups over order rows, so a day with no orders
    produces **no row at all** -- the window's calendar and its data are not the
    same length. This helper re-aligns them to the calendar and marks each day:

    * a totals metric on a day with no rows is a genuine ``0.0`` -- nothing sold
      really does mean nothing sold;
    * a *ratio* (average order value, cancellation rate) on such a day is
      ``None``, because it has no denominator. Writing ``0`` there would invent
      precisely the collapse this module exists to refuse.

    ``None`` therefore means "undefined", and callers must not arithmetic it.
    """
    by_date = {point["date"]: point for point in summary.get("daily") or []}
    series: List[Optional[float]] = []
    day = summary["start_date"]
    while day <= summary["end_date"]:
        point = by_date.get(day)
        if point is None:
            series.append(0.0 if metric in TOTAL_METRICS else None)
        elif metric == "average_order_value":
            orders = point["order_count"]
            series.append(point["revenue"] / orders if orders else None)
        elif metric == "cancelled_rate":
            placed = point["order_count"] + point["cancelled_count"]
            series.append(point["cancelled_count"] / placed * 100.0 if placed else None)
        else:
            series.append(float(point[metric]))
        day += timedelta(days=1)
    return series


def _shape_evidence(
    current: Dict[str, Any], baseline: Dict[str, Any], metric: str
) -> List[Dict[str, Any]]:
    """Whether the shortfall landed on one day or spread across the window.

    A period total says how much was lost but not *how*: one closed evening and a
    week of steadily thinning traffic produce the same percentage and call for
    opposite responses. The Service answers that from the daily series, so the
    Agent never has to guess the shape from the total.

    The classification runs on totals only. A per-order average has no value on a
    day with no orders, so there is no per-day series to classify; those metrics
    get their raw series plus a plain statement that the shape is not computable.
    """
    label = METRIC_LABELS.get(metric, metric)
    current_series = _daily_series(current, metric)
    baseline_series = _daily_series(baseline, metric)
    if not current_series or not baseline_series:
        return [
            {
                "label": f"{label}逐日走势",
                "value": None,
                "detail": "统计层没有提供逐日数据，无法判断是单日塌方还是整期缓降。",
            }
        ]

    series_note = {
        "label": f"本期{label}逐日明细",
        "value": [None if v is None else round(v, 2) for v in current_series],
        "detail": (
            f"{current['start_date']} 至 {current['end_date']}（共 {len(current_series)} 天），"
            "按日顺序；null 表示当天没有记录。"
        ),
    }

    if metric not in TOTAL_METRICS:
        return [
            {
                "label": f"{label}逐日走势",
                "value": None,
                "detail": (
                    f"{label}是每单平均量，没有成交的日期没有取值，"
                    "因此无法按日判断是单日塌方还是整期缓降。"
                ),
            },
            series_note,
        ]

    baseline_average = sum(baseline_series) / len(baseline_series)
    shortfall = sum(baseline_series) - sum(current_series)
    worst_shortfall = max(baseline_average - value for value in current_series)
    below_average = sum(1 for value in current_series if value < baseline_average)

    if shortfall <= 0:
        shape = TREND_NONE
    elif worst_shortfall / shortfall >= SHORTFALL_CONCENTRATION:
        shape = TREND_SINGLE_DAY
    elif below_average / len(current_series) >= SHORTFALL_CONCENTRATION:
        shape = TREND_GRADUAL
    else:
        shape = TREND_DIFFUSE

    # Totals only from here on, so the series has no ``None`` gaps to guard.
    worst_index = min(range(len(current_series)), key=lambda i: current_series[i])
    quiet_days = sum(1 for value in current_series if not value)
    return [
        {
            "label": f"{label}逐日走势",
            "value": shape,
            "detail": (
                f"上期日均 {baseline_average:.2f}；本期有 {below_average}/{len(current_series)}"
                f" 天低于该值，其中 {quiet_days} 天完全无成交；最差单日 "
                f"{current['start_date'] + timedelta(days=worst_index)}"
                f"（{current_series[worst_index]:.2f}）。"
            ),
        },
        series_note,
    ]


def _dish_trend_gap(item_name: str) -> Dict[str, Any]:
    """State, rather than paper over, the missing per-dish daily breakdown.

    ``StatsRepo.daily_totals`` has no dish dimension and ``dish_totals`` has no
    day dimension, so nothing in the system can say whether one dish's fall was a
    single bad day. Saying so is the honest answer.
    """
    return {
        "label": f"「{item_name}」逐日走势",
        "value": None,
        "detail": "现有统计只有整店逐日数据，没有单品逐日数据，无法判断是单日塌方还是整期缓降。",
    }


async def _cancellation_attribution(
    branch_id: int,
    current: Dict[str, Any],
    *,
    staff: Optional[StaffUser],
    session: Optional[AsyncSession],
) -> List[Dict[str, Any]]:
    """Split the window's cancellations into customer-initiated and staff-initiated.

    The statistics layer reports *how many* orders ended up cancelled but not
    *who* cancelled them, and the two mean different things: a customer walking
    away is a demand signal, a staff cancellation is an operations one. The only
    place that distinction exists is the audit trail, where a customer's own
    cancellation is stamped ``by: customer`` (``OrderService.cancel_order``)
    while a staff one is not.

    Two honest caveats, both reported as evidence rather than hidden:

    * the counts are keyed on when the *cancellation happened*, whereas the
      statistics' ``cancelled_count`` is keyed on when the order was *placed*;
    * ``AuditService.list_logs`` returns the newest rows up to a cap and takes no
      date range, so a busy branch can overflow it -- if the cap was reached
      before the window opened, the split is flagged as incomplete.
    """
    if staff is None:
        return [
            {
                "label": "本期取消归因",
                "value": None,
                "detail": "调用方没有提供员工身份，无法按门店读取操作日志，因此无法区分顾客取消与员工取消。",
            }
        ]

    logs = await AuditService.list_logs(
        staff,
        branch_id=branch_id,
        action="order.status_changed",
        limit=MAX_AUDIT_LIMIT,
        session=session,
    )
    start, end = _window_bounds(current)
    cancellations = [
        log
        for log in logs
        if (log.details or {}).get("to") == "cancelled" and start <= log.created_at < end
    ]
    by_customer = sum(
        1 for log in cancellations if (log.details or {}).get("by") == "customer"
    )
    by_staff = sum(
        1
        for log in cancellations
        if (log.details or {}).get("by") != "customer" and log.actor_type == "staff"
    )
    # Neither marker: the row was written with no actor at all. Rare on the API
    # paths (both cancel flows name a party), but it must not be folded into
    # "staff" -- an unattributed row is not evidence of anything.
    unattributed = len(cancellations) - by_customer - by_staff

    evidence: List[Dict[str, Any]] = [
        {
            "label": "本期取消归因（按取消发生时间）",
            "value": {
                "customer": by_customer,
                "staff": by_staff,
                "unattributed": unattributed,
                "total": len(cancellations),
            },
            "detail": (
                "顾客主动取消指顾客在订单上自行取消（日志标记 by=customer）；"
                "员工取消指员工在后台取消；未标明操作者的既不算顾客也不算员工。"
                "注意口径：这里数的是「取消动作发生在本期的次数」，与按订单创建时间统计的"
                "「取消订单数」不是同一个数。"
            ),
        }
    ]

    if len(logs) >= MAX_AUDIT_LIMIT:
        oldest = logs[-1].created_at
        if oldest is not None and oldest > start:
            evidence.append(
                {
                    "label": "操作日志覆盖范围",
                    "value": None,
                    "detail": (
                        f"操作日志按条数上限读取，最早只读到 {oldest}，未覆盖本期起点 {start}，"
                        "上面的归因可能不完整。"
                    ),
                }
            )
    return evidence


def _resolve_window(
    start_date: Optional[Any], end_date: Optional[Any]
) -> Tuple[date, date, date, date]:
    """Both windows: the judged one and the equal-length one before it.

    The baseline ends the day before the current window starts, so the two never
    overlap and the comparison is like-for-like regardless of window length.
    """
    end = _to_date(end_date, "结束日期") or _today()
    start = _to_date(start_date, "开始日期") or (end - timedelta(days=DEFAULT_WINDOW_DAYS - 1))
    if start > end:
        raise ValidationError("开始日期不能晚于结束日期。")
    span_days = (end - start).days + 1
    if span_days > MAX_RANGE_DAYS:
        raise ValidationError(f"诊断区间最长 {MAX_RANGE_DAYS} 天。")

    baseline_end = start - timedelta(days=1)
    baseline_start = baseline_end - timedelta(days=span_days - 1)
    return start, end, baseline_start, baseline_end


def _period(summary: Dict[str, Any]) -> Dict[str, Any]:
    """The comparable slice of a summary, plus its active-day count."""
    return {
        "start_date": summary["start_date"],
        "end_date": summary["end_date"],
        "order_count": summary["order_count"],
        "cancelled_count": summary["cancelled_count"],
        "revenue": summary["revenue"],
        "average_order_value": summary["average_order_value"],
        "units_sold": summary["units_sold"],
        "active_days": _active_days(summary),
    }


def _finding(
    metric: str,
    *,
    current: float,
    previous: float,
    change_pct: float,
    direction: str,
    severity: str,
    evidence: List[Dict[str, Any]],
    menu_item_id: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "metric": metric,
        "label": METRIC_LABELS.get(metric, metric),
        "menu_item_id": menu_item_id,
        "direction": direction,
        "severity": severity,
        "current": round(float(current), 2),
        "previous": round(float(previous), 2),
        "change_pct": round(float(change_pct), 2),
        "evidence": evidence,
    }


def _severity(change_pct: float) -> str:
    """A fall at or past :data:`CRITICAL_CHANGE_PCT` leads the page."""
    return "critical" if change_pct <= -CRITICAL_CHANGE_PCT else "warning"


def _volume_findings(
    current: Dict[str, Any], baseline: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Falling order volume, revenue and average order value."""
    findings: List[Dict[str, Any]] = []
    notes: List[str] = []
    for metric in VOLUME_METRICS:
        change = _change_pct(current[metric], baseline[metric])
        if change is None:
            # The baseline gate above already rules this out for order_count and
            # revenue; the guard keeps a zero-priced catalogue from dividing by
            # zero on average_order_value.
            notes.append(f"上期{METRIC_LABELS[metric]}为 0，本指标无法判断。")
            continue
        if change > -CHANGE_THRESHOLD_PCT:
            continue
        evidence = [
            {
                "label": f"本期{METRIC_LABELS[metric]}",
                "value": round(float(current[metric]), 2),
                "detail": f"{current['start_date']} 至 {current['end_date']}",
            },
            {
                "label": f"上期{METRIC_LABELS[metric]}",
                "value": round(float(baseline[metric]), 2),
                "detail": f"{baseline['start_date']} 至 {baseline['end_date']}",
            },
            {
                "label": "变化幅度",
                "value": round(float(change), 2),
                "detail": f"阈值 {CHANGE_THRESHOLD_PCT}%，超过即判定异常",
            },
            {
                "label": "上期有效经营天数",
                "value": _active_days(baseline),
                "detail": f"至少 {MIN_BASELINE_DAYS} 天才会用于判断",
            },
        ]
        # How the fall was distributed, not just how big it was: one dead evening
        # and a week of decline call for different actions.
        evidence.extend(_shape_evidence(current, baseline, metric))
        findings.append(
            _finding(
                metric,
                current=current[metric],
                previous=baseline[metric],
                change_pct=change,
                direction="down",
                severity=_severity(change),
                evidence=evidence,
            )
        )
    return findings, notes


def _cancellation_finding(
    current: Dict[str, Any], baseline: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """A rise in the cancellation rate, measured in percentage points."""
    current_rate = _rate(current["cancelled_count"], current["order_count"])
    baseline_rate = _rate(baseline["cancelled_count"], baseline["order_count"])
    delta = current_rate - baseline_rate
    if delta < CANCELLATION_RATE_DELTA_PP:
        return None
    return _finding(
        "cancelled_rate",
        current=current_rate,
        previous=baseline_rate,
        change_pct=delta,
        direction="up",
        severity=(
            "critical" if delta >= CRITICAL_CANCELLATION_RATE_PP else "warning"
        ),
        evidence=[
            {
                "label": "本期取消率",
                "value": round(current_rate, 2),
                "detail": (
                    f"{current['cancelled_count']} 次取消 / "
                    f"{current['cancelled_count'] + current['order_count']} 单"
                ),
            },
            {
                "label": "上期取消率",
                "value": round(baseline_rate, 2),
                "detail": (
                    f"{baseline['cancelled_count']} 次取消 / "
                    f"{baseline['cancelled_count'] + baseline['order_count']} 单"
                ),
            },
            {
                "label": "变化幅度（百分点）",
                "value": round(delta, 2),
                "detail": f"上升 {CANCELLATION_RATE_DELTA_PP} 个百分点即判定异常",
            },
        ],
    )


def _dish_findings(
    current: Dict[str, Any], baseline: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Dish-by-dish falls, judged only where a like-for-like baseline exists.

    A dish is compared only when it appears in *both* rankings. The rankings are
    capped by ``top_limit`` and sorted by volume, so a dish missing from the
    baseline is far more likely to have ranked below the cut-off than to have
    sold nothing -- reporting it as a total collapse would be a fabrication.
    """
    findings: List[Dict[str, Any]] = []
    notes: List[str] = []

    baseline_items = {item["menu_item_id"]: item for item in baseline["top_items"]}
    current_items = {item["menu_item_id"]: item for item in current["top_items"]}

    skipped = [
        item["name"] for item in current["top_items"] if item["menu_item_id"] not in baseline_items
    ]
    skipped += [
        item["name"]
        for item in baseline["top_items"]
        if item["menu_item_id"] not in current_items
    ]
    if skipped:
        shown = "、".join(skipped[:MAX_SKIPPED_ITEMS_IN_NOTE])
        more = f" 等 {len(skipped)} 个" if len(skipped) > MAX_SKIPPED_ITEMS_IN_NOTE else ""
        notes.append(
            f"菜品 {shown}{more} 未同时出现在上期与本期销量排行内，"
            "无法判断其销量变化（不视为下降）。"
        )

    for item in current["top_items"]:
        previous = baseline_items.get(item["menu_item_id"])
        if previous is None:
            continue
        change = _change_pct(item["quantity"], previous["quantity"])
        if change is None or change > -CHANGE_THRESHOLD_PCT:
            continue
        findings.append(
            _finding(
                "dish_sales",
                current=item["quantity"],
                previous=previous["quantity"],
                change_pct=change,
                direction="down",
                severity=_severity(change),
                menu_item_id=item["menu_item_id"],
                evidence=[
                    {
                        "label": f"「{item['name']}」本期销量",
                        "value": item["quantity"],
                        "detail": f"{current['start_date']} 至 {current['end_date']}",
                    },
                    {
                        "label": f"「{item['name']}」上期销量",
                        "value": previous["quantity"],
                        "detail": f"{baseline['start_date']} 至 {baseline['end_date']}",
                    },
                    {
                        "label": "变化幅度",
                        "value": round(float(change), 2),
                        "detail": f"阈值 {CHANGE_THRESHOLD_PCT}%，超过即判定异常",
                    },
                    # No dish dimension exists in the daily series, so unlike the
                    # volume metrics this fall cannot be shaped. Say so.
                    _dish_trend_gap(item["name"]),
                ],
            )
        )
    return findings, notes


def _attach_evidence_refs(findings: List[Dict[str, Any]]) -> None:
    """Give every evidence item a stable handle, in place.

    ``f{index}.e{index}`` is unique across the payload, so the Agent can cite a
    fact by name and a citation either resolves or is dropped -- no fuzzy
    matching between prose and numbers.
    """
    for finding_index, finding in enumerate(findings):
        for evidence_index, item in enumerate(finding["evidence"]):
            item["ref"] = f"f{finding_index}.e{evidence_index}"


class DiagnosisService:
    @staticmethod
    async def diagnose(
        branch_id: Optional[int] = None,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        *,
        staff: Optional[StaffUser] = None,
        session: Optional[AsyncSession] = None,
    ) -> Dict[str, Any]:
        """Judge one branch's window against the equal-length window before it.

        Two reads of :meth:`StatsService.get_branch_summary` supply every number
        -- no aggregate is written here. Branch scope rides along with ``staff``:
        an out-of-scope request raises ``ForbiddenError`` from the statistics
        service before any comparison happens.

        Returns the shape described by
        :class:`~backend.app.schemas.diagnostics.DiagnosisResponse`. ``status`` is
        ``insufficient_data`` when the baseline cannot support a comparison; in
        that case ``findings`` is empty **by design** and ``notes`` says why.
        """
        start, end, baseline_start, baseline_end = _resolve_window(start_date, end_date)

        current = await StatsService.get_branch_summary(
            branch_id,
            start,
            end,
            top_limit=MAX_TOP_ITEMS,
            staff=staff,
            session=session,
        )
        baseline = await StatsService.get_branch_summary(
            branch_id,
            baseline_start,
            baseline_end,
            top_limit=MAX_TOP_ITEMS,
            staff=staff,
            session=session,
        )

        current_period = _period(current)
        baseline_period = _period(baseline)
        notes: List[str] = []
        findings: List[Dict[str, Any]] = []

        # Nothing anywhere: there is no comparison to make, only a reason to give.
        if not _has_rows(current) and not _has_rows(baseline):
            notes.append("所选区间与它的上一个对比区间都没有任何订单记录，无法进行经营诊断。")
            return _payload(
                current, current_period, baseline_period, notes, findings,
                status="insufficient_data",
            )

        # Baseline gate: a window with no sales, or one that is too thin to
        # describe a normal week, cannot be a "before" picture. Either way the
        # answer is a refusal, not a finding -- the two are different results and
        # the caller has to be able to tell them apart.
        judged = False
        if not baseline["order_count"]:
            notes.append(
                f"上期（{baseline_start} 至 {baseline_end}）没有成交订单，"
                "无法判断本期各项指标是升是降。"
            )
        elif baseline_period["active_days"] < MIN_BASELINE_DAYS:
            notes.append(
                f"上期有效经营天数仅 {baseline_period['active_days']} 天"
                f"（少于 {MIN_BASELINE_DAYS} 天），样本不足以判断趋势。"
            )
        else:
            judged = True
            volume_findings, volume_notes = _volume_findings(current, baseline)
            findings.extend(volume_findings)
            notes.extend(volume_notes)

            cancellation = _cancellation_finding(current, baseline)
            if cancellation is not None:
                # Who walked away matters as much as how many: the statistics
                # report the count but not the side, and only the audit trail
                # knows. The service layer is allowed to read it; the Agent is not.
                cancellation["evidence"].extend(
                    await _cancellation_attribution(
                        current["branch_id"], current, staff=staff, session=session
                    )
                )
                findings.append(cancellation)

            dish_findings, dish_notes = _dish_findings(current, baseline)
            findings.extend(dish_findings)
            notes.extend(dish_notes)

            # A window that still contains today is only partly elapsed, so its
            # totals are partial -- the drop may be an artefact of the clock.
            if end >= _today():
                notes.append(
                    f"本期结束日 {end} 尚未过完，本期数据不完整，下降幅度可能被高估。"
                )

        _attach_evidence_refs(findings)
        return _payload(
            current,
            current_period,
            baseline_period,
            notes,
            findings,
            status="ok" if judged else "insufficient_data",
        )


def _payload(
    current: Dict[str, Any],
    current_period: Dict[str, Any],
    baseline_period: Dict[str, Any],
    notes: List[str],
    findings: List[Dict[str, Any]],
    *,
    status: str,
) -> Dict[str, Any]:
    """The response body, with the thresholds that produced the findings."""
    return {
        "branch_id": current["branch_id"],
        "branch_name": current["branch_name"],
        "status": status,
        "notes": notes,
        "current_period": current_period,
        "baseline_period": baseline_period,
        "thresholds": {
            "change_threshold_pct": CHANGE_THRESHOLD_PCT,
            "critical_change_pct": CRITICAL_CHANGE_PCT,
            "cancellation_rate_delta_pp": CANCELLATION_RATE_DELTA_PP,
            "critical_cancellation_rate_pp": CRITICAL_CANCELLATION_RATE_PP,
            "min_baseline_days": MIN_BASELINE_DAYS,
        },
        "findings": findings,
    }


__all__ = [
    "CANCELLATION_RATE_DELTA_PP",
    "CHANGE_THRESHOLD_PCT",
    "CRITICAL_CHANGE_PCT",
    "CRITICAL_CANCELLATION_RATE_PP",
    "DEFAULT_WINDOW_DAYS",
    "DiagnosisService",
    "MAX_SKIPPED_ITEMS_IN_NOTE",
    "METRIC_LABELS",
    "MIN_BASELINE_DAYS",
    "SHORTFALL_CONCENTRATION",
    "TOTAL_METRICS",
    "TREND_DIFFUSE",
    "TREND_GRADUAL",
    "TREND_NONE",
    "TREND_SINGLE_DAY",
    "VOLUME_METRICS",
]
