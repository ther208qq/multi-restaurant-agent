"""Reproducible diagnosis harness baseline (Task52).

Runs three deterministic, database-free scenarios against the production
harness pieces (guarded tools, the LangChain agent, and the answer guardrails)
and prints what the trace recorded:

* ``multi_step`` -- two tool calls followed by a valid structured answer;
* ``budget_exhausted`` -- a model that keeps calling a tool past its budget;
* ``guardrail_rejected`` -- an answer with a fabricated ref and number.

The scenario inputs are fixed, so re-running the script produces the same
shape and counts. Tool durations are real measurements and naturally vary; the
report keeps them in milliseconds for comparison, not as byte-stable output.

Usage:
    .venv/Scripts/python.exe scripts/diagnosis_harness_baseline.py
    .venv/Scripts/python.exe scripts/diagnosis_harness_baseline.py --output sdd/diagnosis_harness_baseline.json
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from pydantic import Field  # noqa: E402

from backend.app.agent.diagnosis_investigation import (  # noqa: E402
    _compose_answer_and_report,
)
from backend.app.agent.diagnosis_tools import DiagnosisToolContext  # noqa: E402
from backend.app.agent.harness import (  # noqa: E402
    HarnessConfig,
    RunTrace,
    ToolCallBudget,
    run_harness_turn,
)
from backend.app.agent.time_range import resolve_explicit_range  # noqa: E402
from backend.app.models.user import StaffUser  # noqa: E402

BASELINE_THREAD = "baseline-thread"
BASELINE_BRANCH = 1


class ScriptedChatModel(BaseChatModel):
    """Return queued AIMessages; every extra call repeats the last one."""

    responses: List[AIMessage]
    index: int = 0
    seen: List[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-diagnosis-baseline"

    def bind_tools(self, tools, **kwargs):  # noqa: D401 - LangChain hook
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        position = min(self.index, len(self.responses) - 1)
        self.index += 1
        template = self.responses[position]
        message = AIMessage(
            content=template.content,
            tool_calls=[
                {**call, "id": f"{call.get('id', 'call')}-{self.index}"}
                for call in template.tool_calls
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


@tool
async def get_diagnosis(start_date: str, end_date: str) -> str:
    """Deterministic diagnosis fixture for the baseline scenario."""
    return json.dumps(
        {
            "status": "ok",
            "start_date": start_date,
            "end_date": end_date,
            "order_count": 80,
            "revenue": 4000.0,
            "findings": [
                {
                    "metric": "revenue",
                    "label": "营业额",
                    "direction": "down",
                    "change_pct": -20.0,
                    "evidence": [
                        {"ref": "f0.e0", "label": "本期营业额", "value": 4000.0},
                        {"ref": "f0.e1", "label": "上期营业额", "value": 5000.0},
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )


@tool
async def get_daily_series(start_date: str, end_date: str) -> str:
    """Deterministic daily-series fixture for the baseline scenario."""
    return json.dumps(
        {
            "start_date": start_date,
            "end_date": end_date,
            "daily": [
                {"date": "2026-10-12", "revenue": 300.0, "ref": "daily1.e1"}
            ],
        },
        ensure_ascii=False,
    )


def _call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "type": "tool_call", "id": call_id}],
    )


def _valid_answer() -> AIMessage:
    return AIMessage(
        content=json.dumps(
            {
                "answer": (
                    "2026-10-10 至 2026-10-16 营业额 4000 元，"
                    "上期为 5000 元（f0.e0、f0.e1）。"
                ),
                "facts": [
                    {"text": "本期营业额 4000 元", "evidence_refs": ["f0.e0"]}
                ],
                "inferences": [
                    {
                        "text": "营业额较上期下降（f0.e0、f0.e1）",
                        "evidence_refs": ["f0.e0", "f0.e1"],
                    }
                ],
                "hypotheses": [],
                "checks": [],
            },
            ensure_ascii=False,
        )
    )


def _run_agent(
    responses: List[AIMessage], *, budget_limit: int, recursion_limit: int
) -> tuple:
    config = HarnessConfig(
        tool_calls_limit=budget_limit, recursion_limit=recursion_limit
    )
    trace = RunTrace(
        thread_id=BASELINE_THREAD, branch_id=BASELINE_BRANCH, config=config
    )
    model = ScriptedChatModel(responses=responses)
    harness_turn = asyncio.run(
        run_harness_turn(
            tools=[get_diagnosis, get_daily_series],
            question="最近生意为什么下降？",
            thread_id=BASELINE_THREAD,
            config=config,
            trace=trace,
            checkpointer=MemorySaver(),
            model=model,
        )
    )
    assert harness_turn.turn is not None
    turn = harness_turn.turn
    budget = harness_turn.budget
    if turn.error:
        trace.record_degradation(turn.error)
    if budget.exhausted:
        trace.record_degradation(budget.reason or "tool_call_limit")
    return trace, budget, turn


def _diagnosis_fixture() -> Dict[str, Any]:
    return {
        "branch_id": BASELINE_BRANCH,
        "branch_name": "基线门店",
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
        "findings": [
            {
                "metric": "revenue",
                "label": "营业额",
                "direction": "down",
                "severity": "warning",
                "current": 4000.0,
                "previous": 5000.0,
                "change_pct": -20.0,
                "menu_item_id": None,
                "evidence": [
                    {
                        "ref": "f0.e0",
                        "label": "本期营业额",
                        "value": 4000.0,
                        "detail": "",
                    },
                    {
                        "ref": "f0.e1",
                        "label": "上期营业额",
                        "value": 5000.0,
                        "detail": "",
                    },
                ],
            }
        ],
    }


def _window() -> Dict[str, Any]:
    return resolve_explicit_range(
        date(2026, 10, 10), date(2026, 10, 16), today=date(2026, 10, 17)
    )


def _compose_scenario(
    *, trace: RunTrace, budget: ToolCallBudget, answer: str
) -> Dict[str, Any]:
    context = DiagnosisToolContext(
        staff=StaffUser(id=1, username="baseline", role="admin", branch_id=None),
        session=object(),
        branch_id=BASELINE_BRANCH,
        trace=trace,
    )
    composed = _compose_answer_and_report(
        agent_answer=answer,
        diagnosis=_diagnosis_fixture(),
        context=context,
        budget=budget,
        window=_window(),
        question="最近生意为什么下降？",
        trace=trace,
    )
    trace.record_budget(budget.snapshot(trace.config.recursion_limit))
    trace.finish()
    return {
        "trace": trace.summary(),
        "report": {
            "source": composed["report"]["source"],
            "dropped": composed["report"]["dropped"],
            "facts": len(composed["report"]["facts"]),
            "inferences": len(composed["report"]["inferences"]),
            "checks": len(composed["report"]["checks"]),
            "hypotheses": len(composed["hypotheses"]),
            "notes": len(composed["notes"]),
            "answer_replaced": "受控的确定性摘要" in " ".join(composed["notes"]),
        },
    }


def build_report() -> Dict[str, Any]:
    """Build the three-scenario baseline report without a database or LLM."""
    normal_trace, normal_budget, normal_turn = _run_agent(
        [
            _call("get_diagnosis", {"start_date": "2026-10-10", "end_date": "2026-10-16"}, "c1"),
            _call("get_daily_series", {"start_date": "2026-10-10", "end_date": "2026-10-16"}, "c2"),
            _valid_answer(),
        ],
        budget_limit=6,
        recursion_limit=24,
    )
    multi_step = _compose_scenario(
        trace=normal_trace,
        budget=normal_budget,
        answer=normal_turn.answer,
    )
    multi_step["agent_error"] = normal_turn.error

    exhausted_trace, exhausted_budget, exhausted_turn = _run_agent(
        [_call("get_diagnosis", {"start_date": "2026-10-10", "end_date": "2026-10-16"}, "loop")],
        budget_limit=1,
        recursion_limit=6,
    )
    exhausted_trace.record_budget(
        exhausted_budget.snapshot(exhausted_trace.config.recursion_limit)
    )
    exhausted_trace.finish()
    budget_exhausted = {
        "trace": exhausted_trace.summary(),
        "agent_error": exhausted_turn.error,
    }

    rejected_trace = RunTrace(
        thread_id="baseline-guardrail", branch_id=BASELINE_BRANCH
    )
    rejected_budget = ToolCallBudget(used=1)
    guardrail_rejected = _compose_scenario(
        trace=rejected_trace,
        budget=rejected_budget,
        answer=json.dumps(
            {
                "answer": "营业额下降了 88%。",
                "facts": [{"text": "猜的", "evidence_refs": ["f9.e9"]}],
            },
            ensure_ascii=False,
        ),
    )

    return {
        "schema": "diagnosis-harness-baseline/v1",
        "description": (
            "Deterministic offline baseline for the diagnosis agent harness. "
            "Inputs are fixed; tool durations are measured and may vary."
        ),
        "config": {
            "tool_calls_limit": HarnessConfig().tool_calls_limit,
            "recursion_limit": HarnessConfig().recursion_limit,
        },
        "scenarios": {
            "multi_step": multi_step,
            "budget_exhausted": budget_exhausted,
            "guardrail_rejected": guardrail_rejected,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="write the JSON report to this path")
    args = parser.parse_args()

    text = json.dumps(build_report(), ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {output}")
    else:
        print(text)


if __name__ == "__main__":
    main()
