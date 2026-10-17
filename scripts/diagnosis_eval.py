"""Run the offline diagnosis evaluation and write its metrics report (Task60).

The default run needs no provider, no API key and no database: it replays the
fixed dataset through the real harness and guardrails, prints one line per
scenario, and writes ``sdd/diagnosis_eval_report.json``. Exit code is non-zero
when any scenario fails, which is what a CI job should gate on.

``--live`` additionally runs the existing manual smoke against a local eval
server; that part is opt-in because it needs a running server and a real model.

Usage:
    .venv/Scripts/python.exe scripts/diagnosis_eval.py
    .venv/Scripts/python.exe scripts/diagnosis_eval.py --output /tmp/report.json
    .venv/Scripts/python.exe scripts/diagnosis_eval.py --live
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.app.agent.diagnosis_evaluation import (  # noqa: E402
    DEFAULT_DATASET_PATH,
    load_dataset,
    run_evaluation,
    write_report,
)

DEFAULT_OUTPUT = Path(PROJECT_ROOT) / "sdd" / "diagnosis_eval_report.json"
SMOKE_SCRIPT = Path(PROJECT_ROOT) / "scripts" / "smoke_diagnosis_investigation.py"


def _print_scenarios(report: dict) -> None:
    for scenario in report["scenarios"]:
        marker = "PASS" if scenario["passed"] else "FAIL"
        turns = len(scenario["turns"])
        print(f"[{marker}] {scenario['id']} ({turns} 轮)")
        for failure in scenario["failures"]:
            print(f"       - {failure}")


def _print_summary(summary: dict) -> None:
    print("\n汇总：")
    print(
        f"  场景 {summary['passed']}/{summary['scenarios']} 通过"
        f"（{summary['turns']} 轮，通过率 {summary['pass_rate']:.0%}）"
    )
    print(f"  工具选择准确率：{summary['tool_selection_accuracy']:.0%}")
    print(f"  窗口准确率：{summary['window_accuracy']:.0%}")
    print(f"  引用有效率：{summary['citation_validity']:.0%}")
    print(f"  数字幻觉率：{summary['number_hallucination_rate']:.2%}")
    print(f"  降级率：{summary['degradation_rate']:.0%}")
    print(f"  护栏介入率：{summary['guardrail_intervention_rate']:.0%}")
    print(
        f"  耗时：{summary['total_latency_ms']:.1f} ms "
        f"（平均 {summary['avg_latency_ms']:.1f} ms/轮）"
    )
    tokens = summary["tokens"]
    print(
        "  token："
        f"模型输入 {tokens['prompt']}，工具输出 {tokens['tool_output']}，"
        f"回答 {tokens['answer']}，合计 {tokens['total']}"
        f"（平均 {summary['avg_tokens_per_turn']}/轮）"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET_PATH,
        help="fixed evaluation dataset (default: backend/app/agent/diagnosis_eval_dataset.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="metrics report path (default: sdd/diagnosis_eval_report.json)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="after the offline run, also run scripts/smoke_diagnosis_investigation.py",
    )
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="print the full report JSON to stdout",
    )
    args = parser.parse_args()

    dataset = load_dataset(args.dataset)
    report = asyncio.run(run_evaluation(dataset))
    target = write_report(report, args.output)

    _print_scenarios(report)
    _print_summary(report["summary"])
    print(f"\n报告已写入：{target}")
    if args.print_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    exit_code = 0 if report["summary"]["failed"] == 0 else 1
    if args.live:
        print("\n运行 live smoke（需要本地 eval 服务和真实模型）……")
        completed = subprocess.run(
            [sys.executable, str(SMOKE_SCRIPT)],
            check=False,
            cwd=PROJECT_ROOT,
        )
        if completed.returncode != 0:
            exit_code = completed.returncode or 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
