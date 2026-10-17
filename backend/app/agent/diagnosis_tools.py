"""Read-only tools the diagnosis investigation Agent may call (Task41).

Every tool is a thin adapter over an existing Service method -- there is no SQL
here, and the branch is baked into the request context, never accepted from the
model. That is the permission boundary: the LLM chooses *what* to look at and
*which window*, not *whose data*.

Each result carries a compact JSON payload plus stable evidence refs. The refs
are namespaced per call (``daily2.e3``, ``peers1.e2``, ...) so citing the same
tool twice cannot make two different facts share a handle. ``get_diagnosis``
keeps the Service's own ``f{N}.e{M}`` refs untouched -- those are what the
existing analysis guardrails already validate.

Tools never raise: a rejected call returns ``{"status": "error", ...}`` to the
model and is recorded as a failed step, which keeps "we could not check this"
visible in the investigation trace instead of aborting the whole turn.
"""

import asyncio
import copy
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from langchain_core.tools import BaseTool, tool
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.harness import RunTrace, estimate_tokens
from backend.app.agent.time_range import (
    annotate_coverage,
    coverage_anchor,
    expression_uses_explicit_dates,
    resolve_explicit_range,
    resolve_time_range as resolve_window_expression,
)
from backend.app.exceptions import ValidationError
from backend.app.models.user import StaffUser
from backend.app.service.audit import AuditService, audit_log_payload
from backend.app.service.branch_peers import BranchPeerService
from backend.app.service.business_profile import BusinessProfileService
from backend.app.service.diagnostics import DiagnosisService, DishDailyService
from backend.app.service.stats import StatsService

logger = logging.getLogger("diagnosis_tools")

MAX_DIAGNOSIS_FINDINGS = 12
MAX_NOTES = 12
MAX_DISHES = 10
MAX_SERIES_POINTS = 62
MAX_NOTABLE_DAYS = 5
MAX_PEER_ROWS = 10
MAX_OPERATIONAL_CHANGES = 30

# Every tool result is serialised under one budget, independent of the provider
# tokenizer. The estimate below is deterministic (CJK = 1 token, other = 4 chars
# per token) and matches the harness context budget.
MAX_TOOL_OUTPUT_CHARS = 6000
MAX_TOOL_OUTPUT_TOKENS = 3000
# Room for the ``meta`` object so the final serialised text still fits.
_META_CHAR_RESERVE = 256
_META_TOKEN_RESERVE = 64

# Changes that can plausibly explain a sales movement. Order and reservation
# status churn is deliberately excluded: it is high-volume and rarely causal,
# and including it would crowd the relevant rows out of the bounded result.
OPERATIONAL_ACTIONS = (
    "menu_item.updated",
    "menu_item.created",
    "menu_category.created",
    "stock.updated",
    "reservation.table_reassigned",
    "reservation.rescheduled",
    "branch.updated",
    "table.created",
    "table.updated",
    "table.deleted",
)

# Names exposed to the model. Used to report what a budget-exhausted turn did
# not get to check.
TOOL_NAMES = (
    "resolve_time_range",
    "get_diagnosis",
    "get_daily_series",
    "get_business_profile",
    "get_dish_daily",
    "compare_peers",
    "get_operational_changes",
)


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _parse_date(raw: Any, label: str) -> date:
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        raise ValidationError(f"{label} 必须是 YYYY-MM-DD 格式。每个工具都需要明确的日期。")


def _fits_budget(payload: Any) -> bool:
    """Whether a payload fits both the character and token budgets."""
    text = _dump(payload)
    return (
        len(text) <= MAX_TOOL_OUTPUT_CHARS - _META_CHAR_RESERVE
        and estimate_tokens(text) <= MAX_TOOL_OUTPUT_TOKENS - _META_TOKEN_RESERVE
    )


def _bounded_result(
    payload: Dict[str, Any],
    *,
    list_keys: Sequence[str] = (),
    omitted: Optional[Dict[str, int]] = None,
) -> str:
    """Serialise one tool result under a uniform budget, with metadata.

    Budget enforcement only removes trailing items from the named top-level
    lists, so what remains is still exact JSON: no item is cut in half and no
    scalar or evidence ref is rewritten. ``meta.omitted`` states how many items
    were dropped; callers add their own structural caps through ``omitted``.
    """
    payload = copy.deepcopy(payload)
    omitted = {
        key: int(value) for key, value in (omitted or {}).items() if int(value) > 0
    }
    budget_dropped = False
    for key in list_keys:
        items = payload.get(key)
        if not isinstance(items, list) or not items or _fits_budget(payload):
            continue
        lo, hi = 0, len(items)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            payload[key] = items[:mid]
            if _fits_budget(payload):
                lo = mid
            else:
                hi = mid - 1
        payload[key] = items[:lo]
        dropped = len(items) - lo
        if dropped:
            budget_dropped = True
            omitted[key] = omitted.get(key, 0) + dropped
    if omitted:
        payload["truncated"] = True
        if budget_dropped:
            notes = payload.get("notes")
            if isinstance(notes, list):
                notes.append("结果超过输出预算，部分内容已省略（见 meta.omitted）。")
    meta = {
        "chars": 0,
        "tokens": 0,
        "budget_chars": MAX_TOOL_OUTPUT_CHARS,
        "budget_tokens": MAX_TOOL_OUTPUT_TOKENS,
        "truncated": bool(omitted) or bool(payload.get("truncated")),
        "omitted": omitted,
    }
    payload["meta"] = meta
    for _ in range(3):  # digit counts in chars/tokens can shift the final size
        text = _dump(payload)
        chars = len(text)
        tokens = estimate_tokens(text)
        if (chars, tokens) == (meta["chars"], meta["tokens"]):
            break
        meta["chars"] = chars
        meta["tokens"] = tokens
    return _dump(payload)


def profile_evidence_records(
    payload: Dict[str, Any], refs: Dict[str, str]
) -> List[Dict[str, Any]]:
    """The six profile refs as ``{ref,label,value,detail}`` records.

    ``refs`` alone (name -> ref string) is enough for the step timeline and the
    model, but the thread registry is rebuilt by scraping ``ref``-bearing dicts
    out of tool messages. Emitting compact records here mirrors how
    ``get_daily_series`` attaches a ``ref`` to every daily point, so a follow-up
    turn can still resolve ``profile1.*`` citations. Values are the small
    scalars a later turn may quote, not copies of the full distributions.
    """
    summary = payload.get("summary") or {}
    reservations = payload.get("reservations") or {}
    baselines = payload.get("baselines") or {}
    return [
        {
            "ref": refs["order_types"],
            "label": "订单类型结构",
            "value": [
                {"order_type": row.get("order_type"), "order_count": row.get("order_count")}
                for row in payload.get("order_types") or []
            ],
            "detail": f"成交 {summary.get('order_count')} 单，销售额 {summary.get('revenue')}",
        },
        {
            "ref": refs["weekday_distribution"],
            "label": "星期分布",
            "value": [
                row.get("order_count")
                for row in payload.get("weekday_distribution") or []
            ],
            "detail": f"按 {payload.get('timezone') or ''} 划分，周一至周日",
        },
        {
            "ref": refs["hour_distribution"],
            "label": "时段分布",
            "value": [
                row.get("order_count")
                for row in payload.get("hour_distribution") or []
            ],
            "detail": f"按 {payload.get('timezone') or ''} 划分，0 至 23 点",
        },
        {
            "ref": refs["reservations"],
            "label": "预订健康度",
            "value": {
                key: reservations.get(key)
                for key in (
                    "booked_count",
                    "cancelled_count",
                    "cancellation_rate_pct",
                    "arrived_count",
                    "due_count",
                    "no_show_count",
                    "no_show_rate_pct",
                    "no_show_basis",
                )
            },
            "detail": (
                f"预订 {reservations.get('booked_count')} 条，取消 "
                f"{reservations.get('cancelled_count')} 条，疑似未到店 "
                f"{reservations.get('no_show_count')} 条"
            ),
        },
        *[
            {
                "ref": refs[key],
                "label": baseline.get("label") or key,
                "value": {
                    field: baseline.get(field)
                    for field in (
                        "start_date",
                        "end_date",
                        "order_count",
                        "revenue",
                        "status",
                        "order_count_change_pct",
                        "revenue_change_pct",
                    )
                },
                "detail": "订单量与销售额的变化比例",
            }
            for key, baseline in (
                ("last_week_same_weekday", baselines.get("last_week_same_weekday") or {}),
                ("last_year_same_period", baselines.get("last_year_same_period") or {}),
            )
        ],
    ]


def _daily_summary(daily: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Exact per-dish aggregates over the full series, without the series."""
    points = list(daily)
    measured = [
        (point, point.get("quantity"))
        for point in points
        if isinstance(point.get("quantity"), (int, float))
    ]
    zeros = [point for point, quantity in measured if quantity == 0]
    peak = max(measured, key=lambda item: item[1], default=None)
    trough = min(measured, key=lambda item: item[1], default=None)

    def edge(entry: Optional[Any]) -> Optional[Dict[str, Any]]:
        if entry is None:
            return None
        point, quantity = entry
        return {"date": str(point.get("date")), "quantity": quantity}

    return {
        "days": len(points),
        "zero_sales_days": len(zeros),
        "zero_sales_dates": [
            str(point.get("date")) for point in zeros[:MAX_NOTABLE_DAYS]
        ],
        "peak": edge(peak),
        "trough": edge(trough),
    }


def _fit_dish_daily(payload: Dict[str, Any]) -> Dict[str, int]:
    """Shrink returned per-dish series to a common length until the budget fits.

    Used only when ``include_daily=True``. Every dish keeps the same prefix
    length so one verbose dish cannot crowd the others out.
    """
    dishes = [
        dish
        for dish in payload.get("dishes", [])
        if isinstance(dish.get("daily"), list) and dish["daily"]
    ]
    if not dishes or _fits_budget(payload):
        return {}
    original = {id(dish): list(dish["daily"]) for dish in dishes}
    max_days = max(len(days) for days in original.values())

    def apply(cap: int) -> None:
        for dish in dishes:
            dish["daily"] = original[id(dish)][:cap]

    def probe(cap: int) -> bool:
        """Measure the payload at ``cap`` days *including* truncation flags."""
        apply(cap)
        flagged = []
        for dish in dishes:
            if len(original[id(dish)]) > cap:
                dish["daily_truncated"] = True
                flagged.append(dish)
        fits = _fits_budget(payload)
        for dish in flagged:
            dish.pop("daily_truncated", None)
        return fits

    lo, hi = 0, max_days
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if probe(mid):
            lo = mid
        else:
            hi = mid - 1
    apply(lo)

    omitted: Dict[str, int] = {}
    for dish in dishes:
        dropped = len(original[id(dish)]) - lo
        if dropped:
            dish["daily_truncated"] = True
            omitted["daily"] = omitted.get("daily", 0) + dropped
    return omitted


@dataclass
class DiagnosisToolContext:
    """Per-turn request state shared by all tools.

    ``lock`` serialises Service calls: the tools may be invoked concurrently by
    the graph, and an ``AsyncSession`` is not safe for concurrent use. It also
    keeps the trace and evidence lists in call-completion order.
    """

    staff: StaffUser
    session: AsyncSession
    branch_id: int
    requested_start: Optional[date] = None
    requested_end: Optional[date] = None
    # First/last day with any order in this branch (``StatsService.
    # get_data_coverage``). Lets relative phrases resolve against the data that
    # exists instead of an empty "today" on a historical dataset.
    coverage: Optional[Dict[str, Any]] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    observations: List[Any] = field(default_factory=list)
    resolved_window: Optional[Dict[str, Any]] = None
    diagnosis: Optional[Dict[str, Any]] = None
    # Observability only: the guarded tools append timing/output stats here.
    # It never influences tool results or the response contract.
    trace: RunTrace = field(default_factory=RunTrace)
    _sequence: int = 0

    def namespace(self, prefix: str) -> str:
        """A fresh per-call prefix, e.g. ``daily2.``, for evidence refs."""
        self._sequence += 1
        return f"{prefix}{self._sequence}."

    def add_evidence(
        self, ref: str, label: str, value: Any = None, detail: str = ""
    ) -> str:
        self.evidence.append(
            {"ref": ref, "label": label, "value": value, "detail": detail}
        )
        return ref

    def observe(self, payload: Any) -> None:
        """Keep the raw tool result so the number whitelist can be built from it."""
        self.observations.append(payload)

    def record_step(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        status: str = "ok",
        summary: str = "",
        evidence_refs: Optional[List[str]] = None,
        window: Optional[Dict[str, Any]] = None,
        expected_window: Optional[Dict[str, Any]] = None,
    ) -> None:
        step = {
            "index": len(self.steps) + 1,
            "tool": tool_name,
            "arguments": arguments,
            "status": status,
            "summary": summary,
            "evidence_refs": list(evidence_refs or []),
        }
        if window is not None:
            step["window"] = window
        if expected_window is not None:
            step["expected_window"] = expected_window
        self.steps.append(step)


def _notable_days(
    context: DiagnosisToolContext,
    namespace: str,
    item_id: int,
    daily: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return the few days that explain a dish's movement, each with a ref.

    Zero-sales days come first (they are the usual collapse pattern), then the
    peak and trough. Every returned point is an exact row from the series.
    """
    measured = [
        (index, point)
        for index, point in enumerate(daily, 1)
        if isinstance(point.get("quantity"), (int, float))
    ]
    if not measured:
        return []

    selected: Dict[int, str] = {}
    zero_limit = max(1, MAX_NOTABLE_DAYS - 2)
    for index, point in measured:
        if point["quantity"] != 0:
            continue
        if len(selected) >= zero_limit:
            break
        selected[index] = "当天销量为 0"
    peak = max(measured, key=lambda item: item[1]["quantity"])
    trough = min(measured, key=lambda item: item[1]["quantity"])
    if len(selected) < MAX_NOTABLE_DAYS:
        selected.setdefault(peak[0], "窗口内最高单日销量")
    if len(selected) < MAX_NOTABLE_DAYS:
        selected.setdefault(trough[0], "窗口内最低单日销量")

    notable: List[Dict[str, Any]] = []
    for index in sorted(selected):
        point = daily[index - 1]
        ref = context.add_evidence(
            f"{namespace}{item_id}.d{index}",
            f"菜品 #{item_id} {point['date']} 销量",
            point["quantity"],
            str(point.get("day_type", "")),
        )
        notable.append(
            {
                "date": str(point["date"]),
                "quantity": point["quantity"],
                "day_type": point.get("day_type"),
                "reason": selected[index],
                "ref": ref,
            }
        )
    return notable


def _window_fields(window: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """The ISO start/end of a window, for the step trace."""
    if not window:
        return None
    start = window.get("start_date")
    end = window.get("end_date")
    if start is None or end is None:
        return None
    return {"start_date": str(start), "end_date": str(end)}


async def _check_window(
    context: DiagnosisToolContext,
    tool_name: str,
    arguments: Dict[str, Any],
    start: date,
    end: date,
) -> Optional[str]:
    """Reject a date tool whose window contradicts ``resolved_window``.

    The first date tool of a turn that never called ``resolve_time_range``
    adopts its own window (``source="agent"``), so the response still reports
    one window and every later tool must agree with it. Once a window exists,
    a different one is a correctable error: the tool does not silently query a
    second range, because the answer and its baseline would then mix windows.

    The context lock serialises the read/adopt step: parallel tool calls from
    one model message must not each adopt a different window.
    """
    async with context.lock:
        resolved = context.resolved_window
        if resolved is None:
            today = datetime.now(timezone.utc).date()
            adopted = resolve_explicit_range(start, end, today=today, source="agent")
            context.resolved_window = annotate_coverage(
                adopted, context.coverage, today=today
            )
            return None

        expected_start = resolved.get("start_date")
        expected_end = resolved.get("end_date")
        if (start, end) == (expected_start, expected_end):
            return None

    requested = {"start_date": str(start), "end_date": str(end)}
    expected = {
        "start_date": str(expected_start),
        "end_date": str(expected_end),
    }
    message = (
        f"窗口不一致：{tool_name} 请求 {start} 至 {end}，"
        f"但本轮已解析的窗口是 {expected_start} 至 {expected_end}。"
        "请改用已解析窗口的日期重试。"
    )
    context.record_step(
        tool_name,
        arguments,
        status="error",
        summary=(
            f"窗口不一致：请求 {start} 至 {end}，"
            f"已解析窗口 {expected_start} 至 {expected_end}。"
        ),
        window=requested,
        expected_window=expected,
    )
    return _bounded_result(
        {
            "status": "error",
            "reason": "window_mismatch",
            "message": message,
            "requested_window": requested,
            "resolved_window": expected,
        }
    )


def _fail(
    context: DiagnosisToolContext,
    tool_name: str,
    arguments: Dict[str, Any],
    error: Exception,
) -> str:
    message = getattr(error, "message", None) or str(error)
    context.record_step(
        tool_name, arguments, status="error", summary=f"工具调用失败：{message}"
    )
    return _bounded_result({"status": "error", "message": message})


def resolve_investigation_window(
    context: DiagnosisToolContext, expression: Optional[str] = None
) -> Dict[str, Any]:
    """Resolve one time expression against this request's data coverage.

    Shared by the ``resolve_time_range`` tool and the deterministic fallback so
    both choose the same window. Explicit request dates win. Explicit dates in
    the question are honoured exactly. For relative phrases, when the newest
    data is older than the default recent window, the reference date is the
    last covered day (``source="coverage"``), and every result states the
    observed data range in ``notes``.
    """
    expression = (expression or "").strip()
    today = datetime.now(timezone.utc).date()
    coverage = context.coverage or {}
    data_end = coverage.get("end_date")
    explicit_request = bool(context.requested_start and context.requested_end)

    anchored = (
        not explicit_request
        and not expression_uses_explicit_dates(expression)
        and coverage_anchor(data_end, today=today) != today
    )

    if explicit_request:
        window = resolve_explicit_range(context.requested_start, context.requested_end)
        if expression:
            window["notes"].append(
                "请求已指定日期区间；该区间优先于问题中的时间表达。"
            )
    else:
        window = resolve_window_expression(
            expression,
            today=data_end if anchored else None,
            source="question" if expression else "default",
        )

    return annotate_coverage(window, coverage, anchored=anchored, today=today)


def build_diagnosis_tools(context: DiagnosisToolContext) -> List[BaseTool]:
    """Bind the read-only tools to one staff member, branch and session."""

    @tool
    async def resolve_time_range(expression: str = "") -> str:
        """把问题里的时间表达（如“上周”“最近7天”“上个月”“2026-10-01到2026-10-07”）解析成询问窗口。

        返回 start_date / end_date 与相邻等长基线 baseline_start_date / baseline_end_date，
        以及 source（question/default/explicit/coverage）、partial 和 notes。每个具体问题只调用一次，
        没有明确时间表达时传空字符串即可。相对时间会按数据库可用数据范围解析：若最近可用数据
        已超过默认窗口（历史数据集），会自动锚定到最近有数据的日期并在 notes 中说明。
        解析结果用于后续工具的日期参数；回答时必须告知用户实际使用的区间。
        """
        tool_name = "resolve_time_range"
        arguments = {"expression": expression}
        try:
            async with context.lock:
                resolved = resolve_investigation_window(context, expression)
                previous = context.resolved_window
                if previous and (
                    previous.get("start_date"),
                    previous.get("end_date"),
                ) != (resolved["start_date"], resolved["end_date"]):
                    resolved["notes"].append(
                        f"本轮此前的工具窗口为 {previous.get('start_date')} 至 "
                        f"{previous.get('end_date')}，现已按本次解析更新。"
                    )
                context.resolved_window = resolved
            context.observe(resolved)
            context.record_step(
                tool_name,
                arguments,
                summary=(
                    f"{resolved['start_date']} 至 {resolved['end_date']}"
                    f"（来源 {resolved['source']}，基线 "
                    f"{resolved['baseline_start_date']} 至 {resolved['baseline_end_date']}）"
                ),
                window=_window_fields(resolved),
            )
            return _bounded_result(resolved)
        except Exception as error:  # trace it, never abort the whole turn
            logger.warning("%s failed: %s", tool_name, error)
            return _fail(context, tool_name, arguments, error)

    @tool
    async def get_diagnosis(start_date: str, end_date: str) -> str:
        """运行确定性经营诊断：本期与相邻等长上期的订单量、营业额、客单价、取消率与单品异常。

        返回 status（ok / insufficient_data）、两份周期的汇总、findings 及带 ref 的证据。
        门店由当前登录员工决定，不需要也不能传入 branch_id。日期为闭区间（含首尾）。
        """
        tool_name = "get_diagnosis"
        arguments = {"start_date": start_date, "end_date": end_date}
        try:
            start = _parse_date(start_date, "start_date")
            end = _parse_date(end_date, "end_date")
            mismatch = await _check_window(
                context, tool_name, arguments, start, end
            )
            if mismatch is not None:
                return mismatch
            async with context.lock:
                payload = await DiagnosisService.diagnose(
                    context.branch_id,
                    start,
                    end,
                    staff=context.staff,
                    session=context.session,
                )
            findings = payload.get("findings", [])
            notes = payload.get("notes", [])
            omitted = {
                "findings": max(0, len(findings) - MAX_DIAGNOSIS_FINDINGS),
                "notes": max(0, len(notes) - MAX_NOTES),
            }
            payload["findings"] = findings[:MAX_DIAGNOSIS_FINDINGS]
            payload["notes"] = notes[:MAX_NOTES]
            context.diagnosis = payload
            context.observe(payload)
            refs = [
                item["ref"]
                for finding in payload["findings"]
                for item in finding.get("evidence", [])
            ]
            context.record_step(
                tool_name,
                arguments,
                summary=(
                    f"{payload['current_period']['start_date']} 至 "
                    f"{payload['current_period']['end_date']}："
                    f"{payload['status']}，{len(payload['findings'])} 条异常"
                ),
                evidence_refs=refs,
                window=_window_fields({"start_date": start, "end_date": end}),
            )
            return _bounded_result(
                payload,
                list_keys=("findings", "notes"),
                omitted=omitted,
            )
        except Exception as error:  # trace it, never abort the whole turn
            logger.warning("%s failed: %s", tool_name, error)
            return _fail(context, tool_name, arguments, error)

    @tool
    async def get_daily_series(start_date: str, end_date: str) -> str:
        """读取某窗口的逐日营业额、订单量和取消数，用于判断变化集中在哪一天。

        返回 daily 列表（每点带 ref）与窗口汇总。日期为闭区间；建议窗口不超过 62 天。
        """
        tool_name = "get_daily_series"
        arguments = {"start_date": start_date, "end_date": end_date}
        try:
            start = _parse_date(start_date, "start_date")
            end = _parse_date(end_date, "end_date")
            mismatch = await _check_window(
                context, tool_name, arguments, start, end
            )
            if mismatch is not None:
                return mismatch
            async with context.lock:
                summary = await StatsService.get_branch_summary(
                    context.branch_id,
                    start,
                    end,
                    top_limit=1,
                    staff=context.staff,
                    session=context.session,
                )
            namespace = context.namespace("daily")
            daily = []
            for index, point in enumerate(summary["daily"][:MAX_SERIES_POINTS], 1):
                ref = context.add_evidence(
                    f"{namespace}e{index}",
                    f"{point['date']} 营业额",
                    point["revenue"],
                    f"订单 {point['order_count']} 单，取消 {point['cancelled_count']} 单",
                )
                daily.append({**point, "ref": ref})
            totals_ref = context.add_evidence(
                f"{namespace}t1",
                f"{summary['start_date']} 至 {summary['end_date']} 营业额合计",
                summary["revenue"],
                f"订单 {summary['order_count']} 单，取消 {summary['cancelled_count']} 单，"
                f"客单价 {summary['average_order_value']}",
            )
            truncated = len(summary["daily"]) > MAX_SERIES_POINTS
            refs = [point["ref"] for point in daily] + [totals_ref]
            context.record_step(
                tool_name,
                arguments,
                summary=(
                    f"逐日数据 {len(daily)} 天，营业额合计 {summary['revenue']}"
                    + ("（超出部分未返回）" if truncated else "")
                ),
                evidence_refs=refs,
                window=_window_fields({"start_date": start, "end_date": end}),
            )
            result = {
                "branch_id": summary["branch_id"],
                "branch_name": summary["branch_name"],
                "start_date": summary["start_date"],
                "end_date": summary["end_date"],
                "order_count": summary["order_count"],
                "cancelled_count": summary["cancelled_count"],
                "revenue": summary["revenue"],
                "average_order_value": summary["average_order_value"],
                "units_sold": summary["units_sold"],
                "daily": daily,
                "truncated": truncated,
                "notes": (
                    [f"窗口超过 {MAX_SERIES_POINTS} 天，仅返回前 {MAX_SERIES_POINTS} 天。"]
                    if truncated
                    else []
                ),
            }
            context.observe(result)
            return _bounded_result(
                result,
                list_keys=("daily",),
                omitted={
                    "daily": max(0, len(summary["daily"]) - MAX_SERIES_POINTS),
                },
            )
        except Exception as error:  # trace it, never abort the whole turn
            logger.warning("%s failed: %s", tool_name, error)
            return _fail(context, tool_name, arguments, error)

    @tool
    async def get_business_profile(start_date: str, end_date: str) -> str:
        """查询窗口内的经营结构画像：预订健康度（预订/取消/疑似未到店）、时段与星期分布、订单类型结构，以及上周同星期/去年同期基线。

        返回 summary、order_types、weekday_distribution、hour_distribution、reservations、
        baselines 与 notes；各维度的 evidence_ref 见 refs 字段。时段与星期按门店业务时区划分，
        统计窗口仍为日期闭区间。未到店是疑似值：预订时间已过但状态仍为待确认/已确认。
        用于回答“预订是否异常”“集中在什么时段/星期”“堂食外卖结构”“与上周同日或去年同期比”等问题。
        """
        tool_name = "get_business_profile"
        arguments = {"start_date": start_date, "end_date": end_date}
        try:
            start = _parse_date(start_date, "start_date")
            end = _parse_date(end_date, "end_date")
            mismatch = await _check_window(
                context, tool_name, arguments, start, end
            )
            if mismatch is not None:
                return mismatch
            async with context.lock:
                payload = await BusinessProfileService.get_business_profile(
                    context.branch_id,
                    start,
                    end,
                    staff=context.staff,
                    session=context.session,
                )
            namespace = context.namespace("profile")
            summary = payload["summary"]
            reservations = payload["reservations"]
            baselines = payload["baselines"]
            refs = {
                "order_types": context.add_evidence(
                    f"{namespace}t1",
                    "订单类型结构",
                    payload["order_types"],
                    f"窗口成交 {summary['order_count']} 单，"
                    f"销售额 {summary['revenue']}",
                ),
                "weekday_distribution": context.add_evidence(
                    f"{namespace}w1",
                    "星期分布",
                    payload["weekday_distribution"],
                    f"按 {payload['timezone']} 划分",
                ),
                "hour_distribution": context.add_evidence(
                    f"{namespace}h1",
                    "时段分布",
                    payload["hour_distribution"],
                    f"按 {payload['timezone']} 划分",
                ),
                "reservations": context.add_evidence(
                    f"{namespace}r1",
                    "预订健康度",
                    reservations,
                    f"预订 {reservations['booked_count']} 条，"
                    f"取消 {reservations['cancelled_count']} 条，"
                    f"疑似未到店 {reservations['no_show_count']} 条",
                ),
                "last_week_same_weekday": context.add_evidence(
                    f"{namespace}b1",
                    baselines["last_week_same_weekday"]["label"],
                    baselines["last_week_same_weekday"],
                    "订单量与销售额的变化比例",
                ),
                "last_year_same_period": context.add_evidence(
                    f"{namespace}b2",
                    baselines["last_year_same_period"]["label"],
                    baselines["last_year_same_period"],
                    "订单量与销售额的变化比例",
                ),
            }
            result = dict(payload)
            result["refs"] = refs
            result["evidence"] = profile_evidence_records(result, refs)
            context.record_step(
                tool_name,
                arguments,
                summary=(
                    f"订单 {summary['order_count']} 单；预订 "
                    f"{reservations['booked_count']} 条（取消 "
                    f"{reservations['cancelled_count']}，疑似未到店 "
                    f"{reservations['no_show_count']}）"
                ),
                evidence_refs=list(refs.values()),
                window=_window_fields({"start_date": start, "end_date": end}),
            )
            context.observe(result)
            return _bounded_result(
                result,
                list_keys=(
                    "order_types",
                    "weekday_distribution",
                    "hour_distribution",
                ),
            )
        except Exception as error:  # trace it, never abort the whole turn
            logger.warning("%s failed: %s", tool_name, error)
            return _fail(context, tool_name, arguments, error)

    @tool
    async def get_dish_daily(
        menu_item_ids: List[int],
        start_date: str,
        end_date: str,
        include_daily: bool = False,
    ) -> str:
        """查询指定菜品在本期与上期的销量走势（汇总、变化形态与异常日）。

        menu_item_ids 来自诊断 findings 里的 menu_item_id，最多 10 个。
        默认只返回每道菜的汇总（current_period / baseline_period）、shape 和
        notable_days（零销量日、最高/最低单日销量，均带 evidence_ref），不返回完整逐日序列。
        需要完整逐日序列时传 include_daily=true；逐日明细会受输出预算限制，必要时请缩小菜品数量。
        """
        tool_name = "get_dish_daily"
        arguments = {
            "menu_item_ids": menu_item_ids,
            "start_date": start_date,
            "end_date": end_date,
            "include_daily": include_daily,
        }
        try:
            start = _parse_date(start_date, "start_date")
            end = _parse_date(end_date, "end_date")
            mismatch = await _check_window(
                context, tool_name, arguments, start, end
            )
            if mismatch is not None:
                return mismatch
            async with context.lock:
                payload = await DishDailyService.get_dish_daily(
                    context.branch_id,
                    menu_item_ids,
                    start,
                    end,
                    staff=context.staff,
                    session=context.session,
                )
            namespace = context.namespace("dish")
            refs: List[str] = []
            omitted: Dict[str, int] = {}
            source_dishes = list(payload.get("dishes", []))
            if len(source_dishes) > MAX_DISHES:
                omitted["dishes"] = len(source_dishes) - MAX_DISHES
            dishes: List[Dict[str, Any]] = []
            for dish in source_dishes[:MAX_DISHES]:
                item_id = dish["menu_item_id"]
                refs.extend(
                    [
                        context.add_evidence(
                            f"{namespace}{item_id}.e1",
                            f"菜品 #{item_id} 本期销量",
                            dish["current_period"]["quantity"],
                            str(dish["current_period"]["start_date"]),
                        ),
                        context.add_evidence(
                            f"{namespace}{item_id}.e2",
                            f"菜品 #{item_id} 上期销量",
                            dish["baseline_period"]["quantity"],
                            str(dish["baseline_period"]["start_date"]),
                        ),
                    ]
                )
                if dish["change_pct"] is not None:
                    refs.append(
                        context.add_evidence(
                            f"{namespace}{item_id}.e3",
                            f"菜品 #{item_id} 销量变化幅度",
                            dish["change_pct"],
                            "百分比",
                        )
                    )
                daily = list(dish.get("daily") or [])
                entry: Dict[str, Any] = {
                    "menu_item_id": item_id,
                    "status": dish.get("status"),
                    "direction": dish.get("direction"),
                    "change_pct": dish.get("change_pct"),
                    "availability": dish.get("availability"),
                    "current_period": dish.get("current_period"),
                    "baseline_period": dish.get("baseline_period"),
                    "shape": dish.get("shape"),
                    "daily_summary": _daily_summary(daily),
                }
                if include_daily:
                    daily_out = [dict(point) for point in daily[:MAX_SERIES_POINTS]]
                    for day_index, point in enumerate(daily_out, 1):
                        point["ref"] = context.add_evidence(
                            f"{namespace}{item_id}.d{day_index}",
                            f"菜品 #{item_id} {point['date']} 销量",
                            point["quantity"],
                            point["day_type"],
                        )
                        refs.append(point["ref"])
                    if len(daily) > MAX_SERIES_POINTS:
                        entry["daily_truncated"] = True
                        omitted["daily"] = omitted.get("daily", 0) + (
                            len(daily) - MAX_SERIES_POINTS
                        )
                    entry["daily"] = daily_out
                else:
                    notable = _notable_days(context, namespace, item_id, daily)
                    refs.extend(item["ref"] for item in notable)
                    entry["notable_days"] = notable
                dishes.append(entry)

            notes = list(payload.get("notes") or [])[:MAX_NOTES]
            if not include_daily:
                notes.append(
                    "默认只返回汇总与异常日；需要某个菜品的完整逐日序列时，"
                    "传 include_daily=true 重查。"
                )
            result: Dict[str, Any] = {
                key: payload[key]
                for key in (
                    "branch_id",
                    "start_date",
                    "end_date",
                    "baseline_start_date",
                    "baseline_end_date",
                )
                if key in payload
            }
            result["dishes"] = dishes
            result["notes"] = notes
            context.record_step(
                tool_name,
                arguments,
                summary=(
                    f"查询 {len(dishes)} 个菜品的"
                    + ("逐日销量" if include_daily else "销量汇总与异常日")
                ),
                evidence_refs=refs,
                window=_window_fields({"start_date": start, "end_date": end}),
            )
            # Observe the full payload: even budget-omitted rows are real tool
            # observations, and the number whitelist must not depend on size.
            context.observe(result)
            if include_daily:
                nested_omitted = _fit_dish_daily(result)
                for key, value in nested_omitted.items():
                    omitted[key] = omitted.get(key, 0) + value
            return _bounded_result(
                result,
                list_keys=("dishes",),
                omitted=omitted,
            )
        except Exception as error:  # trace it, never abort the whole turn
            logger.warning("%s failed: %s", tool_name, error)
            return _fail(context, tool_name, arguments, error)

    @tool
    async def compare_peers(start_date: str, end_date: str) -> str:
        """把本店与可见的其他门店在同一窗口做可比性对比（目标店包含在计数内）。

        用于判断“本店下降是否只是全店共性问题”。返回各店 status、change_pct、verdict。
        门店员工只能看到本店，会得到 not_comparable；这不是错误。
        """
        tool_name = "compare_peers"
        arguments = {"start_date": start_date, "end_date": end_date}
        try:
            start = _parse_date(start_date, "start_date")
            end = _parse_date(end_date, "end_date")
            mismatch = await _check_window(
                context, tool_name, arguments, start, end
            )
            if mismatch is not None:
                return mismatch
            async with context.lock:
                payload = await BranchPeerService.compare_branch_peers(
                    context.branch_id,
                    start,
                    end,
                    staff=context.staff,
                    session=context.session,
                )
            namespace = context.namespace("peers")
            refs: List[str] = []
            all_peers = list(payload.get("peers", []))
            payload["peers"] = all_peers[:MAX_PEER_ROWS]
            for index, peer in enumerate(payload["peers"], 1):
                peer["ref"] = context.add_evidence(
                    f"{namespace}e{index}",
                    f"{peer['branch_name']} 营业额变化",
                    peer.get("change_pct"),
                    peer["verdict"],
                )
                refs.append(peer["ref"])
            summary_ref = context.add_evidence(
                f"{namespace}s1",
                "同业对比结论",
                payload["comparable_count"],
                payload["summary"],
            )
            refs.append(summary_ref)
            context.record_step(
                tool_name,
                arguments,
                summary=payload["summary"],
                evidence_refs=refs,
                window=_window_fields({"start_date": start, "end_date": end}),
            )
            context.observe(payload)
            return _bounded_result(
                payload,
                list_keys=("peers",),
                omitted={"peers": max(0, len(all_peers) - MAX_PEER_ROWS)},
            )
        except Exception as error:  # trace it, never abort the whole turn
            logger.warning("%s failed: %s", tool_name, error)
            return _fail(context, tool_name, arguments, error)

    @tool
    async def get_operational_changes(start_date: str, end_date: str) -> str:
        """查询窗口内可能影响经营的运营变更：菜品修改/新建、库存调整、预订改约或改派餐桌、门店与餐桌调整。

        返回 changes 列表（每条带 ref、操作人、时间和变更详情）。用于验证“是不是运营动作导致”的假设；
        订单状态变化不在本工具范围内。日期为闭区间。
        """
        tool_name = "get_operational_changes"
        arguments = {"start_date": start_date, "end_date": end_date}
        try:
            start = _parse_date(start_date, "start_date")
            end = _parse_date(end_date, "end_date")
            mismatch = await _check_window(
                context, tool_name, arguments, start, end
            )
            if mismatch is not None:
                return mismatch
            start_at = datetime.combine(start, time.min, tzinfo=timezone.utc)
            end_at = datetime.combine(
                end + timedelta(days=1), time.min, tzinfo=timezone.utc
            )
            async with context.lock:
                logs = await AuditService.list_logs(
                    context.staff,
                    branch_id=context.branch_id,
                    actions=list(OPERATIONAL_ACTIONS),
                    start=start_at,
                    end=end_at,
                    limit=MAX_OPERATIONAL_CHANGES + 1,
                    session=context.session,
                )
            truncated = len(logs) > MAX_OPERATIONAL_CHANGES
            namespace = context.namespace("ops")
            changes = []
            refs: List[str] = []
            for index, log in enumerate(logs[:MAX_OPERATIONAL_CHANGES], 1):
                item = audit_log_payload(log)
                item["ref"] = context.add_evidence(
                    f"{namespace}e{index}",
                    f"{item['action_label']}（{item['entity_label']}"
                    f"#{item['entity_id']}）",
                    item["entity_id"],
                    f"{item['actor_name'] or '系统'} @ {item['created_at']}",
                )
                refs.append(item["ref"])
                changes.append(item)
            context.record_step(
                tool_name,
                arguments,
                summary=(
                    f"窗口内运营变更 {len(changes)} 条"
                    + ("（超出部分未返回）" if truncated else "")
                ),
                evidence_refs=refs,
                window=_window_fields({"start_date": start, "end_date": end}),
            )
            result = {
                "start_date": start,
                "end_date": end,
                "changes": changes,
                "truncated": truncated,
                "notes": (
                    [f"匹配记录超过 {MAX_OPERATIONAL_CHANGES} 条，仅返回最近的部分。"]
                    if truncated
                    else []
                ),
            }
            context.observe(result)
            return _bounded_result(
                result,
                list_keys=("changes",),
                omitted={
                    "changes": max(0, len(logs) - MAX_OPERATIONAL_CHANGES),
                },
            )
        except Exception as error:  # trace it, never abort the whole turn
            logger.warning("%s failed: %s", tool_name, error)
            return _fail(context, tool_name, arguments, error)

    return [
        resolve_time_range,
        get_diagnosis,
        get_daily_series,
        get_business_profile,
        get_dish_daily,
        compare_peers,
        get_operational_changes,
    ]


__all__ = [
    "DiagnosisToolContext",
    "MAX_DIAGNOSIS_FINDINGS",
    "MAX_DISHES",
    "MAX_NOTABLE_DAYS",
    "MAX_NOTES",
    "MAX_OPERATIONAL_CHANGES",
    "MAX_PEER_ROWS",
    "MAX_SERIES_POINTS",
    "MAX_TOOL_OUTPUT_CHARS",
    "MAX_TOOL_OUTPUT_TOKENS",
    "OPERATIONAL_ACTIONS",
    "TOOL_NAMES",
    "build_diagnosis_tools",
]
