"""Tests for the Task60 offline diagnosis evaluation."""

import copy
import json

import pytest

from backend.app.agent.diagnosis_evaluation import (
    _citation_metrics,
    _number_metrics,
    load_dataset,
    run_evaluation,
    run_scenario,
    write_report,
)
from backend.app.agent.diagnosis_tools import TOOL_NAMES

CLEAN_SCENARIOS = {
    "dish_collapse",
    "peer_comparison",
    "ops_change",
    "follow_up_window_switch",
    "business_profile",
    "budget_exhausted",
    "degraded_probes",
    "window_recovery",
}


def test_dataset_is_versioned_and_uses_known_tools():
    dataset = load_dataset()
    assert dataset.version == 1
    assert len(dataset.sha256) == 64
    assert len(dataset.scenarios) >= 8
    known = set(TOOL_NAMES)
    for scenario in dataset.scenarios:
        for turn in scenario["turns"]:
            assert turn["window"] in dataset.windows
            for step in turn["steps"]:
                if "tool" in step:
                    assert step["tool"] in known


async def test_all_scenarios_pass():
    report = await run_evaluation(load_dataset())
    failures = [
        (scenario["id"], scenario["failures"])
        for scenario in report["scenarios"]
        if not scenario["passed"]
    ]
    assert failures == []
    summary = report["summary"]
    assert summary["scenarios"] >= 8
    assert summary["passed"] == summary["scenarios"]
    assert summary["failed"] == 0
    assert summary["tool_selection_accuracy"] == 1.0
    assert summary["degradation_rate"] > 0  # the probe scenarios must degrade


async def test_clean_scenarios_are_fully_grounded():
    report = await run_evaluation(load_dataset())
    by_id = {scenario["id"]: scenario for scenario in report["scenarios"]}
    for scenario_id in CLEAN_SCENARIOS:
        for turn in by_id[scenario_id]["turns"]:
            metrics = turn["metrics"]
            assert metrics["citation"]["validity"] == 1.0, scenario_id
            assert metrics["numbers"]["hallucinated"] == [], scenario_id


async def test_guardrail_scenarios_are_scored_not_ignored():
    report = await run_evaluation(load_dataset())
    by_id = {scenario["id"]: scenario for scenario in report["scenarios"]}

    invented = by_id["invented_number"]["turns"][0]["metrics"]
    assert invented["dropped"] == 1
    assert invented["numbers"]["hallucinated"]
    assert invented["answer_violations"]
    assert "answer_guardrail_violation" in invented["degrade_reasons"]

    invalid = by_id["invalid_ref"]["turns"][0]["metrics"]
    assert invalid["dropped"] == 2
    assert invalid["citation"]["validity"] == 0.0
    assert invalid["report_source"] == "rule"
    assert "all_claims_rejected" in invalid["degrade_reasons"]


def test_forged_number_and_ref_scoring():
    model_report = {
        "answer": "营业额是 99999 元（f0.e1）。",
        "facts": [{"text": "营业额 99999 元。", "evidence_refs": ["f0.e1"]}],
        "inferences": [{"text": "下降了。", "evidence_refs": ["f9.e9"]}],
        "hypotheses": [{"text": "可能是天气。", "evidence_refs": ["f0.e1"]}],
        "checks": [],
    }
    citations = _citation_metrics(model_report, ["f0.e1"])
    assert citations["total"] == 3
    assert citations["valid"] == 1
    assert citations["missing"] == 0
    assert citations["validity"] == pytest.approx(1 / 3)

    missing = _citation_metrics(
        {"facts": [{"text": "无引用事实。", "evidence_refs": []}]}, []
    )
    assert missing["missing"] == 1
    assert missing["validity"] == 0.0

    numbers = _number_metrics(model_report, [9600.0])
    assert numbers["hallucinated"] == [99999.0, 99999.0]
    assert numbers["total"] == 2


async def test_wrong_expectation_fails_loudly():
    dataset = load_dataset()
    scenario = copy.deepcopy(dataset.scenarios[0])
    scenario["turns"][0]["expect"]["tools"] = ["resolve_time_range"]
    result = await run_scenario(dataset, scenario)
    assert result["passed"] is False
    assert any("工具轨迹" in failure for failure in result["failures"])


async def test_report_is_written_with_dataset_hash(tmp_path):
    report = await run_evaluation(load_dataset())
    target = write_report(report, tmp_path / "nested" / "report.json")
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["summary"] == report["summary"]
    assert saved["dataset"]["sha256"] == load_dataset().sha256
    assert saved["harness"] == {
        "mode": "offline-scripted",
        "provider_calls": 0,
        "database_access": False,
    }
