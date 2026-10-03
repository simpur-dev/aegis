"""任务智能解析的评测命令（批次 C4 的取证入口）。

用法：

    uv run python -m scripts.eval_report_parsing                 # 只量确定性腿（规则 + 词表）
    uv run python -m scripts.eval_report_parsing --with-llm      # 接上 LLM 裁决腿（需 AEGIS_LLM_API_KEY）
    uv run python -m scripts.eval_report_parsing --json out.json # 落盘给 REPORT 引用

口径写在 `aegis.services.parsing_eval`（测试也调它）：本脚本只负责取数据集、打印、退出码，
不在这里再算一遍准确率——两份算式迟早对不上账。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from aegis.config import get_settings
from aegis.services.llm_gateway import build_gateway_if_configured
from aegis.services.parsing_eval import MIN_CASES, evaluate, load_dataset

DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "report_parsing_cases.jsonl"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="灾情文本解析准确率评测")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="JSONL 评测集路径")
    parser.add_argument("--with-llm", action="store_true", help="接上 LLM 裁决腿（无密钥时报错退出，不假装跑过）")
    parser.add_argument("--json", dest="json_out", default=None, help="把报表写到该路径")
    parser.add_argument("--show-failures", action="store_true", help="逐条列出判错的用例")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    dataset = load_dataset(args.dataset)
    llm = None
    note_suffix = ""
    if args.with_llm:
        llm = build_gateway_if_configured(get_settings())
        if llm is None:
            print("需要 AEGIS_LLM_API_KEY 才能开裁决腿；本次未测得 LLM 腿的任何数字。", file=sys.stderr)
            return 2
        note_suffix = "（含 LLM 裁决腿）"

    report = await evaluate(dataset, with_llm=args.with_llm, llm=llm)
    payload = report.as_dict()
    print(
        json.dumps(
            {key: value for key, value in payload.items() if key != "outcomes"},
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"用例 {report.cases} 条，灾种覆盖 {len(report.hazards_covered)} 类，数据集性质 {report.dataset_kind}")
    if note_suffix:
        print(f"本次口径：{payload['note']}")
    print("官方结论：" + ("可按指标引用" if report.official_claim else "不构成官方准确率证据（合成回归集）"))

    if args.show_failures:
        for outcome in report.outcomes:
            if not (outcome.hazard_correct and outcome.level_correct):
                print(json.dumps(outcome.as_dict(), ensure_ascii=False))

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {args.json_out}")

    if report.cases < MIN_CASES:
        print(f"用例数 {report.cases} < {MIN_CASES}，样本不足", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
