"""Task45: the investigation answer is subject to the same evidence guards.

The composition function is tested directly: a scripted JSON answer, the
deterministic payload, and the tool observations that define what may be
quoted. No model and no database are involved.
"""

import json
from datetime import date

from backend.app.agent.harness import ToolCallBudget
from backend.app.agent.diagnosis_investigation import _compose_answer_and_report
from backend.app.agent.diagnosis_tools import DiagnosisToolContext
from backend.app.agent.time_range import resolve_explicit_range
from backend.app.models.user import StaffUser


def _diagnosis() -> dict:
    return {
        "branch_id": 1,
        "branch_name": "市中心店",
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
        "thresholds": {"change_threshold_pct": 10.0},
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
                    {"ref": "f0.e0", "label": "本期营业额", "value": 4000.0, "detail": ""},
                    {"ref": "f0.e1", "label": "上期营业额", "value": 5000.0, "detail": ""},
                ],
            },
            {
                "metric": "order_count",
                "label": "订单量",
                "direction": "down",
                "severity": "warning",
                "current": 80,
                "previous": 100,
                "change_pct": -20.0,
                "menu_item_id": None,
                "evidence": [
                    {"ref": "f1.e0", "label": "本期订单量", "value": 80, "detail": ""},
                    {"ref": "f1.e1", "label": "上期订单量", "value": 100, "detail": ""},
                ],
            },
        ],
    }


def _context() -> DiagnosisToolContext:
    context = DiagnosisToolContext(
        staff=StaffUser(id=1, username="admin", role="admin", branch_id=None),
        session=object(),
        branch_id=1,
    )
    context.observations.append(
        {"daily": [{"date": date(2026, 10, 12), "revenue": 300.0, "order_count": 8}]}
    )
    return context


def _window() -> dict:
    return resolve_explicit_range(
        date(2026, 10, 10), date(2026, 10, 16), today=date(2026, 10, 17)
    )


def _structured(**sections) -> str:
    payload = {"answer": "本期营业额下降。", **sections}
    return json.dumps(payload, ensure_ascii=False)


def _compose(answer: str, *, budget=None, **kwargs):
    return _compose_answer_and_report(
        agent_answer=answer,
        diagnosis=_diagnosis(),
        context=_context(),
        budget=budget or ToolCallBudget(used=3),
        window=_window(),
        question="最近生意为什么下降？",
        **kwargs,
    )


def _public(result: dict) -> str:
    """Only the parts that become the HTTP response; repair inputs are internal."""
    return json.dumps(
        {
            key: result[key]
            for key in ("answer", "report", "hypotheses", "notes")
        },
        ensure_ascii=False,
        default=str,
    )


def test_claims_with_fabricated_refs_or_numbers_are_dropped():
    result = _compose(
        _structured(
            facts=[
                {
                    "text": "本期营业额为 4000 元",
                    "evidence_refs": ["f0.e0"],
                }
            ],
            inferences=[
                {"text": "原因未知", "evidence_refs": ["f9.e9"]},
                {
                    "text": "库存成本增加 9999 元",
                    "evidence_refs": ["f0.e0"],
                },
            ],
        )
    )

    assert result["report"]["dropped"] == 2
    assert result["report"]["facts"][0]["text"] == "本期营业额为 4000 元"
    rendered = _public(result)
    assert "f9.e9" not in rendered
    assert "9999" not in rendered
    # The inference section is filled from the rules, not left empty.
    assert result["report"]["inferences"]


def test_forbidden_causal_keyword_never_becomes_an_inference():
    result = _compose(
        _structured(
            inferences=[
                {"text": "可能是因为天气太热导致客流减少", "evidence_refs": ["f0.e0"]}
            ]
        )
    )

    texts = [claim["text"] for claim in result["report"]["inferences"]]
    assert not any("天气" in text for text in texts)
    assert result["report"]["dropped"] == 1


def test_hypothesis_is_kept_in_its_own_unverified_bucket():
    result = _compose(
        _structured(hypotheses=[{"text": "可能是新菜研发占用了后厨时间"}])
    )

    assert len(result["hypotheses"]) == 1
    hypothesis = result["hypotheses"][0]
    assert hypothesis["status"] == "unverified"
    assert hypothesis["text"].startswith("未经证实：")
    assert hypothesis["evidence_refs"] == []
    assert all(
        "新菜研发" not in claim["text"]
        for claim in result["report"]["inferences"]
    )


def test_answer_quoting_an_unknown_number_is_replaced():
    result = _compose(
        _structured(answer="营业额下降了 88%，主要原因是客流减少。")
    )

    assert "88" not in result["answer"]
    assert any("证据校验" in note for note in result["notes"])


def test_answer_citing_an_unknown_ref_is_replaced():
    result = _compose(
        _structured(answer="详见 f9.e9 的证据。")
    )

    assert "f9.e9" not in result["answer"]
    assert any("证据校验" in note for note in result["notes"])


def test_budget_exhaustion_names_the_tools_that_were_not_run():
    budget = ToolCallBudget(limit=6, used=6, exhausted=True)
    budget.reason = "tool_call_limit"
    budget.calls = ["resolve_time_range", "get_diagnosis", "get_daily_series"]

    result = _compose(_structured(), budget=budget)

    checks = " ".join(claim["text"] for claim in result["report"]["checks"])
    assert "未执行" in checks
    assert "get_dish_daily" in checks
    assert "compare_peers" in checks


def test_answer_attributing_the_change_to_unobserved_data_is_replaced():
    result = _compose(
        _structured(answer="营业额下降的原因是天气太热，客流减少。")
    )

    assert "天气" not in result["answer"]
    assert any("证据校验" in note for note in result["notes"])


def test_namespaced_refs_are_cited_not_misread_as_numbers():
    context = _context()
    context.evidence.append(
        {
            "ref": "daily1.e1",
            "label": "10月12日营业额",
            "value": 300.0,
            "detail": "订单 8 单",
        }
    )
    result = _compose_answer_and_report(
        agent_answer=_structured(answer="10月12日营业额为 300 元（daily1.e1）。"),
        diagnosis=_diagnosis(),
        context=context,
        budget=ToolCallBudget(used=1),
        window=_window(),
        question="最近生意怎么样？",
    )

    assert result["answer"].startswith("10月12日营业额")
    assert not any("证据校验" in note for note in result["notes"])


def test_fabricated_namespaced_ref_in_the_answer_is_replaced():
    result = _compose(_structured(answer="见 daily9.e9 的明细。"))

    assert "daily9.e9" not in result["answer"]
    assert any("证据校验" in note for note in result["notes"])


def test_unstructured_answer_is_kept_but_the_report_stays_rule_based():
    result = _compose("最近营业额下降，建议查看单品销量。")

    assert result["answer"] == "最近营业额下降，建议查看单品销量。"
    assert result["report"]["source"] == "rule"
    assert any("没有输出结构化结论" in note for note in result["notes"])


def _prior_evidence() -> list:
    """One fact from the previous turn, addressable only by its thread ref."""
    return [
        {
            "ref": "t1.daily9.e1",
            "local_ref": "daily9.e1",
            "turn": 1,
            "label": "10月11日营业额",
            "value": 1234.0,
            "detail": "上一轮工具结果",
        }
    ]


def test_claims_may_cite_a_prior_turn_by_its_qualified_ref():
    result = _compose(
        _structured(
            facts=[
                {
                    "text": "上一轮观察到 10月11日营业额为 1234 元",
                    "evidence_refs": ["t1.daily9.e1"],
                }
            ]
        ),
        thread_evidence=_prior_evidence(),
        turn_index=2,
    )

    assert result["report"]["dropped"] == 0
    assert result["report"]["facts"][0]["evidence_refs"] == ["t1.daily9.e1"]


def test_a_bare_ref_produced_by_only_the_prior_turn_still_resolves():
    result = _compose(
        _structured(
            facts=[
                {
                    "text": "10月11日营业额为 1234 元",
                    "evidence_refs": ["daily9.e1"],
                }
            ]
        ),
        thread_evidence=_prior_evidence(),
        turn_index=2,
    )

    assert result["report"]["dropped"] == 0
    assert result["report"]["facts"][0]["evidence_refs"] == ["daily9.e1"]


def test_a_fabricated_cross_turn_ref_is_still_rejected():
    result = _compose(
        _structured(
            facts=[
                {
                    "text": "上一轮观察到 10月11日营业额为 1234 元",
                    "evidence_refs": ["t1.f9.e9"],
                }
            ]
        ),
        thread_evidence=_prior_evidence(),
        turn_index=2,
    )

    assert result["report"]["dropped"] == 1
    rendered = _public(result)
    assert "t1.f9.e9" not in rendered
    assert result["report"]["facts"]


def test_an_answer_citing_a_fabricated_turn_ref_is_replaced():
    result = _compose(
        _structured(answer="上一轮数据见 t1.f9.e9。"),
        thread_evidence=_prior_evidence(),
        turn_index=2,
    )

    assert "t1.f9.e9" not in result["answer"]
    assert any("证据校验" in note for note in result["notes"])


def test_an_answer_may_quote_a_prior_turns_number_with_its_ref():
    result = _compose(
        _structured(answer="上一轮 10月11日营业额为 1234 元（t1.daily9.e1）。"),
        thread_evidence=_prior_evidence(),
        turn_index=2,
    )

    assert "1234" in result["answer"]
    assert not any("证据校验" in note for note in result["notes"])
