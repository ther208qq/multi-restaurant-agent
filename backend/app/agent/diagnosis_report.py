"""Structured final report for the investigation Agent (Task57).

The Agent submits its answer through ``create_agent(response_format=...)``.
LangChain turns the Pydantic model into a tool the provider must call, parses
the tool-call arguments and puts the validated object in the graph state's
``structured_response`` channel. That makes the report shape a provider-level
contract instead of something parsed out of free-form text.

The schema here is deliberately the same shape the HTTP response already uses
(``answer`` plus four claim buckets), so structured output adds no wire fields.
The guardrails in :mod:`backend.app.agent.diagnosis` still run on the parsed
report: a syntactically valid report can still cite a forged ref or number.

When a claim is dropped the orchestrator may ask the model for one repair. The
prompt builder below gives it the rejected text, the exact reasons, and the
complete list of refs and numbers it is allowed to use.
"""

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from pydantic import BaseModel, Field

REPAIR_SYSTEM_PROMPT = """你是经营诊断报告的证据校验修复器。
你会收到上一版结构化报告和逐条校验失败原因。你的任务是在不新增事实的前提下修正它：
- 只能引用用户消息中列出的 evidence_ref；
- 只能使用用户消息中列出的数字，不要自行计算；
- 推测没有证据时放进 hypotheses，不要写 evidence_ref；
- 不得把原因归咎于系统没有采集的数据（天气、顾客心情、竞品活动等）；
- 保持 JSON 结构不变，只输出一个 JSON 对象，不要 Markdown 代码围栏或解释。"""

_MAX_PREVIOUS_CHARS = 8000
_MAX_REF_LINES = 120
_MAX_NUMBER_LINES = 120


class AgentClaim(BaseModel):
    """A sentence backed by one or more evidence refs."""

    text: str = Field(
        ...,
        min_length=1,
        description="一句基于工具证据的结论，不要包含证据之外的数字。",
    )
    evidence_refs: List[str] = Field(
        default_factory=list,
        description="该结论引用的 evidence_ref，必须来自工具结果，如 f0.e1 或 t1.f0.e1。",
    )


class AgentHypothesis(BaseModel):
    """An explanation the data cannot verify."""

    text: str = Field(
        ...,
        min_length=1,
        description="没有证据支持的解释；不要填写 evidence_ref，系统会标注“未经证实”。",
    )


class AgentInvestigationReport(BaseModel):
    """The investigation Agent's final report.

    ``answer`` is the prose shown to the staff member. The four buckets keep
    facts, evidence-backed inferences, unverified hypotheses and recommended
    checks separate so the UI never presents a guess as a finding.
    """

    answer: str = Field(
        default="",
        description="给用户看的中文回答，说明实际调查的区间，并引用 evidence_ref。",
    )
    facts: List[AgentClaim] = Field(
        default_factory=list,
        description="工具直接给出的事实。",
    )
    inferences: List[AgentClaim] = Field(
        default_factory=list,
        description="有证据支持的推断。",
    )
    hypotheses: List[AgentHypothesis] = Field(
        default_factory=list,
        description="没有证据的解释，系统会标注“未经证实”。",
    )
    checks: List[AgentClaim] = Field(
        default_factory=list,
        description="建议继续核查的事项，必须引用证据。",
    )


# The tool name LangChain derives from the schema class; used to recognise the
# structured final answer in the checkpoint and in historical memos.
STRUCTURED_REPORT_TOOL_NAME = AgentInvestigationReport.__name__


def report_to_dict(report: Any) -> Optional[Dict[str, Any]]:
    """Normalise a structured response to a plain JSON-safe dict."""
    if report is None:
        return None
    if isinstance(report, Mapping):
        return dict(report)
    dump = getattr(report, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except TypeError:  # pragma: no cover - non-Pydantic model_dump
            return dump()
    return None


def is_report_payload(payload: Any) -> bool:
    """Whether a parsed object looks like a report rather than some other JSON."""
    return isinstance(payload, Mapping) and any(
        key in payload
        for key in ("answer", "facts", "inferences", "hypotheses", "checks")
    )


def _number_label(value: float) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return str(round(number, 4))


def build_repair_prompt(
    *,
    question: str,
    previous: Any,
    problems: Sequence[str],
    refs: Sequence[str],
    allowed: Sequence[float],
) -> str:
    """One repair instruction: what was rejected, why, and what is allowed."""
    previous_dict = report_to_dict(previous) or previous
    try:
        previous_text = json.dumps(previous_dict, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        previous_text = str(previous_dict)
    if len(previous_text) > _MAX_PREVIOUS_CHARS:
        previous_text = previous_text[:_MAX_PREVIOUS_CHARS] + "…（已截断）"

    ref_lines = [str(ref) for ref in refs if str(ref)][:_MAX_REF_LINES]
    number_lines = [
        _number_label(number) for number in sorted(set(allowed), key=abs)
    ][:_MAX_NUMBER_LINES]
    problem_lines = [str(problem) for problem in problems if str(problem)] or [
        "回答未通过证据校验。"
    ]

    return (
        f"用户问题：{question}\n\n"
        f"上一版报告（JSON）：\n{previous_text}\n\n"
        "校验失败原因（必须逐条修正）：\n- "
        + "\n- ".join(problem_lines)
        + "\n\n可用 evidence_ref（只能引用这些）：\n"
        + ("、".join(ref_lines) if ref_lines else "（本轮没有可引用的证据）")
        + "\n\n可用数字（只能引用这些，不要计算新数字）：\n"
        + ("、".join(number_lines) if number_lines else "（无）")
        + "\n\n请输出修正后的完整报告 JSON："
        '{"answer": "...", "facts": [{"text": "...", "evidence_refs": ["..."]}], '
        '"inferences": [{"text": "...", "evidence_refs": ["..."]}], '
        '"hypotheses": [{"text": "..."}], '
        '"checks": [{"text": "...", "evidence_refs": ["..."]}]}'
    )


__all__ = [
    "AgentClaim",
    "AgentHypothesis",
    "AgentInvestigationReport",
    "REPAIR_SYSTEM_PROMPT",
    "STRUCTURED_REPORT_TOOL_NAME",
    "build_repair_prompt",
    "is_report_payload",
    "report_to_dict",
]
