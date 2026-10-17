"""Diagnosis Agent harness (Task52 contracts, Task53 runtime).

The module owns everything the investigation endpoint needs around the model:

* **Contracts** -- ``HarnessConfig``, ``RunTrace``, and the evidence-registry
  protocol. They import no LangChain code, so tools and reports can share them.
* **Runtime** -- the chat-model adapter, guarded tools with a shared budget,
  agent construction with the checkpointer, optional timeouts, and the single
  entry point ``run_harness_turn``.

``diagnosis_investigation`` keeps authorisation, window resolution and response
assembly; it does not build models or agents itself. ``diagnosis_agent`` is a
compatibility re-export so existing imports keep working (Task53).
"""

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    NotRequired,
    Optional,
    Sequence,
    Set,
)

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import after_agent, wrap_model_call
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.errors import GraphRecursionError

from backend.app.agent.diagnosis_report import (
    STRUCTURED_REPORT_TOOL_NAME,
    AgentInvestigationReport,
    report_to_dict,
)
from backend.app.agent.llm_providers import OPENAI_PROVIDER_ALIASES
from backend.app.config import settings

logger = logging.getLogger("diagnosis_harness")

# The model may investigate, not roam: six read-only calls are enough for
# diagnosis -> daily series -> dish detail -> peers -> audit changes -> follow-up.
DEFAULT_TOOL_CALLS_LIMIT = 6
# LangGraph counts model and tool super-steps; with 6 tool calls a normal turn
# needs ~13. 24 leaves room for one correction without allowing an open loop.
DEFAULT_RECURSION_LIMIT = 24

# Window resolution is setup, not investigation: it never consumes one of the
# six probes the model may spend on data. It is still traced and still bounded
# by the recursion limit.
FREE_TOOL_NAMES = frozenset({"resolve_time_range"})

# LangGraph v1/v2 writes this message into the state when a graph reaches its
# recursion limit instead of raising; it is infrastructure text, not an answer.
_STEP_LIMIT_SENTINEL = "sorry, need more steps"

AGENT_SYSTEM_PROMPT = """你是连锁餐厅的经营诊断调查 Agent。你只能通过只读工具了解数据，不能编造。

工作方式：
1. 先判断用户问题涉及的时间区间。问题里有明确时间表达（如“上周”“最近7天”“上个月”）时，
   优先调用 resolve_time_range 解析；没有明确表达时不要反复猜测，直接调用它并传空字符串。
   工具会按数据库可用数据范围解析相对时间（历史数据可能锚定到最近有数据的日期），
   返回的 notes 说明了实际区间与数据覆盖；回答时要告诉用户实际调查的是哪段时间。
   所有带日期的工具都必须使用 resolve_time_range 返回的 start_date / end_date；
   若工具返回 window_mismatch，按其中的 resolved_window 重试，不要混用其他区间。
2. 根据问题选择必要的工具，不要为了“看全”而调用所有工具。销售额/订单量问题先看 get_diagnosis；
   需要定位单品再看 get_dish_daily；需要判断是否全店共性问题再看 compare_peers；
   需要确认是否由运营调整导致再看 get_operational_changes。
   需要看预订健康度、时段/星期分布、订单类型结构，或与上周同星期/去年同期对比时，
   再看 get_business_profile。
3. 每一步工具结果都会带 evidence_ref（如 f1.e2）。写结论时，数据事实必须引用真实存在的 ref。
   当前轮可直接使用工具结果里的短 ref；引用历史轮次的证据时，使用历史备忘中带轮次前缀的
   ref（如 t1.f0.e2）。不要给当前轮的短 ref 自行添加错误的前缀。
4. 只能使用工具结果里出现过的数字，不要自行计算或补充工具没有给出的数字。
5. 区分三类内容：工具直接给出的事实（facts）、引用证据的推断（inferences）、
   没有证据的解释（hypotheses，必须标为“未经证实”）。
6. 工具预算有限（resolve_time_range 不占预算）。预算用尽或证据不足时，
   明确说明“尚未检查什么”，不要用猜测补全。
7. 调查结束后，调用结构化结果工具 AgentInvestigationReport 提交最终报告（只调用一次）：
   answer 是给用户看的中文回答；facts / inferences / checks 的每条包含 text 与 evidence_refs；
   hypotheses 只包含 text。不要再用纯文本 JSON 作为最终回答。
8. facts / inferences / checks 的每一条都必须引用真实存在的 evidence_ref；hypotheses 不要写 evidence_ref，
   系统会自动加上“未经证实：”标注。四类内容都只能使用工具结果中出现过的数字。
9. answer 里也要引用具体的 evidence_ref，不要复述工具没有给出的数字。
"""

# CJK characters are roughly one token each for the models used here; Latin
# text and JSON punctuation average about four characters per token. This is a
# budgeting heuristic, not a provider tokenizer.
_CJK_PATTERN = re.compile(
    "[㐀-䶿一-鿿豈-﫿"
    "぀-ヿ가-힯]"
)


def estimate_tokens(value: Any) -> int:
    """Deterministic token estimate for strings, dicts and lists.

    Used for context budgeting and baseline reporting. It intentionally does
    not depend on the provider: the same payload always yields the same number,
    and the number is only compared against harness budgets, never reported as
    exact model usage.
    """
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    if not text:
        return 0
    cjk = len(_CJK_PATTERN.findall(text))
    other = len(text) - cjk
    return int(math.ceil(cjk + other / 4))


@dataclass(frozen=True)
class HarnessConfig:
    """Limits for one investigation turn.

    ``tool_calls_limit`` and ``recursion_limit`` are enforced today. The other
    fields are opt-in; ``None`` means "not enforced", so constructing the
    default config preserves the pre-harness behaviour exactly.
    """

    tool_calls_limit: int = DEFAULT_TOOL_CALLS_LIMIT
    recursion_limit: int = DEFAULT_RECURSION_LIMIT
    max_tool_output_chars: Optional[int] = None
    context_token_budget: Optional[int] = None
    context_char_budget: Optional[int] = None
    tool_timeout_seconds: Optional[float] = None
    turn_timeout_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        if self.tool_calls_limit < 0:
            raise ValueError("tool_calls_limit must be >= 0.")
        if self.recursion_limit < 1:
            raise ValueError("recursion_limit must be >= 1.")
        for name in (
            "max_tool_output_chars",
            "context_token_budget",
            "context_char_budget",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be >= 0 when set.")
        for name in ("tool_timeout_seconds", "turn_timeout_seconds"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be > 0 when set.")


# --------------------------------------------------------------------------- #
# Thread evidence registry (Task55)
# --------------------------------------------------------------------------- #
# A ref is stable inside one investigation thread when it carries the turn it
# was produced in: ``t1.f0.e1`` is the first turn's first finding evidence,
# ``t2.daily1.e1`` the second turn's first daily point. Tool-local refs remain
# the short form the tools emit and the API returns; the turn prefix is added
# when evidence becomes part of the thread registry.
TURN_REF_PATTERN = re.compile(r"^t(\d+)\.(.+)$", re.I)
# Matches both ``f0.e1`` and ``t2.f0.e1`` (and namespaced refs such as
# ``t2.dish1.5.d3``), so prose validation sees the whole qualified token.
REF_TOKEN_PATTERN = re.compile(
    r"\b(?:t\d+\.)?[a-z]+\d+(?:\.\d+)?\.[a-z]+\d+\b", re.I
)


def split_evidence_ref(ref: str, default_turn: int = 1) -> tuple:
    """Return ``(turn, local_ref)`` for a qualified or legacy ref."""
    match = TURN_REF_PATTERN.match(ref or "")
    if match:
        return int(match.group(1)), match.group(2)
    return int(default_turn), str(ref or "")


def qualify_evidence_ref(ref: str, turn: int) -> str:
    """Return the thread-stable form of ``ref`` for one turn."""
    ref = str(ref or "")
    if not ref or TURN_REF_PATTERN.match(ref):
        return ref
    return f"t{int(turn)}.{ref}"


def qualify_refs_in_text(text: str, turn: int) -> str:
    """Prefix every unqualified ref in prose with its turn."""
    return REF_TOKEN_PATTERN.sub(
        lambda match: qualify_evidence_ref(match.group(0), turn),
        text or "",
    )


def evidence_records_from_items(
    items: Iterable[Mapping[str, Any]], turn: int
) -> List[Dict[str, Any]]:
    """Turn API evidence items into qualified thread-registry records."""
    records: List[Dict[str, Any]] = []
    for item in items or ():
        ref = item.get("ref")
        if not isinstance(ref, str) or not ref:
            continue
        ref_turn, local_ref = split_evidence_ref(ref, int(turn))
        records.append(
            {
                "ref": qualify_evidence_ref(local_ref, ref_turn),
                "local_ref": local_ref,
                "turn": ref_turn,
                "label": str(item.get("label") or ""),
                "value": item.get("value"),
                "detail": str(item.get("detail") or ""),
            }
        )
    return records


def _tool_payload(content: Any) -> Any:
    """Parse a ToolMessage's JSON body; ``None`` when it is not JSON."""
    text = content if isinstance(content, str) else _text_of(content)
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _collect_evidence_records(
    payload: Any,
    output: List[Dict[str, Any]],
    seen: Set[str],
    turn: int,
) -> None:
    """Collect ``ref``-bearing dicts from a tool result, turn-qualified."""
    if isinstance(payload, dict):
        ref = payload.get("ref")
        if isinstance(ref, str) and ref:
            ref_turn, local_ref = split_evidence_ref(ref, turn)
            qualified = qualify_evidence_ref(local_ref, ref_turn)
            if qualified not in seen:
                seen.add(qualified)
                output.append(
                    {
                        "ref": qualified,
                        "local_ref": local_ref,
                        "turn": ref_turn,
                        "label": str(payload.get("label") or ""),
                        "value": payload.get("value"),
                        "detail": str(payload.get("detail") or ""),
                    }
                )
            return
        for value in payload.values():
            _collect_evidence_records(value, output, seen, turn)
    elif isinstance(payload, list):
        for item in payload:
            _collect_evidence_records(item, output, seen, turn)


def collect_thread_evidence(
    messages: Sequence[BaseMessage],
) -> List[Dict[str, Any]]:
    """Rebuild the thread registry from checkpointed messages.

    Turn numbers are counted from ``HumanMessage`` boundaries, so the same
    messages always produce the same refs. This works for checkpoints written
    before the registry channel existed (Task55 compatibility): their tool
    messages contain unprefixed refs and are qualified on read.
    """
    records: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    turn = 0
    for message in messages or ():
        if isinstance(message, HumanMessage):
            turn += 1
        elif isinstance(message, ToolMessage) and turn:
            payload = _tool_payload(message.content)
            if payload is not None:
                _collect_evidence_records(payload, records, seen, turn)
    return records


def merge_thread_evidence(
    *groups: Optional[Iterable[Mapping[str, Any]]],
) -> List[Dict[str, Any]]:
    """Merge registry groups by qualified ref, last definition winning."""
    merged: Dict[str, Dict[str, Any]] = {}
    for group in groups:
        for record in group or ():
            ref = record.get("ref")
            if isinstance(ref, str) and ref:
                merged[ref] = dict(record)
    return list(merged.values())


class DiagnosisAgentState(AgentState):
    """Agent state plus the evidence registry persisted with the thread."""

    thread_evidence: NotRequired[List[Dict[str, Any]]]


class ThreadEvidenceRegistry:
    """Evidence accumulated across all turns of one investigation thread.

    Refs are qualified with their turn, which makes ``t1.f0.e1`` and
    ``t2.f0.e1`` different facts even though both tools used the local name
    ``f0.e1``. A legacy bare ref still resolves when it is unambiguous (only
    one turn produced it); if several turns did, the current turn wins, so old
    model output keeps working without letting an ambiguous citation borrow a
    fact from another turn.
    """

    def __init__(
        self,
        records: Iterable[Mapping[str, Any]] = (),
        *,
        current_turn: Optional[int] = None,
    ) -> None:
        self._index: Dict[str, Dict[str, Any]] = {}
        for raw in records or ():
            record = self._normalize(raw, current_turn or 1)
            if record is not None:
                # A later definition wins; insertion order stays stable.
                self._index[record["ref"]] = record
        turns = [record["turn"] for record in self._index.values()]
        self.current_turn = int(current_turn or max(turns, default=1))

    @staticmethod
    def _normalize(
        raw: Mapping[str, Any], default_turn: int
    ) -> Optional[Dict[str, Any]]:
        ref = raw.get("ref")
        if not isinstance(ref, str) or not ref:
            return None
        match = TURN_REF_PATTERN.match(ref)
        if match:
            turn = int(match.group(1))
            local_ref = match.group(2)
        else:
            turn = int(raw.get("turn") or default_turn)
            local_ref = ref
        if not local_ref:
            return None
        return {
            "ref": qualify_evidence_ref(local_ref, turn),
            "local_ref": local_ref,
            "turn": turn,
            "label": str(raw.get("label") or ""),
            "value": raw.get("value"),
            "detail": str(raw.get("detail") or ""),
        }

    def add(
        self, ref: str, label: str, value: Any = None, detail: str = ""
    ) -> str:
        record = self._normalize(
            {
                "ref": ref,
                "label": label,
                "value": value,
                "detail": detail,
                "turn": self.current_turn,
            },
            self.current_turn,
        )
        if record is None:
            raise ValueError("evidence ref must not be empty.")
        if record["ref"] in self._index:
            raise ValueError(f"duplicate evidence ref: {record['ref']}")
        self._index[record["ref"]] = record
        return record["ref"]

    def get(self, ref: str) -> Optional[Dict[str, Any]]:
        record = self._index.get(ref)
        if record is not None:
            return record
        return self.accepted_refs().get(ref)

    def refs(self) -> Set[str]:
        """Qualified refs only -- the stable addresses of this thread."""
        return set(self._index)

    def accepted_refs(self) -> Dict[str, Dict[str, Any]]:
        """Every ref the guardrail may accept, including legacy aliases.

        The current turn's local names map to the current turn's facts; a bare
        ref produced by exactly one older turn keeps resolving for old
        checkpoints. A bare ref that several turns reuse is left out when it is
        not in the current turn: guessing which turn was meant is exactly the
        ambiguity the turn prefix exists to remove.
        """
        accepted: Dict[str, Dict[str, Any]] = dict(self._index)
        by_local: Dict[str, Dict[int, Dict[str, Any]]] = {}
        for record in self._index.values():
            by_local.setdefault(record["local_ref"], {}).setdefault(
                record["turn"], record
            )
        for local_ref, per_turn in by_local.items():
            if self.current_turn in per_turn:
                accepted.setdefault(local_ref, per_turn[self.current_turn])
            elif len(per_turn) == 1:
                accepted.setdefault(local_ref, next(iter(per_turn.values())))
        return accepted

    def snapshot(self) -> List[Dict[str, Any]]:
        return [dict(record) for record in self._index.values()]

    def __len__(self) -> int:
        return len(self._index)


def build_thread_evidence_middleware():
    """Persist the thread registry into the graph's checkpoint state.

    The hook rebuilds the registry from the state's own messages at the end of
    every completed turn. Storing it as a state channel means no extra table is
    introduced in this phase, and a checkpoint written before this channel
    existed simply starts with an empty registry and is rebuilt on the next
    turn.
    """

    @after_agent(name="diagnosis_thread_evidence")
    async def _diagnosis_thread_evidence(state, runtime):
        return {
            "thread_evidence": collect_thread_evidence(
                state.get("messages") or []
            )
        }

    return _diagnosis_thread_evidence


async def load_thread_evidence(checkpointer, thread_id: str) -> tuple:
    """Read a thread's persisted registry and completed-turn count.

    Used by the deterministic fallback, which never runs the graph and so never
    triggers the after-agent hook. Any checkpointer failure degrades to an
    empty registry: reading memory must not fail an otherwise valid answer.
    """
    if checkpointer is None:
        return [], 0
    try:
        saved = await checkpointer.aget_tuple(
            {"configurable": {"thread_id": thread_id}}
        )
    except Exception:  # pragma: no cover - defensive checkpointer seam
        return [], 0
    if saved is None:
        return [], 0
    values = (saved.checkpoint or {}).get("channel_values", {})
    messages = values.get("messages") or []
    records = values.get("thread_evidence")
    if not isinstance(records, list) or not records:
        records = collect_thread_evidence(messages)
    turns = sum(1 for message in messages if isinstance(message, HumanMessage))
    return records, turns


@dataclass
class ToolCallRecord:
    """One guarded tool execution (or a call refused by the budget)."""

    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    status: str = "ok"  # ok / error / timeout / budget_exhausted
    duration_ms: float = 0.0
    output_chars: int = 0
    output_tokens: int = 0
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "arguments": self.arguments,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 3),
            "output_chars": self.output_chars,
            "output_tokens": self.output_tokens,
            "error": self.error,
        }


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


@dataclass
class RunTrace:
    """Everything one investigation turn did, in reporting form.

    The trace is intentionally not part of the HTTP response: Task52 records
    the existing behaviour without changing the response contract. Later tasks
    can expose slices of it once the API is allowed to add fields.
    """

    thread_id: Optional[str] = None
    branch_id: Optional[int] = None
    config: HarnessConfig = field(default_factory=HarnessConfig)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    guardrail_dropped: int = 0
    guardrail_source: Optional[str] = None
    guardrail_violations: List[str] = field(default_factory=list)
    evidence_count: int = 0
    answer_chars: int = 0
    answer_tokens: int = 0
    degradation_reasons: List[str] = field(default_factory=list)
    budget: Optional[Dict[str, Any]] = None
    context: Optional[Dict[str, Any]] = None
    thread_evidence: Optional[Dict[str, Any]] = None
    repair: Optional[Dict[str, Any]] = None

    def record_tool(
        self,
        *,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        status: str = "ok",
        duration_ms: float = 0.0,
        output: Any = None,
        error: Optional[str] = None,
    ) -> ToolCallRecord:
        text = "" if output is None else _text_of(output)
        record = ToolCallRecord(
            name=name,
            arguments=dict(arguments or {}),
            status=status,
            duration_ms=max(0.0, float(duration_ms)),
            output_chars=len(text),
            output_tokens=estimate_tokens(text),
            error=error,
        )
        self.tool_calls.append(record)
        return record

    def record_guardrails(
        self,
        *,
        dropped: int,
        source: str,
        evidence_count: int,
        answer: str,
        violations: Optional[Sequence[str]] = None,
    ) -> None:
        self.guardrail_dropped = int(dropped)
        self.guardrail_source = source
        self.evidence_count = int(evidence_count)
        self.answer_chars = len(answer or "")
        self.answer_tokens = estimate_tokens(answer or "")
        self.guardrail_violations = list(violations or ())

    def record_degradation(self, reason: str) -> None:
        """Record a stable reason code once, preserving first-seen order."""
        if reason and reason not in self.degradation_reasons:
            self.degradation_reasons.append(reason)

    def record_budget(self, snapshot: Mapping[str, Any]) -> None:
        self.budget = dict(snapshot)

    def record_context(self, stats: Mapping[str, Any]) -> None:
        """Remember what the context middleware did on its latest model call."""
        self.context = dict(stats)

    def record_thread_evidence(self, *, count: int, current_turn: int) -> None:
        """Record how much evidence the thread registry holds for this turn."""
        self.thread_evidence = {
            "count": int(count),
            "current_turn": int(current_turn),
        }

    def record_repair(
        self,
        *,
        attempted: bool,
        succeeded: bool,
        residual: Sequence[str] = (),
        reason: Optional[str] = None,
    ) -> None:
        """Record the single structured-report repair attempt (Task57)."""
        self.repair = {
            "attempted": bool(attempted),
            "succeeded": bool(succeeded),
            "residual": list(residual)[:5],
            "reason": reason,
        }

    def finish(self) -> None:
        self.finished_at = datetime.now(timezone.utc)

    def summary(self) -> Dict[str, Any]:
        """Stable aggregate used by logs and the reproducible baseline."""
        by_name: Dict[str, int] = {}
        for call in self.tool_calls:
            by_name[call.name] = by_name.get(call.name, 0) + 1
        failures = sum(1 for call in self.tool_calls if call.status == "error")
        timeouts = sum(1 for call in self.tool_calls if call.status == "timeout")
        refused = sum(
            1 for call in self.tool_calls if call.status == "budget_exhausted"
        )
        elapsed_ms = None
        if self.finished_at is not None:
            elapsed_ms = round(
                (self.finished_at - self.started_at).total_seconds() * 1000, 3
            )
        return {
            "thread_id": self.thread_id,
            "branch_id": self.branch_id,
            "tool_calls": len(self.tool_calls),
            "tool_calls_by_name": by_name,
            "tool_failures": failures,
            "tool_timeouts": timeouts,
            "tool_budget_refusals": refused,
            "total_tool_ms": round(
                sum(call.duration_ms for call in self.tool_calls), 3
            ),
            "total_output_chars": sum(call.output_chars for call in self.tool_calls),
            "estimated_output_tokens": sum(
                call.output_tokens for call in self.tool_calls
            ),
            "evidence_count": self.evidence_count,
            "guardrail_dropped": self.guardrail_dropped,
            "guardrail_source": self.guardrail_source,
            "guardrail_violations": list(self.guardrail_violations),
            "answer_chars": self.answer_chars,
            "answer_tokens": self.answer_tokens,
            "degraded": bool(self.degradation_reasons),
            "degrade_reasons": list(self.degradation_reasons),
            "budget": self.budget,
            "context": self.context,
            "thread_evidence": self.thread_evidence,
            "repair": self.repair,
            "elapsed_ms": elapsed_ms,
        }

    def as_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary(),
            "tools": [call.as_dict() for call in self.tool_calls],
        }


# --------------------------------------------------------------------------- #
# Context management: raw results for the current turn, memos for older ones
# --------------------------------------------------------------------------- #
CONTEXT_MEMO_HEADER = "[历史调查备忘]"
CONTEXT_TRUNCATION_MARKER = "…[context-compacted]"
_MEMO_MAX_CHARS = 2000
_MEMO_ANSWER_MAX_CHARS = 600
_MEMO_TOOL_MAX_CHARS = 700
_MEMO_EVIDENCE_LIMIT = 40
_MEMO_SCALAR_LIMIT = 24
_SHORT_VALUE_CHARS = 80


def _is_blank(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _short(value: Any, limit: int = _SHORT_VALUE_CHARS) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _clip(text: str, limit: int) -> str:
    """Cut ``text`` at ``limit`` characters, keeping a visible marker."""
    if limit <= 0 or len(text) <= limit:
        return text
    keep = max(0, limit - len(CONTEXT_TRUNCATION_MARKER))
    return text[:keep].rstrip() + CONTEXT_TRUNCATION_MARKER


def _message_payload(message: BaseMessage) -> str:
    """Everything a message contributes to the model context, as text."""
    parts = [message_text(message)]
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        parts.append(json.dumps(tool_calls, ensure_ascii=False, default=str))
    return "\n".join(part for part in parts if part)


def _messages_chars(messages: Sequence[BaseMessage]) -> int:
    return sum(len(_message_payload(message)) for message in messages)


def _messages_tokens(messages: Sequence[BaseMessage]) -> int:
    return sum(estimate_tokens(_message_payload(message)) for message in messages)


def _collect_evidence(payload: Any, output: List[tuple], seen: Set[str]) -> None:
    """Collect every ``ref`` record and the scalar facts stored beside it."""
    if isinstance(payload, dict):
        ref = payload.get("ref")
        if isinstance(ref, str) and ref:
            if ref not in seen:
                seen.add(ref)
                fields = [
                    f"{key}={_short(value)}"
                    for key, value in payload.items()
                    if key != "ref"
                    and not _is_blank(value)
                    and isinstance(value, (str, int, float, bool))
                ]
                output.append((ref, fields))
            return
        for value in payload.values():
            _collect_evidence(value, output, seen)
    elif isinstance(payload, list):
        for item in payload:
            _collect_evidence(item, output, seen)


def _collect_scalars(
    payload: Any, output: List[str], prefix: str = "", depth: int = 0
) -> None:
    """Collect bounded scalar fields so numbers outside refs stay visible."""
    if depth > 2 or len(output) >= _MEMO_SCALAR_LIMIT:
        return
    if isinstance(payload, dict):
        if isinstance(payload.get("ref"), str):
            # Already rendered together with its ref by _collect_evidence.
            return
        for key, value in payload.items():
            if len(output) >= _MEMO_SCALAR_LIMIT:
                return
            name = f"{prefix}{key}"
            if isinstance(value, (str, int, float, bool)) and not _is_blank(value):
                if isinstance(value, str) and len(value) > _SHORT_VALUE_CHARS:
                    continue
                output.append(f"{name}={_short(value)}")
            elif isinstance(value, dict):
                _collect_scalars(value, output, prefix=f"{name}.", depth=depth + 1)
            elif (
                isinstance(value, list)
                and len(value) <= 8
                and all(
                    isinstance(item, (str, int, float, bool))
                    for item in value
                )
            ):
                output.append(
                    f"{name}=" + ", ".join(_short(item) for item in value)
                )


def _compact_tool_output(
    content: Any,
    *,
    max_chars: int = _MEMO_TOOL_MAX_CHARS,
    ref_prefix: str = "",
) -> str:
    """Shrink one tool result to its evidence refs and key scalar fields.

    ``ref_prefix`` is used by historical memos: every ref that points into an
    earlier turn is rendered as its thread-stable form (``t1.f0.e1``) so the
    model can cite it unambiguously on a later turn.
    """
    text = content if isinstance(content, str) else _text_of(content)
    payload: Any = None
    if isinstance(content, str):
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            payload = None
    if payload is None:
        return _clip(text, max_chars)

    evidence: List[tuple] = []
    _collect_evidence(payload, evidence, set())
    scalars: List[str] = []
    _collect_scalars(payload, scalars)

    def _display_ref(ref: str) -> str:
        if not ref_prefix or TURN_REF_PATTERN.match(ref):
            return ref
        return f"{ref_prefix}{ref}"

    lines: List[str] = []
    if evidence:
        rendered = " | ".join(
            f"{_display_ref(ref)}: {', '.join(fields)}" if fields else _display_ref(ref)
            for ref, fields in evidence[:_MEMO_EVIDENCE_LIMIT]
        )
        lines.append("证据: " + rendered)
    if scalars:
        lines.append("字段: " + ", ".join(scalars))
    if not lines:
        if isinstance(payload, dict):
            lines.append(_short(payload))
        elif isinstance(payload, list):
            lines.append(f"列表: {len(payload)} 项")
        else:
            lines.append(_short(payload))
    return _clip("\n".join(lines), max_chars)


def _find_final_answer(messages: Sequence[BaseMessage]) -> str:
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        # A structured final answer is a tool call, not prose. Its arguments are
        # the same JSON the pre-Task57 prompt asked for, so the memo keeps the
        # answer and cited refs without a special case downstream.
        for call in getattr(message, "tool_calls", None) or ():
            if (
                isinstance(call, dict)
                and call.get("name") == STRUCTURED_REPORT_TOOL_NAME
                and isinstance(call.get("args"), dict)
            ):
                return json.dumps(call["args"], ensure_ascii=False, default=str)
        if not getattr(message, "tool_calls", None):
            text = message_text(message).strip()
            if text:
                return text
    return ""


def _collect_cited_refs(payload: Any, output: List[str]) -> None:
    if isinstance(payload, dict):
        refs = payload.get("evidence_refs")
        if isinstance(refs, list):
            for ref in refs:
                if isinstance(ref, str) and ref not in output:
                    output.append(ref)
        for value in payload.values():
            _collect_cited_refs(value, output)
    elif isinstance(payload, list):
        for item in payload:
            _collect_cited_refs(item, output)


def _answer_text_and_refs(raw: str) -> tuple:
    """Return the prose the user saw and the refs the structured answer cited."""
    text = raw.strip()
    cited: List[str] = []
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        for key in ("answer", "summary"):
            if isinstance(payload.get(key), str) and payload[key].strip():
                text = payload[key].strip()
                break
        _collect_cited_refs(payload, cited)
    return text, cited


def _memo_for_turn(
    messages: Sequence[BaseMessage], *, turn_index: int = 1
) -> tuple:
    """Build the compact investigation memo for one completed turn."""
    ref_prefix = f"t{int(turn_index)}."
    calls: Dict[Any, tuple] = {}
    for message in messages:
        for call in getattr(message, "tool_calls", None) or ():
            if isinstance(call, dict):
                calls[call.get("id")] = (
                    call.get("name", "tool"),
                    call.get("args") or {},
                )

    blocks: List[str] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        name, args = calls.get(
            getattr(message, "tool_call_id", None),
            (getattr(message, "name", None) or "tool", {}),
        )
        if name == STRUCTURED_REPORT_TOOL_NAME:
            # The report is rendered as this turn's answer below, not as a tool.
            continue
        if not isinstance(args, dict):
            args = {}
        arg_text = ", ".join(f"{key}={_short(value)}" for key, value in args.items())
        status = getattr(message, "status", None) or "ok"
        header = f"- {name}({arg_text}) → {status}" if arg_text else f"- {name} → {status}"
        blocks.append(
            header
            + "\n"
            + _compact_tool_output(message.content, ref_prefix=ref_prefix)
        )

    final_raw = _find_final_answer(messages)
    answer, cited = _answer_text_and_refs(final_raw) if final_raw else ("", [])
    display_answer = qualify_refs_in_text(answer, turn_index)

    lines = [
        f"{CONTEXT_MEMO_HEADER} 第 {int(turn_index)} 轮"
        f"（历史证据 ref 已加 {ref_prefix} 前缀）："
    ]
    lines.extend(blocks or ["- 无工具调用。"])
    if cited:
        lines.append(
            "本轮回答引用: "
            + "、".join(qualify_evidence_ref(ref, turn_index) for ref in cited)
        )
    if display_answer:
        lines.append(
            "本轮回答摘要: "
            + _clip(display_answer, _MEMO_ANSWER_MAX_CHARS)
        )
    return _clip("\n".join(lines), _MEMO_MAX_CHARS), display_answer


def compact_context(
    messages: Sequence[BaseMessage],
    *,
    token_budget: Optional[int] = None,
    char_budget: Optional[int] = None,
) -> tuple:
    """Rewrite the model view: current turn raw, older turns as memos.

    The checkpoint state is never touched -- only the list handed to the model.
    When a budget is exceeded, the oldest memos are dropped first; only then are
    current-turn tool results compacted (evidence refs and their numbers are
    kept). The current question and system prompt are never removed.
    """
    original = list(messages)
    before_tokens = _messages_tokens(original)
    before_chars = _messages_chars(original)

    turns: List[List[BaseMessage]] = []
    current: List[BaseMessage] = []
    for message in original:
        if isinstance(message, HumanMessage) and current:
            turns.append(current)
            current = [message]
        else:
            current.append(message)
    if current:
        turns.append(current)

    prior_turns, current_turn = (turns[:-1], turns[-1]) if turns else ([], [])

    memoized_turns = 0
    compacted_turns: List[List[BaseMessage]] = []
    for turn_index, turn in enumerate(prior_turns, 1):
        memo, answer = _memo_for_turn(turn, turn_index=turn_index)
        human = next(
            (message for message in turn if isinstance(message, HumanMessage)), None
        )
        if human is None:
            compacted_turns.append([HumanMessage(content=memo)])
            memoized_turns += 1
            continue
        human_text = message_text(human).strip()
        combined = f"{human_text}\n\n{memo}" if human_text else memo
        turn_view: List[BaseMessage] = [human.model_copy(update={"content": combined})]
        if answer:
            turn_view.append(AIMessage(content=answer))
        compacted_turns.append(turn_view)
        memoized_turns += 1

    def _flatten(prior: Sequence[Sequence[BaseMessage]]) -> List[BaseMessage]:
        return [message for turn in prior for message in turn] + list(current_turn)

    def _over_budget(prior: Sequence[Sequence[BaseMessage]]) -> bool:
        view = _flatten(prior)
        if token_budget is not None and _messages_tokens(view) > token_budget:
            return True
        if char_budget is not None and _messages_chars(view) > char_budget:
            return True
        return False

    dropped_turns = 0
    while compacted_turns and _over_budget(compacted_turns):
        compacted_turns.pop(0)
        dropped_turns += 1

    if dropped_turns:
        note = f"（更早的 {dropped_turns} 轮调查因上下文预算已省略。）"
        if compacted_turns:
            first_turn = list(compacted_turns[0])
            for index, message in enumerate(first_turn):
                if isinstance(message, HumanMessage):
                    first_turn[index] = message.model_copy(
                        update={"content": f"{note}\n{message_text(message)}"}
                    )
                    break
            compacted_turns[0] = first_turn
        else:
            compacted_turns.append([HumanMessage(content=f"{CONTEXT_MEMO_HEADER} {note}")])

    current_view = list(current_turn)
    truncated_tools = 0
    if _over_budget(compacted_turns):
        for index, message in enumerate(current_view):
            if not isinstance(message, ToolMessage):
                continue
            content = message.content
            if isinstance(content, str) and CONTEXT_TRUNCATION_MARKER in content:
                continue
            compacted = _compact_tool_output(content)
            if compacted != content:
                current_view[index] = message.model_copy(update={"content": compacted})
                truncated_tools += 1

    result = [message for turn in compacted_turns for message in turn] + current_view
    after_tokens = _messages_tokens(result)
    after_chars = _messages_chars(result)

    reasons: List[str] = []
    if dropped_turns:
        reasons.append("context_older_turns_dropped")
    if truncated_tools:
        reasons.append("context_tool_outputs_trimmed")
    if (token_budget is not None and after_tokens > token_budget) or (
        char_budget is not None and after_chars > char_budget
    ):
        reasons.append("context_budget_exceeded")

    stats = {
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
        "before_chars": before_chars,
        "after_chars": after_chars,
        "prior_turns": len(prior_turns),
        "memoized_turns": memoized_turns,
        "dropped_turns": dropped_turns,
        "truncated_tools": truncated_tools,
        "reasons": reasons,
    }
    return result, stats


def build_context_middleware(
    *,
    token_budget: Optional[int] = None,
    char_budget: Optional[int] = None,
    trace: Optional[RunTrace] = None,
):
    """Middleware that bounds the model context without touching state.

    Earlier turns arrive as investigation memos; the current turn keeps its raw
    tool results until a budget forces compaction. ``trace`` only observes.
    """

    async def _diagnosis_context(request, handler):
        messages, stats = compact_context(
            request.messages,
            token_budget=token_budget,
            char_budget=char_budget,
        )
        if trace is not None:
            trace.record_context(stats)
            for reason in stats["reasons"]:
                trace.record_degradation(reason)
        return await handler(request.override(messages=messages))

    return wrap_model_call(_diagnosis_context, name="diagnosis_context")


# --------------------------------------------------------------------------- #
# Runtime: model adapter, guarded tools, agent construction, run entry
# --------------------------------------------------------------------------- #
@dataclass
class ToolCallBudget:
    """Per-turn counters shared by every guarded tool.

    ``consume`` is the enforcement point: once ``used`` reaches ``limit`` every
    further data call is refused and recorded, so the final answer can say the
    investigation was cut short instead of silently stopping. Free tools
    (``FREE_TOOL_NAMES``) are navigation and stay available.
    """

    limit: int = DEFAULT_TOOL_CALLS_LIMIT
    used: int = 0
    exhausted: bool = False
    reason: Optional[str] = None
    calls: List[str] = field(default_factory=list)

    def consume(self, tool_name: str) -> bool:
        """Reserve one budget slot; free tools are always allowed.

        ``resolve_time_range`` is navigation rather than investigation: it is
        recorded in ``calls`` like every other tool, but never increments
        ``used`` and still works after the data budget is exhausted.
        """
        if tool_name in FREE_TOOL_NAMES:
            self.calls.append(tool_name)
            return True
        if self.used >= self.limit:
            self.exhausted = True
            self.reason = self.reason or "tool_call_limit"
            return False
        self.used += 1
        self.calls.append(tool_name)
        return True

    def snapshot(self, recursion_limit: int) -> Dict[str, Any]:
        return {
            "tool_calls_used": self.used,
            "tool_calls_limit": self.limit,
            "recursion_limit": recursion_limit,
            "exhausted": self.exhausted,
            "reason": self.reason,
        }


def _budget_refusal(tool_name: str) -> str:
    return json.dumps(
        {
            "status": "budget_exhausted",
            "tool": tool_name,
            "message": "本次调查的工具调用次数已用尽。请基于已有结果收束回答，并说明尚未检查的内容。",
        },
        ensure_ascii=False,
    )


def _tool_timeout(tool_name: str) -> str:
    """Observation returned in place of a result that took too long.

    The turn continues: the model sees that this probe was skipped and can
    answer from what it already found instead of failing the whole request.
    The duration stays in the trace rather than the model-visible text, so the
    answer guardrail never sees a number the number whitelist cannot account
    for (the tool was cancelled before it could report anything).
    """
    return json.dumps(
        {
            "status": "timeout",
            "tool": tool_name,
            "message": (
                "工具执行超时，已跳过。"
                "请基于已有结果回答，并说明哪些内容尚未检查。"
            ),
        },
        ensure_ascii=False,
    )


def _tool_failure(tool_name: str, error: Exception) -> str:
    """Observation returned when a tool raised, so one failure degrades one probe."""
    return json.dumps(
        {
            "status": "error",
            "tool": tool_name,
            "message": f"{type(error).__name__}: {error}",
        },
        ensure_ascii=False,
    )


def guard_tools(
    tools: Sequence[BaseTool],
    budget: ToolCallBudget,
    trace: Optional[RunTrace] = None,
    timeout_seconds: Optional[float] = None,
) -> List[BaseTool]:
    """Return the same tools with a shared call budget and per-tool timeout.

    The schema and description are preserved so ``bind_tools`` still exposes an
    identical contract to the model; only execution is wrapped. When ``trace``
    is supplied, every attempt -- including a budget refusal, a timeout or a
    failure -- is recorded with its duration and output size.

    Timeouts and exceptions become JSON observations instead of raised errors:
    one slow or broken probe must degrade that tool, not the whole answer. The
    graph then keeps its partial evidence and the model explains the gap.
    ``timeout_seconds`` is opt-in; ``None`` means no deadline.
    """

    guarded: List[BaseTool] = []

    for tool in tools:
        async def _call(_tool=tool, **kwargs: Any) -> str:
            started = time.perf_counter()
            if not budget.consume(_tool.name):
                refusal = _budget_refusal(_tool.name)
                if trace is not None:
                    trace.record_tool(
                        name=_tool.name,
                        arguments=kwargs,
                        status="budget_exhausted",
                        duration_ms=(time.perf_counter() - started) * 1000,
                        output=refusal,
                    )
                return refusal
            try:
                async with asyncio.timeout(timeout_seconds):
                    output = await _tool.ainvoke(kwargs)
            except TimeoutError as error:
                observation = _tool_timeout(_tool.name)
                if trace is not None:
                    trace.record_tool(
                        name=_tool.name,
                        arguments=kwargs,
                        status="timeout",
                        duration_ms=(time.perf_counter() - started) * 1000,
                        output=observation,
                        error=f"{type(error).__name__}: tool exceeded {timeout_seconds}s",
                    )
                    trace.record_degradation("tool_timeout")
                return observation
            except Exception as error:
                observation = _tool_failure(_tool.name, error)
                if trace is not None:
                    trace.record_tool(
                        name=_tool.name,
                        arguments=kwargs,
                        status="error",
                        duration_ms=(time.perf_counter() - started) * 1000,
                        output=observation,
                        error=f"{type(error).__name__}: {error}",
                    )
                    trace.record_degradation("tool_failed")
                return observation
            if trace is not None:
                trace.record_tool(
                    name=_tool.name,
                    arguments=kwargs,
                    status="ok",
                    duration_ms=(time.perf_counter() - started) * 1000,
                    output=output,
                )
            return output

        guarded.append(
            StructuredTool(
                name=tool.name,
                description=tool.description,
                args_schema=tool.args_schema,
                coroutine=_call,
            )
        )

    return guarded


def build_diagnosis_chat_model():
    """Build a LangChain chat model from the app's existing provider settings.

    Imported lazily so the app still starts when the optional provider package
    is absent; in that case the caller degrades to the deterministic analysis.
    """
    provider = (settings.LLM_PROVIDER or "ollama").strip().lower()

    if provider in OPENAI_PROVIDER_ALIASES:
        if not settings.OPENAI_API_KEY:
            logger.warning(
                "LLM_PROVIDER=openai but OPENAI_API_KEY is empty; "
                "the diagnosis agent will use the deterministic fallback."
            )
            return None
        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            logger.warning(
                "langchain-openai is not installed; the diagnosis agent will "
                "use the deterministic fallback."
            )
            return None
        return ChatOpenAI(
            model=settings.OPENAI_MODEL,
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
            temperature=0.1,
            timeout=45,
            max_retries=1,
        )

    try:
        from langchain_ollama import ChatOllama
    except ImportError:
        logger.warning(
            "langchain-ollama is not installed; the diagnosis agent will "
            "use the deterministic fallback."
        )
        return None
    return ChatOllama(
        model=settings.OLLAMA_MODEL,
        base_url=settings.OLLAMA_BASE_URL,
        temperature=0.1,
        num_ctx=settings.OLLAMA_NUM_CTX,
    )


def build_diagnosis_agent(
    model,
    tools: Sequence[BaseTool],
    budget: ToolCallBudget,
    checkpointer=None,
    trace: Optional[RunTrace] = None,
    tool_timeout_seconds: Optional[float] = None,
    middleware: Optional[Sequence[Any]] = None,
    context_token_budget: Optional[int] = None,
    context_char_budget: Optional[int] = None,
):
    """Compile the generic ReAct graph with guarded tools.

    ``model`` may be the app's provider model or a scripted model in tests; the
    graph construction is identical either way. ``trace`` only observes tool
    execution and never changes their results. Context compaction is attached
    by default; callers may pass their own ``middleware`` instead. The thread
    evidence hook is always attached so every completed turn persists its
    registry into the checkpoint.
    """
    if middleware is None:
        middleware = [
            build_context_middleware(
                token_budget=context_token_budget,
                char_budget=context_char_budget,
                trace=trace,
            )
        ]
    middleware = [*middleware, build_thread_evidence_middleware()]
    return create_agent(
        model=model,
        tools=guard_tools(
            tools,
            budget,
            trace=trace,
            timeout_seconds=tool_timeout_seconds,
        ),
        system_prompt=AGENT_SYSTEM_PROMPT,
        middleware=list(middleware),
        response_format=AgentInvestigationReport,
        state_schema=DiagnosisAgentState,
        checkpointer=checkpointer,
        name="diagnosis_investigator",
    )


def message_text(message: BaseMessage) -> str:
    """Extract plain text from a message whose content may be blocks."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content or "")


@dataclass
class AgentTurn:
    """What one agent invocation produced, including a cut-short trace."""

    messages: List[BaseMessage] = field(default_factory=list)
    answer: str = ""
    error: Optional[str] = None
    thread_evidence: List[Dict[str, Any]] = field(default_factory=list)
    turn_index: int = 1
    structured: Optional[Dict[str, Any]] = None


async def run_agent_turn(
    agent,
    *,
    question: str,
    thread_id: str,
    recursion_limit: int = DEFAULT_RECURSION_LIMIT,
) -> AgentTurn:
    """Invoke the compiled agent once and return its final messages.

    If LangGraph hits the recursion limit, the checkpointer's last state is
    fetched so the partial investigation is not lost -- the caller can still
    answer with what was found before the cap.
    """
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
    }
    try:
        result = await agent.ainvoke(
            {"messages": [("user", question)]},
            config=config,
        )
        messages = result.get("messages", [])
        structured = report_to_dict(result.get("structured_response"))
        if structured is not None:
            # A structured answer ends the graph with the report tool's
            # ToolMessage, which is a normal completion, not a step cap. The
            # serialized report is the answer the guardrails inspect.
            return AgentTurn(
                messages=messages,
                answer=json.dumps(structured, ensure_ascii=False, default=str),
                error=None,
                thread_evidence=_thread_evidence_from_result(result, messages),
                turn_index=_thread_turn_index(messages),
                structured=structured,
            )
        stopped = _stopped_before_final_answer(messages)
        return AgentTurn(
            messages=messages,
            # LangGraph's step-limit message is not an answer; leave the prose
            # empty so the caller uses the deterministic summary instead.
            answer="" if stopped else _last_ai_text(messages),
            error=stopped,
            thread_evidence=_thread_evidence_from_result(result, messages),
            turn_index=_thread_turn_index(messages),
        )
    except GraphRecursionError:
        logger.warning("Diagnosis agent hit recursion_limit=%s.", recursion_limit)
        messages: List[BaseMessage] = []
        thread_evidence: List[Dict[str, Any]] = []
        try:
            state = await agent.aget_state(config)
            messages = state.values.get("messages", [])
            thread_evidence = state.values.get("thread_evidence") or []
        except Exception:  # pragma: no cover - no checkpointer / no state
            pass
        if not thread_evidence:
            thread_evidence = collect_thread_evidence(messages)
        return AgentTurn(
            messages=messages,
            answer=_last_ai_text(messages),
            error="recursion_limit",
            thread_evidence=list(thread_evidence),
            turn_index=_thread_turn_index(messages),
        )


def _thread_turn_index(messages: Sequence[BaseMessage]) -> int:
    """The 1-based turn the latest ``HumanMessage`` opened."""
    return max(1, sum(1 for message in messages if isinstance(message, HumanMessage)))


def _thread_evidence_from_result(
    result: Mapping[str, Any], messages: Sequence[BaseMessage]
) -> List[Dict[str, Any]]:
    """Read the persisted registry, rebuilding it when the hook did not run."""
    evidence = result.get("thread_evidence")
    if isinstance(evidence, list) and evidence:
        return list(evidence)
    return collect_thread_evidence(messages)


def _stopped_before_final_answer(messages: Sequence[BaseMessage]) -> Optional[str]:
    """Whether the graph stopped at its step cap before producing a final answer.

    Newer LangGraph versions stop a ReAct graph at ``recursion_limit`` by
    returning the state instead of raising ``GraphRecursionError``. Depending on
    parity the returned state ends on the last executed ``ToolMessage``, on the
    pending ``AIMessage``, or on a synthesized "Sorry, need more steps..." AI
    message. A completed turn always ends in a model-authored ``AIMessage``
    without tool calls, so anything else means the cap was hit. Without this
    check a cut-short investigation would be reported as a normal answer with
    empty prose.
    """
    if not messages:
        return None
    last = messages[-1]
    if isinstance(last, AIMessage) and not getattr(last, "tool_calls", None):
        if _STEP_LIMIT_SENTINEL in message_text(last).lower():
            return "recursion_limit"
        return None
    return "recursion_limit"


def _last_ai_text(messages: Sequence[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not getattr(message, "tool_calls", None):
            text = message_text(message).strip()
            if text:
                return text
    return ""


@dataclass
class HarnessTurn:
    """Result of one harness turn: model availability, budget and the agent turn."""

    model_available: bool
    budget: ToolCallBudget
    turn: Optional[AgentTurn] = None
    model: Any = None


async def run_harness_turn(
    *,
    tools: Sequence[BaseTool],
    question: str,
    thread_id: str,
    config: Optional[HarnessConfig] = None,
    trace: Optional[RunTrace] = None,
    checkpointer=None,
    model: Any = None,
    model_factory: Optional[Callable[[], Any]] = None,
) -> HarnessTurn:
    """Build (if needed) and run one bounded investigation turn.

    Model construction, the shared tool budget, guarded-tool registration, the
    checkpointer and optional timeouts all live here. The caller decides what
    an unavailable model or an exception means for the HTTP response.
    """
    config = config or HarnessConfig()
    if model is None:
        factory = model_factory or build_diagnosis_chat_model
        try:
            model = factory()
        except Exception as error:  # configuration must not become a 500
            logger.warning("Could not build the diagnosis chat model: %s", error)
            model = None

    budget = ToolCallBudget(limit=config.tool_calls_limit)
    if model is None:
        return HarnessTurn(model_available=False, budget=budget, model=None)

    agent = build_diagnosis_agent(
        model,
        tools,
        budget,
        checkpointer=checkpointer,
        trace=trace,
        tool_timeout_seconds=config.tool_timeout_seconds,
        context_token_budget=config.context_token_budget,
        context_char_budget=config.context_char_budget,
    )
    try:
        async with asyncio.timeout(config.turn_timeout_seconds):
            turn = await run_agent_turn(
                agent,
                question=question,
                thread_id=thread_id,
                recursion_limit=config.recursion_limit,
            )
    except TimeoutError:
        if trace is not None:
            trace.record_degradation("turn_timeout")
        raise
    return HarnessTurn(
        model_available=True,
        budget=budget,
        turn=turn,
        model=model,
    )


__all__ = [
    "AGENT_SYSTEM_PROMPT",
    "CONTEXT_MEMO_HEADER",
    "DEFAULT_RECURSION_LIMIT",
    "DEFAULT_TOOL_CALLS_LIMIT",
    "FREE_TOOL_NAMES",
    "REF_TOKEN_PATTERN",
    "TURN_REF_PATTERN",
    "AgentTurn",
    "DiagnosisAgentState",
    "HarnessConfig",
    "HarnessTurn",
    "RunTrace",
    "ThreadEvidenceRegistry",
    "ToolCallBudget",
    "ToolCallRecord",
    "build_context_middleware",
    "build_diagnosis_agent",
    "build_diagnosis_chat_model",
    "build_thread_evidence_middleware",
    "collect_thread_evidence",
    "compact_context",
    "estimate_tokens",
    "evidence_records_from_items",
    "guard_tools",
    "load_thread_evidence",
    "merge_thread_evidence",
    "message_text",
    "qualify_evidence_ref",
    "qualify_refs_in_text",
    "run_agent_turn",
    "run_harness_turn",
    "split_evidence_ref",
]
