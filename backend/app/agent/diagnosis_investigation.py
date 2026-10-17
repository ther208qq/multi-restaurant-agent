"""Diagnosis investigation orchestration (Task44).

The HTTP route stays thin; this module owns the sequence that makes the Agent
safe to expose:

1. **Authorise before anything is read.** A new investigation resolves the
   branch through the shared staff-scope rule. A resumed one parses the thread
   id, checks the caller owns it, and refuses a branch switch -- so a guessed
   thread id can never read another branch's investigation.
2. **Run the bounded ReAct graph** with tools built for exactly this staff
   member, branch and database session. Follow-up turns reuse the checkpointer
   keyed by ``thread_id``; the model sees the earlier conversation, the tools
   never see a branch id from the model.
3. **Degrade, never 500.** If no chat model is configured or the provider fails,
   the endpoint still answers from the deterministic diagnosis and says so.
4. **Return the scaffold.** The deterministic payload, facts/inferences/checks
   report, evidence list, tool trace and budget are returned alongside the
   Agent's prose, so every conclusion can be audited.
"""

import json
import logging
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage

from backend.app.agent.diagnosis import (
    asserts_forbidden_cause,
    collect_numbers,
    deterministic_facts,
    numbers_in_text,
    rule_causes,
    rule_checks,
    validate_claims,
    validate_hypotheses,
)
from backend.app.agent.diagnosis_report import (
    REPAIR_SYSTEM_PROMPT,
    build_repair_prompt,
    is_report_payload,
)
from backend.app.agent.diagnosis_tools import (
    TOOL_NAMES,
    DiagnosisToolContext,
    build_diagnosis_tools,
    resolve_investigation_window,
)
from backend.app.agent.harness import (
    DEFAULT_RECURSION_LIMIT,
    DEFAULT_TOOL_CALLS_LIMIT,
    REF_TOKEN_PATTERN,
    HarnessConfig,
    RunTrace,
    ThreadEvidenceRegistry,
    ToolCallBudget,
    build_diagnosis_chat_model,
    collect_thread_evidence,
    evidence_records_from_items,
    load_thread_evidence,
    merge_thread_evidence,
    message_text,
    qualify_evidence_ref,
    run_harness_turn,
)
from backend.app.agent.llm import extract_json_from_text
from backend.app.agent.time_range import resolve_explicit_range
from backend.app.config import settings
from backend.app.exceptions import ForbiddenError, NotFoundError, ValidationError
from backend.app.models.user import StaffUser
from backend.app.schemas.diagnostics import DiagnosisInvestigationRequest
from backend.app.service.branch import BranchService
from backend.app.service.diagnostics import DiagnosisService
from backend.app.service.scope import resolve_staff_branch
from backend.app.service.stats import StatsService

logger = logging.getLogger("diagnosis_investigation")

THREAD_PATTERN = re.compile(r"^diag-(\d+)-(\d+)-([0-9a-f]{32})$")
DATE_TOOLS = {
    "get_diagnosis",
    "get_daily_series",
    "get_business_profile",
    "get_dish_daily",
    "compare_peers",
    "get_operational_changes",
}


def new_thread_id(staff_id: int, branch_id: int) -> str:
    return f"diag-{staff_id}-{branch_id}-{uuid4().hex}"


def parse_thread_id(thread_id: str) -> tuple:
    """Return ``(staff_id, branch_id)`` or 404 for an opaque/invalid id."""
    match = THREAD_PATTERN.match(thread_id or "")
    if not match:
        raise NotFoundError("调查线程不存在。")
    return int(match.group(1)), int(match.group(2))


def _requested_window(
        start: Optional[date], end: Optional[date]
) -> Optional[Dict[str, Any]]:
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise ValidationError("start_date 与 end_date 需要同时提供。")
    if start > end:
        raise ValidationError("开始日期不能晚于结束日期。")
    return resolve_explicit_range(start, end)


def _window_from_steps(steps: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Infer the window from the first dated tool call if no resolver ran."""
    for step in steps:
        if step.get("tool") not in DATE_TOOLS:
            continue
        arguments = step.get("arguments") or {}
        try:
            start = date.fromisoformat(str(arguments.get("start_date")))
            end = date.fromisoformat(str(arguments.get("end_date")))
        except (TypeError, ValueError):
            continue
        return resolve_explicit_range(start, end, source="agent")
    return None


def _diagnosis_evidence(diagnosis: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        dict(item)
        for finding in diagnosis.get("findings", [])
        for item in finding.get("evidence", [])
        if item.get("ref")
    ]


def _merge_evidence(*groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen = set()
    for group in groups:
        for item in group:
            ref = item.get("ref")
            if not ref or ref in seen:
                continue
            seen.add(ref)
            merged.append(item)
    return merged


def _report(diagnosis: Dict[str, Any]) -> Dict[str, Any]:
    """The deterministic report scaffold -- no second LLM call.

    The Agent's prose is the answer; these sections are the checked facts and
    rule-based next steps, so the UI can show structure even when the model
    contributed only wording.
    """
    findings = diagnosis.get("findings") or []
    return {
        "status": (
            "insufficient_data"
            if diagnosis.get("status") == "insufficient_data"
            else "ok"
        ),
        "source": "rule",
        "dropped": 0,
        "facts": deterministic_facts(diagnosis),
        "inferences": rule_causes(diagnosis) if findings else [],
        "checks": rule_checks(diagnosis),
    }


def _fallback_answer(diagnosis: Dict[str, Any], report: Dict[str, Any]) -> str:
    period = diagnosis.get("current_period") or {}
    parts = [
        f"{diagnosis.get('branch_name', '本店')} "
        f"{period.get('start_date')} 至 {period.get('end_date')}："
        f"订单 {period.get('order_count', 0)} 单，"
        f"营业额 {period.get('revenue', 0)} 元，"
        f"客单价 {period.get('average_order_value', 0)} 元。"
    ]
    findings = diagnosis.get("findings") or []
    if findings:
        parts.append(
            "检测到异常："
            + "；".join(
                f"{finding['label']} {finding['change_pct']}%"
                for finding in findings[:5]
            )
            + "。"
        )
    elif diagnosis.get("status") == "insufficient_data":
        notes = diagnosis.get("notes") or []
        if notes:
            parts.append("无法完成判断：" + "；".join(notes[:3]))
    causes = [claim["text"] for claim in report.get("inferences", [])]
    if causes:
        parts.append("规则分析：" + "；".join(causes[:3]))
    checks = [claim["text"] for claim in report.get("checks", [])]
    if checks:
        parts.append("建议核查：" + "；".join(checks[:3]))
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Guardrails on the Agent's final answer (Task45)
# --------------------------------------------------------------------------- #
_REF_TOKEN = REF_TOKEN_PATTERN
_NUMBER_TOLERANCE = 0.011
_CAUSAL_MARKER = re.compile(r"因为|由于|导致|原因|归因")


def _number_known(number: float, allowed: set) -> bool:
    return any(abs(number - candidate) <= _NUMBER_TOLERANCE for candidate in allowed)


def _collect_calendar_numbers(value: Any, out: set) -> None:
    """Year/month/day components of observed dates.

    Tool results carry real ``date`` objects (a daily point, an audit timestamp).
    The model may legitimately quote "10 月 12 日", so those calendar numbers
    must be in the whitelist even though ``collect_numbers`` ignores dates.
    """
    if isinstance(value, date):
        out.update({float(value.year), float(value.month), float(value.day)})
    elif isinstance(value, dict):
        for item in value.values():
            _collect_calendar_numbers(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_calendar_numbers(item, out)


def _allowed_numbers(
        diagnosis: Dict[str, Any],
        context: DiagnosisToolContext,
        window: Dict[str, Any],
        question: str,
        thread_evidence: Optional[List[Dict[str, Any]]] = None,
) -> set:
    """Every number the Agent could legitimately quote.

    Tool observations and the deterministic payload are the source of truth;
    the window's calendar numbers and span are added because "最近 7 天" or
    "10 月 10 日" are facts the system itself supplied, and the question's own
    numbers may be echoed back. Earlier turns' evidence is added too: a
    cross-turn citation (``t1.f0.e1``) may quote a number that is no longer in
    the current turn's tool results.
    """
    numbers = collect_numbers(diagnosis)
    numbers |= collect_numbers(context.observations)
    numbers |= collect_numbers(context.evidence)
    _collect_calendar_numbers(context.observations, numbers)
    _collect_calendar_numbers(context.evidence, numbers)
    if thread_evidence:
        numbers |= collect_numbers(thread_evidence)
        for record in thread_evidence:
            for key in ("label", "detail"):
                value = record.get(key)
                if isinstance(value, str):
                    numbers.update(numbers_in_text(value))
            value = record.get("value")
            if isinstance(value, str):
                numbers.update(numbers_in_text(value))
    numbers.add(
        float((window["end_date"] - window["start_date"]).days + 1)
    )
    for key in (
            "start_date",
            "end_date",
            "baseline_start_date",
            "baseline_end_date",
    ):
        day = window.get(key)
        if isinstance(day, date):
            numbers.update({float(day.year), float(day.month), float(day.day)})
    numbers.update(numbers_in_text(question))
    return numbers


def _answer_violations(
        answer: str, refs: Dict[str, Any], allowed: set
) -> List[str]:
    """Reasons the prose answer cannot be shown as-is.

    Structured claims are validated separately; this catches the free-form
    ``answer`` quoting an invented number or citing a ref no tool produced.
    """
    problems: List[str] = []
    unknown_refs = [
        ref for ref in _REF_TOKEN.findall(answer) if ref not in refs
    ]
    if unknown_refs:
        problems.append("引用了不存在的证据 " + "、".join(sorted(set(unknown_refs))[:3]))
    without_refs = _REF_TOKEN.sub(" ", answer)
    unknown_numbers = [
        number
        for number in numbers_in_text(without_refs)
        if not _number_known(number, allowed)
    ]
    if unknown_numbers:
        problems.append(
            "出现证据之外的数字 " + "、".join(str(n) for n in unknown_numbers[:3])
        )
    # The claims are already screened one by one; this catches the same bad
    # attribution hiding in the prose. A bare mention of "周末" is fine (the
    # user may have asked about it); only a causal sentence is rejected.
    if _CAUSAL_MARKER.search(answer) and asserts_forbidden_cause(answer):
        problems.append("把原因归咎于系统没有采集的数据")
    return problems


def _thread_registry(
        thread_evidence: Optional[List[Dict[str, Any]]],
        evidence: List[Dict[str, Any]],
        turn_index: int,
) -> ThreadEvidenceRegistry:
    """Combine persisted thread evidence with this turn's tool evidence.

    The checkpoint registry already contains the current turn when the
    after-agent hook ran; adding the current items again is harmless because
    the registry merges by qualified ref. It also covers a checkpoint written
    before the registry channel existed, and the direct-call tests that build a
    context without any persisted state.
    """
    records = list(thread_evidence or [])
    records.extend(evidence_records_from_items(evidence, turn_index))
    return ThreadEvidenceRegistry(records, current_turn=turn_index)


def _with_thread_refs(
        evidence: List[Dict[str, Any]], turn_index: int
) -> List[Dict[str, Any]]:
    """Add the stable thread handle beside each local evidence ref."""
    qualified: List[Dict[str, Any]] = []
    for item in evidence:
        copy = dict(item)
        ref = copy.get("ref")
        if isinstance(ref, str) and ref:
            copy["thread_ref"] = qualify_evidence_ref(ref, turn_index)
        qualified.append(copy)
    return qualified


def _compose_answer_and_report(
        *,
        agent_answer: str,
        diagnosis: Dict[str, Any],
        context: DiagnosisToolContext,
        budget: ToolCallBudget,
        window: Dict[str, Any],
        question: str,
        trace: Optional[RunTrace] = None,
        thread_evidence: Optional[List[Dict[str, Any]]] = None,
        turn_index: int = 1,
        structured: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate the Agent's JSON claims and fill any gaps with the rules.

    The model writes an ``answer`` plus four claim lists. Each claim is accepted
    only when it cites a real ref and quotes only numbers present in tool
    results; hypotheses are forced into the unverified bucket. Sections that
    yield nothing fall back to the deterministic facts / rule causes / rule
    checks, so the response is never empty and ``source`` truthfully says
    whether the model contributed anything.

    ``structured`` is the parsed ``response_format`` object when the graph
    produced one (Task57); ``agent_answer`` is then only the serialized
    fallback. ``problems`` in the result lists one reason per dropped claim so
    the orchestrator can ask the model for a single repair.

    ``thread_evidence`` is the thread's persisted registry. It lets a claim
    cite an earlier turn's fact with its qualified ref (``t1.f0.e1``) while the
    current turn keeps using the short local names its tools returned.
    """
    evidence = _merge_evidence(context.evidence, _diagnosis_evidence(diagnosis))
    registry = _thread_registry(thread_evidence, evidence, turn_index)
    refs = registry.accepted_refs()
    allowed = _allowed_numbers(
        diagnosis, context, window, question, thread_evidence=thread_evidence
    )
    notes: List[str] = []
    problems: List[str] = []

    if structured is None:
        parsed = extract_json_from_text(agent_answer or "")
        structured = parsed if is_report_payload(parsed) else None
    else:
        structured = dict(structured) if is_report_payload(structured) else None
    model_report = dict(structured) if structured is not None else None

    facts: List[Dict[str, Any]] = []
    inferences: List[Dict[str, Any]] = []
    checks: List[Dict[str, Any]] = []
    hypotheses: List[Dict[str, Any]] = []
    dropped = 0

    if structured is not None:
        answer = str(
            structured.get("answer") or structured.get("summary") or ""
        ).strip()
        fact_problems: List[str] = []
        inference_problems: List[str] = []
        check_problems: List[str] = []
        hypothesis_problems: List[str] = []
        facts, d_facts = validate_claims(
            structured.get("facts"),
            refs=refs,
            allowed=allowed,
            problems=fact_problems,
        )
        inferences, d_inf = validate_claims(
            structured.get("inferences"),
            refs=refs,
            allowed=allowed,
            problems=inference_problems,
        )
        checks, d_checks = validate_claims(
            structured.get("checks"),
            refs=refs,
            allowed=allowed,
            problems=check_problems,
        )
        kept_hypotheses, d_hyp = validate_hypotheses(
            structured.get("hypotheses"),
            allowed=allowed,
            problems=hypothesis_problems,
        )
        problems.extend(f"【事实】{item}" for item in fact_problems)
        problems.extend(f"【推断】{item}" for item in inference_problems)
        problems.extend(f"【核查】{item}" for item in check_problems)
        problems.extend(f"【未经证实】{item}" for item in hypothesis_problems)
        hypotheses = [
            {
                "text": item["text"],
                "status": "unverified",
                "evidence_refs": [],
                "note": "",
            }
            for item in kept_hypotheses
        ]
        agent_claims_kept = bool(facts or inferences or checks or hypotheses)
        dropped = d_facts + d_inf + d_checks + d_hyp
        if not answer:
            answer = ""
        if dropped:
            notes.append(
                f"有 {dropped} 条模型结论因引用或数字无法验证而被丢弃。"
            )
    else:
        agent_claims_kept = False
        answer = (agent_answer or "").strip()
        if answer:
            notes.append(
                "模型没有输出结构化结论；报告使用确定性事实与规则分析，"
                "回答文字已单独校验。"
            )

    if not facts:
        facts = deterministic_facts(diagnosis)
    if not inferences:
        inferences = rule_causes(diagnosis) if diagnosis.get("findings") else []
    if not checks:
        checks = rule_checks(diagnosis)
    if budget.exhausted:
        missing = [name for name in TOOL_NAMES if name not in budget.calls]
        checks = list(checks) + [
            {
                "text": (
                        "本次调查因工具调用或循环上限提前结束，未执行："
                        + ("、".join(missing) if missing else "无")
                        + "；相关结论需要人工核查。"
                ),
                "evidence_refs": [],
            }
        ]

    report = {
        "status": (
            "insufficient_data"
            if diagnosis.get("status") == "insufficient_data"
            else "ok"
        ),
        "source": "mixed" if agent_claims_kept else "rule",
        "dropped": dropped,
        "facts": facts,
        "inferences": inferences,
        "checks": checks,
    }

    model_answer_missing = not answer
    if not answer:
        answer = _fallback_answer(diagnosis, report)
        notes.append("模型没有返回可用的文字结论，已使用确定性诊断摘要。")

    violations = _answer_violations(answer, refs, allowed)
    problems.extend(f"【回答】{item}" for item in violations)
    if violations:
        notes.append(
            "模型回答未通过证据校验（" + "；".join(violations) + "），"
                                                               "已替换为受控的确定性摘要。"
        )
        answer = _fallback_answer(diagnosis, report)

    if trace is not None:
        trace.record_guardrails(
            dropped=dropped,
            source=report["source"],
            evidence_count=len(evidence),
            answer=answer,
            violations=violations,
        )
        if model_answer_missing:
            trace.record_degradation("model_answer_missing")
        if structured is not None and dropped and report["source"] == "rule":
            trace.record_degradation("all_claims_rejected")
        if violations:
            trace.record_degradation("answer_guardrail_violation")
        if budget.exhausted:
            trace.record_degradation(budget.reason or "tool_call_limit")

    return {
        "answer": answer,
        "report": report,
        "hypotheses": hypotheses,
        "notes": notes,
        # Internal repair-loop inputs (Task57): never serialized to the API.
        "problems": problems,
        "answer_violations": violations,
        "refs": list(refs),
        "allowed_numbers": sorted(allowed),
        "evidence_count": len(evidence),
        "model_report": model_report,
    }


async def _repair_structured_report(
        *,
        model: Any,
        question: str,
        previous: Optional[Mapping[str, Any]],
        problems: List[str],
        refs: List[str],
        allowed: List[float],
) -> Optional[Dict[str, Any]]:
    """Ask the model once to fix the specific guardrail violations (Task57).

    This is a plain model call, not another agent turn: no tools, no budget
    consumption, and no chance of a second investigation loop. Any failure --
    provider error, non-JSON output, wrong shape -- is a failed repair; the
    caller then uses the deterministic summary.
    """
    prompt = build_repair_prompt(
        question=question,
        previous=previous or {},
        problems=problems,
        refs=refs,
        allowed=allowed,
    )
    try:
        response = await model.ainvoke(
            [
                SystemMessage(content=REPAIR_SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ]
        )
    except Exception as error:  # a degraded repair is not a failed request
        logger.warning("Structured report repair call failed: %s", error)
        return None
    text = (
        message_text(response) if hasattr(response, "content") else str(response)
    )
    parsed = extract_json_from_text(text)
    return dict(parsed) if is_report_payload(parsed) else None


def _deterministic_fallback(
        composed: Dict[str, Any],
        diagnosis: Dict[str, Any],
        trace: Optional[RunTrace],
        note: str,
) -> Dict[str, Any]:
    """Replace only the prose; the checked report sections stay as validated."""
    composed["answer"] = _fallback_answer(diagnosis, composed["report"])
    composed["notes"].append(note)
    if trace is not None:
        trace.record_guardrails(
            dropped=composed["report"].get("dropped", 0),
            source=composed["report"].get("source", "rule"),
            evidence_count=composed.get("evidence_count", 0),
            answer=composed["answer"],
            violations=composed.get("answer_violations") or [],
        )
    return composed


async def _repair_if_needed(
        *,
        composed: Dict[str, Any],
        diagnosis: Dict[str, Any],
        model: Any,
        question: str,
        context: DiagnosisToolContext,
        budget: ToolCallBudget,
        window: Dict[str, Any],
        trace: Optional[RunTrace],
        thread_evidence: List[Dict[str, Any]],
        turn_index: int,
) -> Dict[str, Any]:
    """One repair attempt when the guardrails dropped structured conclusions.

    Success means the recomposed report has no residual violations at all.
    Anything else -- an unusable repair payload, or claims that are still
    forged -- falls back to the deterministic summary, so a repaired answer is
    never shown without having passed the same guardrails.

    The caller only reaches this point with a live model (``run_investigation``
    has already returned the deterministic fallback when the model is
    unavailable), so there is no model-less branch here.
    """
    problems = list(composed.get("problems") or [])
    if not problems:
        return composed

    repaired = await _repair_structured_report(
        model=model,
        question=question,
        previous=composed.get("model_report"),
        problems=problems,
        refs=list(composed.get("refs") or []),
        allowed=list(composed.get("allowed_numbers") or []),
    )
    if repaired is None:
        if trace is not None:
            trace.record_repair(
                attempted=True,
                succeeded=False,
                residual=problems[:5],
                reason="repair_unusable",
            )
            trace.record_degradation("answer_repair_failed")
        return _deterministic_fallback(
            composed,
            diagnosis,
            trace,
            "模型未能在一次修复中返回有效报告，已使用确定性诊断摘要。",
        )

    candidate = _compose_answer_and_report(
        agent_answer="",
        structured=repaired,
        diagnosis=diagnosis,
        context=context,
        budget=budget,
        window=window,
        question=question,
        trace=trace,
        thread_evidence=thread_evidence,
        turn_index=turn_index,
    )
    residual = list(candidate.get("problems") or [])
    if residual:
        if trace is not None:
            trace.record_repair(
                attempted=True,
                succeeded=False,
                residual=residual[:5],
                reason="residual_violations",
            )
            trace.record_degradation("answer_repair_failed")
        return _deterministic_fallback(
            candidate,
            diagnosis,
            trace,
            "模型结论在一次修复后仍未通过证据校验，已使用确定性诊断摘要。",
        )

    if trace is not None:
        trace.record_repair(attempted=True, succeeded=True)
    candidate["notes"].append(
        "模型初版结论未通过证据校验，已根据校验原因修正一次后采用。"
    )
    return candidate


async def _fallback(
        *,
        context: DiagnosisToolContext,
        branch_name: str,
        thread_id: str,
        branch_id: int,
        staff: StaffUser,
        session,
        window: Dict[str, Any],
        status: str,
        reason: str,
        reason_code: str = "llm_unavailable",
        thread_evidence: Optional[List[Dict[str, Any]]] = None,
        turn_index: int = 1,
) -> Dict[str, Any]:
    """Deterministic answer used when the Agent cannot run or is unavailable."""
    started = time.perf_counter()
    diagnosis = await DiagnosisService.diagnose(
        branch_id,
        window["start_date"],
        window["end_date"],
        staff=staff,
        session=session,
    )
    context.trace.record_tool(
        name="get_diagnosis",
        arguments={
            "start_date": str(window["start_date"]),
            "end_date": str(window["end_date"]),
        },
        status="ok",
        duration_ms=(time.perf_counter() - started) * 1000,
        output=diagnosis,
    )
    report = _report(diagnosis)
    answer = _fallback_answer(diagnosis, report)
    steps = [
        {
            "index": 1,
            "tool": "get_diagnosis",
            "arguments": {
                "start_date": str(window["start_date"]),
                "end_date": str(window["end_date"]),
            },
            "status": "ok",
            "summary": "模型不可用，已改用确定性诊断。",
            "evidence_refs": [
                item["ref"] for item in _diagnosis_evidence(diagnosis)
            ],
            "window": {
                "start_date": str(window["start_date"]),
                "end_date": str(window["end_date"]),
            },
        }
    ]
    notes = list(window.get("notes") or [])
    notes.append(reason)
    raw_evidence = _merge_evidence(
        context.evidence, _diagnosis_evidence(diagnosis)
    )
    thread_records = merge_thread_evidence(
        thread_evidence,
        evidence_records_from_items(raw_evidence, turn_index),
    )
    evidence = _with_thread_refs(raw_evidence, turn_index)
    budget_snapshot = {
        "tool_calls_used": 0,
        "tool_calls_limit": DEFAULT_TOOL_CALLS_LIMIT,
        "recursion_limit": DEFAULT_RECURSION_LIMIT,
        "exhausted": False,
        "reason": None,
    }
    context.trace.record_guardrails(
        dropped=0,
        source="rule",
        evidence_count=len(evidence),
        answer=answer,
    )
    context.trace.record_thread_evidence(
        count=len(thread_records), current_turn=turn_index
    )
    context.trace.record_degradation(reason_code)
    context.trace.record_budget(budget_snapshot)
    context.trace.finish()
    logger.info(
        "diagnosis harness trace: %s",
        json.dumps(context.trace.summary(), ensure_ascii=False),
    )
    return {
        "thread_id": thread_id,
        "branch_id": branch_id,
        "branch_name": branch_name,
        "status": status,
        "answer": answer,
        "resolved_window": window,
        "steps": steps,
        "evidence": evidence,
        "hypotheses": [],
        "report": report,
        "diagnosis": diagnosis,
        "budget": budget_snapshot,
        "notes": notes,
    }


async def _data_coverage(
        branch_id: int, staff: StaffUser, session
) -> Optional[Dict[str, Any]]:
    """What date range this branch can actually answer for.

    A failed coverage read is not fatal: the investigation simply falls back to
    today-relative windows, which is the pre-coverage behaviour.
    """
    try:
        return await StatsService.get_data_coverage(
            branch_id=branch_id, staff=staff, session=session
        )
    except Exception as error:
        logger.warning(
            "Could not read data coverage for branch #%s: %s", branch_id, error
        )
        return None


async def run_investigation(
        payload: DiagnosisInvestigationRequest,
        staff: StaffUser,
        session,
        *,
        checkpointer=None,
        model=None,
) -> Dict[str, Any]:
    """Handle one investigation turn (new or follow-up)."""
    requested = _requested_window(payload.start_date, payload.end_date)

    if payload.thread_id:
        owner_id, thread_branch = parse_thread_id(payload.thread_id)
        if owner_id != staff.id:
            raise ForbiddenError("您没有权限访问该调查线程。")
        branch_id = resolve_staff_branch(
            staff, payload.branch_id if payload.branch_id is not None else thread_branch
        )
        if branch_id != thread_branch:
            raise ForbiddenError("该调查线程属于其他门店，不能在此线程中切换门店。")
        thread_id = payload.thread_id
    else:
        branch_id = resolve_staff_branch(staff, payload.branch_id)
        thread_id = new_thread_id(staff.id, branch_id)

    branch = await BranchService.get_branch(branch_id, session=session)
    if branch is None:
        raise NotFoundError(f"门店 #{branch_id} 不存在。")

    coverage = await _data_coverage(branch_id, staff, session)
    # The fallback path never invokes the graph, so read the thread registry
    # first: it supplies earlier turns' evidence and the current turn number.
    prior_evidence, prior_turns = await load_thread_evidence(
        checkpointer, thread_id
    )
    expected_turn = prior_turns + 1

    config = HarnessConfig(
        tool_calls_limit=DEFAULT_TOOL_CALLS_LIMIT,
        recursion_limit=DEFAULT_RECURSION_LIMIT,
        context_token_budget=settings.DIAGNOSIS_CONTEXT_TOKEN_BUDGET,
        context_char_budget=settings.DIAGNOSIS_CONTEXT_CHAR_BUDGET,
        tool_timeout_seconds=settings.DIAGNOSIS_TOOL_TIMEOUT_SECONDS,
        turn_timeout_seconds=settings.DIAGNOSIS_TURN_TIMEOUT_SECONDS,
    )
    trace = RunTrace(thread_id=thread_id, branch_id=branch_id, config=config)
    context = DiagnosisToolContext(
        staff=staff,
        session=session,
        branch_id=branch_id,
        requested_start=payload.start_date,
        requested_end=payload.end_date,
        coverage=coverage,
        trace=trace,
    )
    if coverage:
        # Coverage dates are system-supplied facts; keeping them in the
        # observations also puts their calendar numbers in the answer whitelist.
        context.observe(coverage)
    # The prompt tells the model today's date, so mentioning it must not be
    # mistaken for an invented number.
    today = datetime.now(timezone.utc).date()
    context.observe({"today": today})
    if requested:
        # Explicit request dates are this turn's window even if the model never
        # calls resolve_time_range. Seeding it here makes a date tool that uses
        # a different range fail with a correctable window_mismatch instead of
        # silently answering about dates the caller did not ask for.
        context.resolved_window = resolve_investigation_window(context)
    tools = build_diagnosis_tools(context)

    if coverage and coverage.get("has_data"):
        coverage_note = (
            f"；数据库可用数据：{coverage['start_date']} 至 {coverage['end_date']}"
        )
    else:
        coverage_note = "；该门店目前没有订单数据"
    question = (
        f"{payload.question}\n"
        f"（当前门店：{branch.name} #{branch_id}；今天：{today}{coverage_note}。"
        "相对时间表达会按可用数据解析，实际区间见 resolve_time_range 的 notes；"
        "回答时请说明实际调查的区间，若因数据覆盖范围调整过，要明确告诉用户。）"
    )

    # Model construction, tool budgeting/guarding, the checkpointer, optional
    # timeouts and the agent run all live in the harness (Task53).
    try:
        harness_turn = await run_harness_turn(
            tools=tools,
            question=question,
            thread_id=thread_id,
            config=config,
            trace=trace,
            checkpointer=checkpointer,
            model=model,
            model_factory=build_diagnosis_chat_model,
        )
    except TimeoutError:
        # The whole investigation exceeded its deadline. Keep prior-turn
        # evidence and answer deterministically instead of failing the request.
        logger.warning(
            "Diagnosis agent exceeded the %.1fs turn timeout.",
            settings.DIAGNOSIS_TURN_TIMEOUT_SECONDS,
        )
        trace.record_degradation("turn_timeout")
        window = requested or _window_from_steps(context.steps) or resolve_investigation_window(context)
        return await _fallback(
            context=context,
            branch_name=branch.name,
            thread_id=thread_id,
            branch_id=branch_id,
            staff=staff,
            session=session,
            window=window,
            status="llm_unavailable",
            reason=(
                f"模型调查超过 {settings.DIAGNOSIS_TURN_TIMEOUT_SECONDS:g} 秒，"
                "本次回答来自确定性诊断，未进行多步调查。"
            ),
            reason_code="turn_timeout",
            thread_evidence=prior_evidence,
            turn_index=expected_turn,
        )
    except Exception as error:  # provider/serialization failures degrade too
        logger.exception("Diagnosis agent failed: %s", error)
        window = requested or _window_from_steps(context.steps) or resolve_investigation_window(context)
        return await _fallback(
            context=context,
            branch_name=branch.name,
            thread_id=thread_id,
            branch_id=branch_id,
            staff=staff,
            session=session,
            window=window,
            status="llm_unavailable",
            reason=f"模型调用失败，本次回答来自确定性诊断：{error}",
            reason_code="llm_call_failed",
            thread_evidence=prior_evidence,
            turn_index=expected_turn,
        )

    budget = harness_turn.budget
    if not harness_turn.model_available:
        window = requested or resolve_investigation_window(context)
        return await _fallback(
            context=context,
            branch_name=branch.name,
            thread_id=thread_id,
            branch_id=branch_id,
            staff=staff,
            session=session,
            window=window,
            status="llm_unavailable",
            reason="当前未配置可用的对话模型，本次回答来自确定性诊断，未进行多步调查。",
            reason_code="llm_unavailable",
            thread_evidence=prior_evidence,
            turn_index=expected_turn,
        )

    turn = harness_turn.turn
    assert turn is not None
    if turn.error == "recursion_limit":
        budget.exhausted = True
        budget.reason = budget.reason or "recursion_limit"
        trace.record_degradation("recursion_limit")

    window = (
            context.resolved_window
            or requested
            or _window_from_steps(context.steps)
            or resolve_investigation_window(context)
    )

    diagnosis = context.diagnosis
    if diagnosis is None:
        diagnosis = await DiagnosisService.diagnose(
            branch_id,
            window["start_date"],
            window["end_date"],
            staff=staff,
            session=session,
        )

    turn_index = turn.turn_index or expected_turn
    thread_evidence = merge_thread_evidence(
        prior_evidence,
        turn.thread_evidence or collect_thread_evidence(turn.messages),
    )
    trace.record_thread_evidence(
        count=len(thread_evidence), current_turn=turn_index
    )

    composed = _compose_answer_and_report(
        agent_answer=turn.answer,
        structured=turn.structured,
        diagnosis=diagnosis,
        context=context,
        budget=budget,
        window=window,
        question=payload.question,
        trace=trace,
        thread_evidence=thread_evidence,
        turn_index=turn_index,
    )
    # Task57: a structurally valid report can still cite a forged ref or an
    # invented number. Give the model exactly one chance to fix those specific
    # claims; otherwise the deterministic summary is what the user sees.
    composed = await _repair_if_needed(
        composed=composed,
        diagnosis=diagnosis,
        model=harness_turn.model,
        question=payload.question,
        context=context,
        budget=budget,
        window=window,
        trace=trace,
        thread_evidence=thread_evidence,
        turn_index=turn_index,
    )
    report = composed["report"]
    answer = composed["answer"]
    hypotheses = composed["hypotheses"]
    notes = list(window.get("notes") or []) + composed["notes"]
    if budget.exhausted:
        notes.append("本次调查已达到工具调用或循环上限，结论可能不完整。")

    status = "ok"
    if budget.exhausted:
        status = "budget_exhausted"
    elif diagnosis.get("status") == "insufficient_data":
        status = "insufficient_data"

    evidence = _with_thread_refs(
        _merge_evidence(context.evidence, _diagnosis_evidence(diagnosis)),
        turn_index,
    )
    budget_snapshot = budget.snapshot(config.recursion_limit)
    trace.record_budget(budget_snapshot)
    trace.finish()
    logger.info(
        "diagnosis harness trace: %s",
        json.dumps(trace.summary(), ensure_ascii=False),
    )

    return {
        "thread_id": thread_id,
        "branch_id": branch_id,
        "branch_name": branch.name,
        "status": status,
        "answer": answer,
        "resolved_window": window,
        "steps": context.steps,
        "evidence": evidence,
        "hypotheses": hypotheses,
        "report": report,
        "diagnosis": diagnosis,
        "budget": budget_snapshot,
        "notes": notes,
    }


__all__ = [
    "DATE_TOOLS",
    "THREAD_PATTERN",
    "new_thread_id",
    "parse_thread_id",
    "run_investigation",
]
