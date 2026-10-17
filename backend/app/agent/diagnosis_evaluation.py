"""Offline evaluation harness for the diagnosis investigation Agent (Task60).

The harness has three jobs:

* a fixed, versioned dataset of questions and scripted tool trajectories
  (``diagnosis_eval_dataset.json``);
* a scripted chat model plus fixture tools that run through the real
  ``run_harness_turn`` graph, so tool guarding, budgets, timeouts and context
  management behave exactly as they do in production;
* scoring through the production ``_compose_answer_and_report`` guardrails,
  which is what makes "citation valid" and "number hallucinated" comparable
  with what a staff member would actually see.

Nothing here touches a database or a provider. A run is deterministic, needs no
credentials, and finishes in well under a second, which is what makes it usable
in CI. The optional live smoke against a real model stays in
``scripts/smoke_diagnosis_investigation.py``.
"""

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import MemorySaver
from pydantic import Field

from backend.app.agent.diagnosis import numbers_in_text
from backend.app.agent.diagnosis_investigation import (
    DATE_TOOLS,
    _compose_answer_and_report,
)
from backend.app.agent.diagnosis_report import STRUCTURED_REPORT_TOOL_NAME
from backend.app.agent.diagnosis_tools import (
    DiagnosisToolContext,
    profile_evidence_records,
)
from backend.app.agent.harness import (
    DEFAULT_RECURSION_LIMIT,
    DEFAULT_TOOL_CALLS_LIMIT,
    HarnessConfig,
    RunTrace,
    estimate_tokens,
    merge_thread_evidence,
    message_text,
    run_harness_turn,
)

DEFAULT_DATASET_PATH = Path(__file__).with_name("diagnosis_eval_dataset.json")
DATE_TOOL_NAMES = frozenset(DATE_TOOLS)
_DATE_KEYS = frozenset(
    {
        "start_date",
        "end_date",
        "baseline_start_date",
        "baseline_end_date",
        "date",
    }
)
_NUMBER_TOLERANCE = 0.011


# --------------------------------------------------------------------------- #
# Dataset
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EvalDataset:
    """The fixed evaluation dataset, with fixture dates already parsed."""

    version: int
    description: str
    windows: Dict[str, Dict[str, Any]]
    diagnoses: Dict[str, Dict[str, Any]]
    daily: Dict[str, Any]
    dishes: Dict[str, Any]
    peers: Dict[str, Any]
    changes: Dict[str, Any]
    profile: Dict[str, Any]
    scenarios: List[Dict[str, Any]]
    sha256: str
    source_path: str


def _coerce_dates(value: Any, key: Optional[str] = None) -> Any:
    """Turn ISO date strings into ``date`` objects inside fixture payloads.

    The production tools return real dates, and the guardrail's calendar-number
    whitelist only sees date objects. Scenario steps are deliberately left as
    strings: they are tool arguments handed to LangChain, not observations.
    """
    if isinstance(value, dict):
        return {item: _coerce_dates(content, item) for item, content in value.items()}
    if isinstance(value, list):
        return [_coerce_dates(item, key) for item in value]
    if isinstance(value, str) and key in _DATE_KEYS:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return value
    return value


def _validate_windows(windows: Mapping[str, Mapping[str, Any]]) -> None:
    if not windows:
        raise ValueError("evaluation dataset has no windows.")
    for name, window in windows.items():
        try:
            start = window["start_date"]
            end = window["end_date"]
            baseline_start = window["baseline_start_date"]
            baseline_end = window["baseline_end_date"]
        except KeyError as error:
            raise ValueError(f"window {name!r} is missing {error.args[0]!r}.") from error
        if not all(
            isinstance(value, date)
            for value in (start, end, baseline_start, baseline_end)
        ):
            raise ValueError(f"window {name!r} has an invalid date.")
        if start > end:
            raise ValueError(f"window {name!r} starts after it ends.")
        if baseline_start > baseline_end:
            raise ValueError(f"window {name!r} baseline starts after it ends.")
        if (end - start).days != (baseline_end - baseline_start).days:
            raise ValueError(f"window {name!r} baseline is not equal-length.")
        if baseline_end >= start:
            raise ValueError(f"window {name!r} baseline is not adjacent.")


def _validate_scenarios(scenarios: List[Dict[str, Any]]) -> None:
    seen = set()
    for scenario in scenarios:
        scenario_id = str(scenario.get("id") or "")
        if not scenario_id or scenario_id in seen:
            raise ValueError(f"scenario id is missing or duplicated: {scenario_id!r}")
        seen.add(scenario_id)
        turns = scenario.get("turns") or []
        if not turns:
            raise ValueError(f"scenario {scenario_id!r} has no turns.")
        for position, turn in enumerate(turns, 1):
            if not turn.get("question"):
                raise ValueError(
                    f"scenario {scenario_id!r} turn {position} has no question."
                )
            steps = turn.get("steps") or []
            if not steps:
                raise ValueError(
                    f"scenario {scenario_id!r} turn {position} has no steps."
                )
            for step in steps:
                if ("tool" in step) == ("report" in step):
                    raise ValueError(
                        f"scenario {scenario_id!r} turn {position} step must have "
                        "exactly one of 'tool' or 'report'."
                    )
            if "report" not in steps[-1]:
                raise ValueError(
                    f"scenario {scenario_id!r} turn {position} must end with a report."
                )
            expected = (turn.get("expect") or {}).get("tools")
            if expected is not None and list(expected) != [
                step["tool"] for step in steps if "tool" in step
            ]:
                raise ValueError(
                    f"scenario {scenario_id!r} turn {position}: expect.tools does "
                    "not match the scripted tool steps."
                )


def load_dataset(path: Optional[Path] = None) -> EvalDataset:
    """Load and validate the fixed dataset; raises ``ValueError`` on drift."""
    source = Path(path or DEFAULT_DATASET_PATH)
    raw_bytes = source.read_bytes()
    raw = json.loads(raw_bytes.decode("utf-8"))
    dataset = EvalDataset(
        version=int(raw.get("version") or 0),
        description=str(raw.get("description") or ""),
        windows=_coerce_dates(raw.get("windows") or {}),
        diagnoses=_coerce_dates(raw.get("diagnoses") or {}),
        daily=_coerce_dates(raw.get("daily") or {}),
        dishes=_coerce_dates(raw.get("dishes") or {}),
        peers=_coerce_dates(raw.get("peers") or {}),
        changes=_coerce_dates(raw.get("changes") or {}),
        profile=_coerce_dates(raw.get("profile") or {}),
        scenarios=list(raw.get("scenarios") or []),
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        source_path=str(source),
    )
    _validate_windows(dataset.windows)
    _validate_scenarios(dataset.scenarios)
    return dataset


# --------------------------------------------------------------------------- #
# Scripted model
# --------------------------------------------------------------------------- #
class ScriptedTurnModel(BaseChatModel):
    """Emit one queued AIMessage per model call and record prompt tokens.

    The queued messages are the scenario's scripted trajectory: a tool call, a
    structured report, or a plain JSON answer. ``bind_tools`` is a no-op because
    the trajectory is fixed; everything else (the graph, guardrails, tool
    wrappers) is the production path.
    """

    responses: List[AIMessage]
    index: int = 0
    seen: List[Any] = Field(default_factory=list)
    prompt_tokens: int = 0
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-diagnosis-eval"

    def bind_tools(self, tools, **kwargs):
        return self

    def _next_message(self, messages) -> AIMessage:
        self.seen.append(list(messages))
        self.calls += 1
        self.prompt_tokens += _messages_tokens(messages)
        position = min(self.index, len(self.responses) - 1)
        self.index += 1
        template = self.responses[position]
        return AIMessage(
            content=template.content,
            tool_calls=[
                {**call, "id": f"{call.get('id', 'call')}-{self.index}"}
                for call in template.tool_calls
            ],
        )

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[ChatGeneration(message=self._next_message(messages))]
        )

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[ChatGeneration(message=self._next_message(messages))]
        )


def _messages_tokens(messages: Sequence[BaseMessage]) -> int:
    total = 0
    for message in messages:
        total += estimate_tokens(message_text(message))
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            total += estimate_tokens(tool_calls)
    return total


def _build_responses(turn: Mapping[str, Any]) -> List[AIMessage]:
    responses: List[AIMessage] = []
    for position, step in enumerate(turn.get("steps") or [], 1):
        if "tool" in step:
            responses.append(
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": str(step["tool"]),
                            "args": dict(step.get("args") or {}),
                            "id": f"eval-{position}",
                            "type": "tool_call",
                        }
                    ],
                )
            )
        else:
            responses.append(
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": STRUCTURED_REPORT_TOOL_NAME,
                            "args": dict(step["report"]),
                            "id": f"eval-report-{position}",
                            "type": "tool_call",
                        }
                    ],
                )
            )
    return responses


# --------------------------------------------------------------------------- #
# Fixture tools
# --------------------------------------------------------------------------- #
@dataclass
class EvalFixtures:
    """The fixed observations a scenario's tools may return."""

    windows: Dict[str, Dict[str, Any]]
    diagnoses: Dict[str, Dict[str, Any]]
    daily: Dict[str, Any]
    dishes: Dict[str, Any]
    peers: Dict[str, Any]
    changes: Dict[str, Any]
    profile: Dict[str, Any]
    faults: Dict[str, str] = field(default_factory=dict)

    def window(self, key: str) -> Dict[str, Any]:
        try:
            return self.windows[key]
        except KeyError as error:
            raise ValueError(f"unknown fixture window: {key!r}") from error

    def find_window(
        self, start: date, end: date
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        for key, window in self.windows.items():
            if (window["start_date"], window["end_date"]) == (start, end):
                return key, window
        return None

    def key_for_window(self, window: Mapping[str, Any]) -> str:
        pair = (window.get("start_date"), window.get("end_date"))
        for key, candidate in self.windows.items():
            if (candidate["start_date"], candidate["end_date"]) == pair:
                return key
        raise ValueError(f"resolved window is not part of the fixtures: {pair!r}")

    def diagnosis_for(self, key: str) -> Dict[str, Any]:
        try:
            return self.diagnoses[key]
        except KeyError as error:
            raise ValueError(f"no diagnosis fixture for window {key!r}") from error


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _window_fields(window: Mapping[str, Any]) -> Dict[str, str]:
    return {
        "start_date": str(window["start_date"]),
        "end_date": str(window["end_date"]),
    }


def build_fixture_tools(
    context: DiagnosisToolContext, fixtures: EvalFixtures
) -> List[BaseTool]:
    """The production tool names and shapes, backed by fixed payloads.

    Window mismatches are rejected the same way the production tools reject
    them, so a scenario can assert both the bad call and the recovered one.
    """

    async def _fault(tool_name: str) -> None:
        kind = fixtures.faults.get(tool_name)
        if kind == "timeout":
            await asyncio.sleep(1.0)
        elif kind == "raise":
            raise RuntimeError(f"fixture failure: {tool_name}")

    def _parse(raw: Any, label: str) -> date:
        return date.fromisoformat(str(raw))

    def _mismatch(
        tool_name: str,
        arguments: Mapping[str, Any],
        requested: Tuple[date, date],
        expected: Mapping[str, Any],
    ) -> str:
        requested_window = {
            "start_date": str(requested[0]),
            "end_date": str(requested[1]),
        }
        expected_window = _window_fields(expected)
        context.record_step(
            tool_name,
            dict(arguments),
            status="error",
            summary=(
                f"窗口不一致：请求 {requested[0]} 至 {requested[1]}，"
                f"已解析窗口 {expected_window['start_date']} 至 "
                f"{expected_window['end_date']}。"
            ),
            window=requested_window,
            expected_window=expected_window,
        )
        return _dump(
            {
                "status": "error",
                "reason": "window_mismatch",
                "message": "请改用已解析窗口的日期重试。",
                "requested_window": requested_window,
                "resolved_window": expected_window,
            }
        )

    def _check_window(
        tool_name: str,
        arguments: Mapping[str, Any],
        start: date,
        end: date,
    ) -> Optional[str]:
        resolved = context.resolved_window
        if resolved is None:
            found = fixtures.find_window(start, end)
            if found is not None:
                context.resolved_window = found[1]
                return None
            return _mismatch(
                tool_name, arguments, (start, end), fixtures.window("main")
            )
        if (start, end) == (resolved["start_date"], resolved["end_date"]):
            return None
        return _mismatch(tool_name, arguments, (start, end), resolved)

    @tool
    async def resolve_time_range(expression: str = "") -> str:
        """把问题里的时间表达解析成固定数据集中的询问窗口与基线。"""
        await _fault("resolve_time_range")
        key = "last_month" if "上个月" in (expression or "") else "main"
        window = fixtures.window(key)
        context.resolved_window = window
        context.observe(window)
        context.record_step(
            "resolve_time_range",
            {"expression": expression},
            summary=(
                f"{window['start_date']} 至 {window['end_date']}"
                f"（基线 {window['baseline_start_date']} 至 "
                f"{window['baseline_end_date']}）"
            ),
            window=_window_fields(window),
        )
        return _dump(window)

    @tool
    async def get_diagnosis(start_date: str, end_date: str) -> str:
        """运行固定数据集的确定性经营诊断并返回异常与证据。"""
        await _fault("get_diagnosis")
        start = _parse(start_date, "start_date")
        end = _parse(end_date, "end_date")
        mismatch = _check_window(
            "get_diagnosis",
            {"start_date": start_date, "end_date": end_date},
            start,
            end,
        )
        if mismatch is not None:
            return mismatch
        key = fixtures.key_for_window(context.resolved_window or {})
        payload = fixtures.diagnosis_for(key)
        context.diagnosis = payload
        context.observe(payload)
        refs = [
            item["ref"]
            for finding in payload.get("findings", [])
            for item in finding.get("evidence", [])
            if item.get("ref")
        ]
        context.record_step(
            "get_diagnosis",
            {"start_date": start_date, "end_date": end_date},
            summary=(
                f"{start} 至 {end}：{payload.get('status')}，"
                f"{len(payload.get('findings') or [])} 条异常"
            ),
            evidence_refs=refs,
            window={"start_date": str(start), "end_date": str(end)},
        )
        return _dump(payload)

    @tool
    async def get_daily_series(start_date: str, end_date: str) -> str:
        """读取固定数据集的逐日营业额、订单量和取消数。"""
        await _fault("get_daily_series")
        start = _parse(start_date, "start_date")
        end = _parse(end_date, "end_date")
        mismatch = _check_window(
            "get_daily_series",
            {"start_date": start_date, "end_date": end_date},
            start,
            end,
        )
        if mismatch is not None:
            return mismatch
        payload = fixtures.daily
        refs: List[str] = []
        for point in payload.get("daily") or []:
            refs.append(
                context.add_evidence(
                    point["ref"],
                    f"{point['date']} 营业额",
                    point["revenue"],
                    f"订单 {point['order_count']} 单，"
                    f"取消 {point['cancelled_count']} 单",
                )
            )
        refs.append(
            context.add_evidence(
                "daily1.t1",
                f"{start} 至 {end} 营业额合计",
                payload.get("revenue"),
                f"订单 {payload.get('order_count')} 单",
            )
        )
        context.observe(payload)
        context.record_step(
            "get_daily_series",
            {"start_date": start_date, "end_date": end_date},
            summary=f"逐日数据 {len(refs) - 1} 天",
            evidence_refs=refs,
            window={"start_date": str(start), "end_date": str(end)},
        )
        return _dump(payload)

    @tool
    async def get_dish_daily(
        menu_item_ids: List[int],
        start_date: str,
        end_date: str,
        include_daily: bool = False,
    ) -> str:
        """查询固定数据集中指定菜品的本期/上期销量与异常日。"""
        await _fault("get_dish_daily")
        start = _parse(start_date, "start_date")
        end = _parse(end_date, "end_date")
        mismatch = _check_window(
            "get_dish_daily",
            {
                "menu_item_ids": menu_item_ids,
                "start_date": start_date,
                "end_date": end_date,
                "include_daily": include_daily,
            },
            start,
            end,
        )
        if mismatch is not None:
            return mismatch
        payload = fixtures.dishes
        refs: List[str] = []
        for dish in payload.get("dishes") or []:
            item_id = dish["menu_item_id"]
            refs.append(
                context.add_evidence(
                    f"dish1.{item_id}.e1",
                    f"菜品 #{item_id} 本期销量",
                    dish["current_period"]["quantity"],
                    str(dish["current_period"]["start_date"]),
                )
            )
            refs.append(
                context.add_evidence(
                    f"dish1.{item_id}.e2",
                    f"菜品 #{item_id} 上期销量",
                    dish["baseline_period"]["quantity"],
                    str(dish["baseline_period"]["start_date"]),
                )
            )
            if dish.get("change_pct") is not None:
                refs.append(
                    context.add_evidence(
                        f"dish1.{item_id}.e3",
                        f"菜品 #{item_id} 销量变化幅度",
                        dish["change_pct"],
                        "百分比",
                    )
                )
            for point in dish.get("notable_days") or []:
                refs.append(
                    context.add_evidence(
                        point["ref"],
                        f"菜品 #{item_id} {point['date']} 销量",
                        point["quantity"],
                        str(point.get("day_type") or ""),
                    )
                )
        context.observe(payload)
        context.record_step(
            "get_dish_daily",
            {
                "menu_item_ids": menu_item_ids,
                "start_date": start_date,
                "end_date": end_date,
                "include_daily": include_daily,
            },
            summary=f"查询 {len(payload.get('dishes') or [])} 个菜品的销量汇与异常日",
            evidence_refs=refs,
            window={"start_date": str(start), "end_date": str(end)},
        )
        return _dump(payload)

    @tool
    async def compare_peers(start_date: str, end_date: str) -> str:
        """把本店与固定数据集中的可比门店做同窗口对比。"""
        await _fault("compare_peers")
        start = _parse(start_date, "start_date")
        end = _parse(end_date, "end_date")
        mismatch = _check_window(
            "compare_peers",
            {"start_date": start_date, "end_date": end_date},
            start,
            end,
        )
        if mismatch is not None:
            return mismatch
        payload = fixtures.peers
        refs: List[str] = []
        for index, peer in enumerate(payload.get("peers") or [], 1):
            peer["ref"] = context.add_evidence(
                f"peers1.e{index}",
                f"{peer['branch_name']} 营业额变化",
                peer.get("change_pct"),
                str(peer.get("verdict") or ""),
            )
            refs.append(peer["ref"])
        refs.append(
            context.add_evidence(
                "peers1.s1",
                "同业对比结论",
                payload.get("comparable_count"),
                str(payload.get("summary") or ""),
            )
        )
        context.observe(payload)
        context.record_step(
            "compare_peers",
            {"start_date": start_date, "end_date": end_date},
            summary=str(payload.get("summary") or ""),
            evidence_refs=refs,
            window={"start_date": str(start), "end_date": str(end)},
        )
        return _dump(payload)

    @tool
    async def get_operational_changes(start_date: str, end_date: str) -> str:
        """查询固定数据集中可能影响经营的运营变更。"""
        await _fault("get_operational_changes")
        start = _parse(start_date, "start_date")
        end = _parse(end_date, "end_date")
        mismatch = _check_window(
            "get_operational_changes",
            {"start_date": start_date, "end_date": end_date},
            start,
            end,
        )
        if mismatch is not None:
            return mismatch
        payload = fixtures.changes
        refs: List[str] = []
        for index, change in enumerate(payload.get("changes") or [], 1):
            refs.append(
                context.add_evidence(
                    change.get("ref") or f"ops1.e{index}",
                    f"{change.get('action_label')}（{change.get('entity_label')}"
                    f"#{change.get('entity_id')}）",
                    change.get("entity_id"),
                    f"{change.get('actor_name') or '系统'} @ "
                    f"{change.get('created_at')}",
                )
            )
        context.observe(payload)
        context.record_step(
            "get_operational_changes",
            {"start_date": start_date, "end_date": end_date},
            summary=f"窗口内运营变更 {len(refs)} 条",
            evidence_refs=refs,
            window={"start_date": str(start), "end_date": str(end)},
        )
        return _dump(payload)

    @tool
    async def get_business_profile(start_date: str, end_date: str) -> str:
        """查询固定数据集中的预订健康度、时段/星期分布、订单类型与基线。"""
        await _fault("get_business_profile")
        start = _parse(start_date, "start_date")
        end = _parse(end_date, "end_date")
        mismatch = _check_window(
            "get_business_profile",
            {"start_date": start_date, "end_date": end_date},
            start,
            end,
        )
        if mismatch is not None:
            return mismatch
        payload = fixtures.profile
        namespace = context.namespace("profile")
        summary = payload["summary"]
        reservations = payload["reservations"]
        baselines = payload["baselines"]
        refs = {
            "order_types": context.add_evidence(
                f"{namespace}t1",
                "订单类型结构",
                payload["order_types"],
                f"成交 {summary['order_count']} 单",
            ),
            "weekday_distribution": context.add_evidence(
                f"{namespace}w1",
                "星期分布",
                payload["weekday_distribution"],
                payload.get("timezone", ""),
            ),
            "hour_distribution": context.add_evidence(
                f"{namespace}h1",
                "时段分布",
                payload["hour_distribution"],
                payload.get("timezone", ""),
            ),
            "reservations": context.add_evidence(
                f"{namespace}r1",
                "预订健康度",
                reservations,
                f"预订 {reservations['booked_count']} 条",
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
        context.observe(result)
        context.record_step(
            "get_business_profile",
            {"start_date": start_date, "end_date": end_date},
            summary=(
                f"订单 {summary['order_count']} 单；预订 "
                f"{reservations['booked_count']} 条"
            ),
            evidence_refs=list(refs.values()),
            window={"start_date": str(start), "end_date": str(end)},
        )
        return _dump(result)

    return [
        resolve_time_range,
        get_diagnosis,
        get_daily_series,
        get_business_profile,
        get_dish_daily,
        compare_peers,
        get_operational_changes,
    ]


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def _number_known(number: float, allowed: List[float]) -> bool:
    return any(abs(number - candidate) <= _NUMBER_TOLERANCE for candidate in allowed)


def _citation_metrics(
    model_report: Optional[Mapping[str, Any]], accepted_refs: List[str]
) -> Dict[str, Any]:
    accepted = set(accepted_refs)
    total = valid = missing = 0
    if isinstance(model_report, Mapping):
        for section in ("facts", "inferences", "checks"):
            claims = model_report.get(section) or []
            if not isinstance(claims, list):
                continue
            for claim in claims:
                if not isinstance(claim, Mapping):
                    continue
                cited = [
                    ref
                    for ref in claim.get("evidence_refs") or []
                    if isinstance(ref, str)
                ]
                if not cited:
                    missing += 1
                for ref in cited:
                    total += 1
                    if ref in accepted:
                        valid += 1
        for hypothesis in model_report.get("hypotheses") or []:
            if not isinstance(hypothesis, Mapping):
                continue
            # A hypothesis may not borrow a fact's authority; any ref is invalid.
            for ref in hypothesis.get("evidence_refs") or []:
                if isinstance(ref, str):
                    total += 1
    denominator = total + missing
    return {
        "total": total,
        "valid": valid,
        "missing": missing,
        "validity": 1.0 if denominator == 0 else valid / denominator,
    }


def _number_metrics(
    model_report: Optional[Mapping[str, Any]], allowed: List[float]
) -> Dict[str, Any]:
    texts: List[str] = []
    if isinstance(model_report, Mapping):
        texts.append(str(model_report.get("answer") or ""))
        for section in ("facts", "inferences", "checks", "hypotheses"):
            items = model_report.get(section) or []
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, Mapping):
                    texts.append(str(item.get("text") or ""))
    total = 0
    hallucinated: List[float] = []
    for text in texts:
        for number in numbers_in_text(text):
            total += 1
            if not _number_known(number, allowed):
                hallucinated.append(number)
    return {
        "total": total,
        "hallucinated": hallucinated,
        "hallucination_rate": 0.0 if total == 0 else len(hallucinated) / total,
    }


def _window_metrics(
    trace: RunTrace,
    expected_window: Mapping[str, Any],
    context: DiagnosisToolContext,
) -> Dict[str, Any]:
    expected = (
        expected_window.get("start_date"),
        expected_window.get("end_date"),
    )
    dated_calls = 0
    matching_calls = 0
    for call in trace.tool_calls:
        if call.name not in DATE_TOOL_NAMES:
            continue
        args = call.arguments or {}
        try:
            start = date.fromisoformat(str(args.get("start_date")))
            end = date.fromisoformat(str(args.get("end_date")))
        except (TypeError, ValueError):
            continue
        dated_calls += 1
        if (start, end) == expected:
            matching_calls += 1
    mismatches = sum(
        1
        for step in context.steps
        if step.get("status") == "error" and step.get("expected_window")
    )
    resolved = context.resolved_window or {}
    resolved_ok = (
        resolved.get("start_date"),
        resolved.get("end_date"),
    ) == expected
    return {
        "expected_start_date": str(expected_window.get("start_date")),
        "expected_end_date": str(expected_window.get("end_date")),
        "resolved_start_date": (
            str(resolved.get("start_date")) if resolved else None
        ),
        "resolved_end_date": str(resolved.get("end_date")) if resolved else None,
        "resolved_window_ok": resolved_ok,
        "dated_calls": dated_calls,
        "matching_calls": matching_calls,
        "accuracy": 1.0 if dated_calls == 0 else matching_calls / dated_calls,
        "mismatches": mismatches,
    }


def _turn_metrics(
    *,
    turn_index: int,
    question: str,
    expect: Mapping[str, Any],
    trace: RunTrace,
    budget,
    context: DiagnosisToolContext,
    composed: Mapping[str, Any],
    model: ScriptedTurnModel,
    expected_window: Mapping[str, Any],
    thread_evidence_count: int,
) -> Dict[str, Any]:
    summary = trace.summary()
    tools = [call.name for call in trace.tool_calls]
    expected_tools = expect.get("tools")
    tokens = {
        "prompt": model.prompt_tokens,
        "tool_output": summary["estimated_output_tokens"],
        "answer": summary["answer_tokens"],
        "context_after": (trace.context or {}).get("after_tokens", 0),
    }
    tokens["total"] = tokens["prompt"] + tokens["tool_output"] + tokens["answer"]
    return {
        "turn_index": turn_index,
        "question": question,
        "tools": tools,
        "tool_selection_match": (
            expected_tools is None or list(expected_tools) == tools
        ),
        "window": _window_metrics(trace, expected_window, context),
        "citation": _citation_metrics(
            composed.get("model_report"), composed.get("refs") or []
        ),
        "numbers": _number_metrics(
            composed.get("model_report"), composed.get("allowed_numbers") or []
        ),
        "dropped": int((composed.get("report") or {}).get("dropped") or 0),
        "report_source": str((composed.get("report") or {}).get("source") or ""),
        "answer_violations": list(composed.get("answer_violations") or []),
        "degraded": bool(summary["degraded"]),
        "degrade_reasons": list(summary["degrade_reasons"]),
        "tool_failures": summary["tool_failures"],
        "tool_timeouts": summary["tool_timeouts"],
        "budget_refusals": summary["tool_budget_refusals"],
        "budget": {
            "tool_calls_used": budget.used,
            "tool_calls_limit": budget.limit,
            "exhausted": budget.exhausted,
            "reason": budget.reason,
        },
        "model_calls": model.calls,
        "evidence_count": summary["evidence_count"],
        "thread_evidence_count": thread_evidence_count,
        "elapsed_ms": summary["elapsed_ms"],
        "tokens": tokens,
        "answer": str(composed.get("answer") or ""),
        "notes": list(composed.get("notes") or []),
    }


def _check_expectations(
    expect: Mapping[str, Any], metrics: Mapping[str, Any]
) -> List[str]:
    failures: List[str] = []

    def compare(label: str, expected: Any, actual: Any) -> None:
        if expected != actual:
            failures.append(f"{label}：期望 {expected!r}，实际 {actual!r}")

    if "tools" in expect:
        compare("工具轨迹", list(expect["tools"]), metrics["tools"])
    window = metrics["window"]
    if "window_accuracy" in expect:
        if abs(float(expect["window_accuracy"]) - window["accuracy"]) > 1e-9:
            failures.append(
                "窗口准确率：期望 "
                f"{expect['window_accuracy']!r}，实际 {window['accuracy']!r}"
            )
    if "window_mismatches" in expect:
        compare("窗口不一致次数", expect["window_mismatches"], window["mismatches"])
    if "resolved_window_ok" in expect:
        compare("解析窗口", expect["resolved_window_ok"], window["resolved_window_ok"])
    if "budget_used" in expect:
        compare("工具预算使用", expect["budget_used"], metrics["budget"]["tool_calls_used"])
    if "budget_exhausted" in expect:
        compare("工具预算耗尽", expect["budget_exhausted"], metrics["budget"]["exhausted"])
    if "budget_refusals" in expect:
        compare("预算拒绝次数", expect["budget_refusals"], metrics["budget_refusals"])
    if "tool_failures" in expect:
        compare("工具失败次数", expect["tool_failures"], metrics["tool_failures"])
    if "tool_timeouts" in expect:
        compare("工具超时次数", expect["tool_timeouts"], metrics["tool_timeouts"])
    if "dropped" in expect:
        compare("护栏丢弃条数", expect["dropped"], metrics["dropped"])
    if "report_source" in expect:
        compare("报告来源", expect["report_source"], metrics["report_source"])
    if "degraded" in expect:
        compare("降级状态", expect["degraded"], metrics["degraded"])
    required_reasons = set(expect.get("degrade_reasons") or [])
    missing_reasons = required_reasons - set(metrics["degrade_reasons"])
    if missing_reasons:
        failures.append(
            "降级原因缺少 "
            f"{sorted(missing_reasons)!r}（实际 {metrics['degrade_reasons']!r}）"
        )
    citation = metrics["citation"]
    if "min_citation_validity" in expect:
        if citation["validity"] < float(expect["min_citation_validity"]) - 1e-9:
            failures.append(
                "引用有效率：期望至少 "
                f"{expect['min_citation_validity']!r}，实际 {citation['validity']!r}"
            )
    if "max_citation_validity" in expect:
        if citation["validity"] > float(expect["max_citation_validity"]) + 1e-9:
            failures.append(
                "引用有效率：期望至多 "
                f"{expect['max_citation_validity']!r}，实际 {citation['validity']!r}"
            )
    hallucinated = metrics["numbers"]["hallucinated"]
    if "min_number_hallucinations" in expect:
        if len(hallucinated) < int(expect["min_number_hallucinations"]):
            failures.append(
                "数字幻觉：期望至少 "
                f"{expect['min_number_hallucinations']} 个，实际 {hallucinated!r}"
            )
    if "max_number_hallucinations" in expect:
        if len(hallucinated) > int(expect["max_number_hallucinations"]):
            failures.append(
                "数字幻觉：期望至多 "
                f"{expect['max_number_hallucinations']} 个，实际 {hallucinated!r}"
            )
    if "answer_violations" in expect:
        compare(
            "回答校验失败",
            expect["answer_violations"],
            bool(metrics["answer_violations"]),
        )
    if "min_thread_evidence_count" in expect:
        if metrics["thread_evidence_count"] < int(
            expect["min_thread_evidence_count"]
        ):
            failures.append(
                "线程证据数：期望至少 "
                f"{expect['min_thread_evidence_count']}，"
                f"实际 {metrics['thread_evidence_count']}"
            )
    return failures


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def _empty_diagnosis() -> Dict[str, Any]:
    return {
        "branch_id": 1,
        "branch_name": "总店",
        "status": "insufficient_data",
        "current_period": {},
        "baseline_period": {},
        "findings": [],
        "notes": ["本轮未调用 get_diagnosis。"],
    }


async def run_scenario(
    dataset: EvalDataset, scenario: Mapping[str, Any]
) -> Dict[str, Any]:
    fixtures = EvalFixtures(
        windows=dataset.windows,
        diagnoses=dataset.diagnoses,
        daily=dataset.daily,
        dishes=dataset.dishes,
        peers=dataset.peers,
        changes=dataset.changes,
        profile=dataset.profile,
        faults=dict(scenario.get("faults") or {}),
    )
    checkpointer = MemorySaver()
    thread_id = f"eval-{scenario['id']}"
    config = HarnessConfig(
        tool_calls_limit=int(
            scenario.get("tool_calls_limit", DEFAULT_TOOL_CALLS_LIMIT)
        ),
        recursion_limit=int(scenario.get("recursion_limit", DEFAULT_RECURSION_LIMIT)),
        tool_timeout_seconds=scenario.get("tool_timeout_seconds"),
        turn_timeout_seconds=30.0,
    )
    previous_evidence: List[Dict[str, Any]] = []
    failures: List[str] = []
    turn_results: List[Dict[str, Any]] = []

    for index, turn_spec in enumerate(scenario.get("turns") or [], 1):
        context = DiagnosisToolContext(
            staff=None,  # type: ignore[arg-type] - fixtures never read identity
            session=None,  # type: ignore[arg-type] - fixtures never touch the DB
            branch_id=1,
        )
        trace = RunTrace(thread_id=thread_id, branch_id=1, config=config)
        model = ScriptedTurnModel(responses=_build_responses(turn_spec))
        try:
            harness_turn = await run_harness_turn(
                tools=build_fixture_tools(context, fixtures),
                question=turn_spec["question"],
                thread_id=thread_id,
                config=config,
                trace=trace,
                checkpointer=checkpointer,
                model=model,
            )
        except Exception as error:  # one broken scenario must not stop the report
            failures.append(
                f"第 {index} 轮执行失败：{type(error).__name__}: {error}"
            )
            break

        agent_turn = harness_turn.turn
        if agent_turn is None:
            failures.append(f"第 {index} 轮没有返回 Agent 结果。")
            break
        if agent_turn.error:
            failures.append(f"第 {index} 轮提前结束：{agent_turn.error}")

        previous_evidence = merge_thread_evidence(
            previous_evidence, agent_turn.thread_evidence
        )
        expected_window = fixtures.window(str(turn_spec.get("window") or "main"))
        window = context.resolved_window or expected_window
        composed = _compose_answer_and_report(
            agent_answer=agent_turn.answer,
            diagnosis=context.diagnosis or _empty_diagnosis(),
            context=context,
            budget=harness_turn.budget,
            window=window,
            question=turn_spec["question"],
            trace=trace,
            thread_evidence=previous_evidence,
            turn_index=agent_turn.turn_index,
            structured=agent_turn.structured,
        )
        trace.record_budget(harness_turn.budget.snapshot(config.recursion_limit))
        trace.finish()

        expect = turn_spec.get("expect") or {}
        metrics = _turn_metrics(
            turn_index=agent_turn.turn_index,
            question=turn_spec["question"],
            expect=expect,
            trace=trace,
            budget=harness_turn.budget,
            context=context,
            composed=composed,
            model=model,
            expected_window=expected_window,
            thread_evidence_count=len(previous_evidence),
        )
        turn_failures = _check_expectations(expect, metrics)
        failures.extend(f"第 {index} 轮 {item}" for item in turn_failures)
        turn_results.append(
            {
                "question": turn_spec["question"],
                "expect": dict(expect),
                "metrics": metrics,
                "report": composed.get("report"),
            }
        )

    return {
        "id": scenario["id"],
        "description": scenario.get("description") or "",
        "passed": not failures,
        "failures": failures,
        "turns": turn_results,
    }


def _aggregate(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    turns = [turn for result in results for turn in result["turns"]]
    turn_count = len(turns)
    scenarios = len(results)
    passed = sum(1 for result in results if result["passed"])

    dated_calls = sum(t["metrics"]["window"]["dated_calls"] for t in turns)
    matching_calls = sum(t["metrics"]["window"]["matching_calls"] for t in turns)
    citation_total = sum(
        t["metrics"]["citation"]["total"] + t["metrics"]["citation"]["missing"]
        for t in turns
    )
    citation_valid = sum(t["metrics"]["citation"]["valid"] for t in turns)
    number_total = sum(t["metrics"]["numbers"]["total"] for t in turns)
    hallucinated = sum(
        len(t["metrics"]["numbers"]["hallucinated"]) for t in turns
    )
    degraded = sum(1 for t in turns if t["metrics"]["degraded"])
    intervened = sum(
        1
        for t in turns
        if t["metrics"]["dropped"] or t["metrics"]["answer_violations"]
    )
    tokens = {
        key: sum(t["metrics"]["tokens"][key] for t in turns)
        for key in ("prompt", "tool_output", "answer", "context_after", "total")
    }
    latency = sum(float(t["metrics"]["elapsed_ms"] or 0.0) for t in turns)
    return {
        "scenarios": scenarios,
        "passed": passed,
        "failed": scenarios - passed,
        "pass_rate": 1.0 if scenarios == 0 else passed / scenarios,
        "turns": turn_count,
        "tool_selection_accuracy": (
            1.0
            if turn_count == 0
            else sum(1 for t in turns if t["metrics"]["tool_selection_match"])
            / turn_count
        ),
        "window_accuracy": (
            1.0 if dated_calls == 0 else matching_calls / dated_calls
        ),
        "citation_validity": (
            1.0 if citation_total == 0 else citation_valid / citation_total
        ),
        "number_hallucination_rate": (
            0.0 if number_total == 0 else hallucinated / number_total
        ),
        "degradation_rate": (
            0.0 if turn_count == 0 else degraded / turn_count
        ),
        "guardrail_intervention_rate": (
            0.0 if turn_count == 0 else intervened / turn_count
        ),
        "avg_latency_ms": (
            0.0 if turn_count == 0 else round(latency / turn_count, 3)
        ),
        "total_latency_ms": round(latency, 3),
        "tokens": tokens,
        "avg_tokens_per_turn": (
            0 if turn_count == 0 else round(tokens["total"] / turn_count, 1)
        ),
        "model_calls": sum(t["metrics"]["model_calls"] for t in turns),
    }


async def run_evaluation(dataset: Optional[EvalDataset] = None) -> Dict[str, Any]:
    """Run every scenario and return the JSON-safe metrics report."""
    dataset = dataset or load_dataset()
    results = [await run_scenario(dataset, scenario) for scenario in dataset.scenarios]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "path": _display_path(dataset.source_path),
            "version": dataset.version,
            "sha256": dataset.sha256,
            "scenarios": len(dataset.scenarios),
        },
        "harness": {
            "mode": "offline-scripted",
            "provider_calls": 0,
            "database_access": False,
        },
        "summary": _aggregate(results),
        "scenarios": results,
    }


def _display_path(raw: str) -> str:
    """Report paths relative to the working directory when they live under it."""
    path = Path(raw)
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


def write_report(report: Mapping[str, Any], path: Path) -> Path:
    """Write the metrics report to disk, creating its directory if needed."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return target


__all__ = [
    "DEFAULT_DATASET_PATH",
    "EvalDataset",
    "EvalFixtures",
    "ScriptedTurnModel",
    "build_fixture_tools",
    "load_dataset",
    "run_evaluation",
    "run_scenario",
    "write_report",
]
