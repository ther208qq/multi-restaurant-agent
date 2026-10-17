"""Task39: the investigation contract can express an Agent-driven answer.

These tests deliberately touch no service or database. They pin the wire shape
the investigation endpoint (Task44) and the frontend panel (Task47) agree on:
the Agent may pick the window, the trace records each tool call, and an
unevidenced explanation has a place to live that is visibly not a finding.
"""

from datetime import date

import pytest
from pydantic import ValidationError

from backend.app.schemas.diagnostics import (
    DiagnosisClaim,
    DiagnosisInvestigationRequest,
    DiagnosisInvestigationResponse,
    DiagnosisPeriod,
    DiagnosisResponse,
    InvestigationBudget,
    InvestigationHypothesis,
    InvestigationReport,
    InvestigationStep,
    InvestigationStepWindow,
    InvestigationWindow,
)


def _period(start: date, end: date) -> DiagnosisPeriod:
    return DiagnosisPeriod(
        start_date=start,
        end_date=end,
        order_count=10,
        cancelled_count=1,
        revenue=500.0,
        average_order_value=55.55,
        units_sold=20,
        active_days=7,
    )


def test_request_dates_are_optional_so_the_agent_can_choose_the_window():
    req = DiagnosisInvestigationRequest(question="最近生意怎么变差了？")
    assert req.start_date is None and req.end_date is None
    assert req.branch_id is None and req.thread_id is None

    with pytest.raises(ValidationError):
        DiagnosisInvestigationRequest(question="")


def test_response_carries_window_trace_evidence_and_hypothesis_status():
    diagnosis = DiagnosisResponse(
        branch_id=1,
        branch_name="市中心店",
        status="ok",
        current_period=_period(date(2026, 10, 10), date(2026, 10, 16)),
        baseline_period=_period(date(2026, 10, 3), date(2026, 10, 9)),
    )
    resp = DiagnosisInvestigationResponse(
        thread_id="t-1",
        branch_id=1,
        branch_name="市中心店",
        status="ok",
        answer="销售额下降主要来自单品 A 的销量断崖。",
        resolved_window=InvestigationWindow(
            start_date=date(2026, 10, 10),
            end_date=date(2026, 10, 16),
            baseline_start_date=date(2026, 10, 3),
            baseline_end_date=date(2026, 10, 9),
            source="question",
            expression="最近 7 天",
        ),
        steps=[
            InvestigationStep(
                index=1,
                tool="resolve_time_range",
                arguments={"expression": "最近 7 天"},
                summary="解析为 2026-10-10 至 2026-10-16",
            ),
            InvestigationStep(
                index=2,
                tool="get_diagnosis",
                arguments={"start_date": "2026-10-10", "end_date": "2026-10-16"},
                evidence_refs=["f1.e1", "f1.e2"],
                window=InvestigationStepWindow(
                    start_date=date(2026, 10, 10),
                    end_date=date(2026, 10, 16),
                ),
            ),
            InvestigationStep(
                index=3,
                tool="get_daily_series",
                arguments={"start_date": "2020-01-01", "end_date": "2020-01-07"},
                status="error",
                window=InvestigationStepWindow(
                    start_date=date(2020, 1, 1),
                    end_date=date(2020, 1, 7),
                ),
                expected_window=InvestigationStepWindow(
                    start_date=date(2026, 10, 10),
                    end_date=date(2026, 10, 16),
                ),
            ),
        ],
        hypotheses=[
            InvestigationHypothesis(
                text="可能是菜单下架导致",
                status="unverified",
            )
        ],
        report=InvestigationReport(
            status="ok",
            source="llm",
            facts=[DiagnosisClaim(text="销售额下降 18%", evidence_refs=["f1.e1"])],
        ),
        diagnosis=diagnosis,
        budget=InvestigationBudget(tool_calls_used=2),
    )

    assert resp.resolved_window.source == "question"
    assert resp.steps[1].evidence_refs == ["f1.e1", "f1.e2"]
    assert resp.steps[2].status == "error"
    assert resp.steps[2].window.start_date == date(2020, 1, 1)
    assert resp.steps[2].expected_window.end_date == date(2026, 10, 16)
    assert resp.hypotheses[0].status == "unverified"
    assert resp.report.inferences == []
    assert resp.budget.exhausted is False


def test_hypothesis_status_rejects_a_state_outside_the_contract():
    with pytest.raises(ValidationError):
        InvestigationHypothesis(text="x", status="confirmed")
