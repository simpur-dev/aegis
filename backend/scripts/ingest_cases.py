"""预案案例批量回填：给内置模板一个能整库灌进图谱的入口。

为什么需要这条命令：`learn_case` 此前只有两个调用方——单条 `POST /api/v1/knowledge/cases`
和单元测试。内置的 16 条预案模板因此从来没进过图谱：新部署的 Neo4j 是空的，每一次召回都
在降级链上从内存库取，`/api/v1/knowledge/recall` 看起来正常（照样有命中），图谱那侧却一条
事实都拿不到。逐条 curl 16 次不是"入口"，是把缺的东西摊给运维。

口径（都是能在验收时被追问的）：
- **装配只走 `build_knowledge`**：命令里不自己 new 提供者。脚本造出的链和生产用的链不是
  一条链时，"回填成功"这句话就只对本进程成立。
- **逐条报告落点**：每条案例打印 `driver/degraded/reason`。整批"导入 16 条"是最没用的汇总——
  图谱没收下、全落在内存时，那个 16 一样会显示出来，却被读成成功。
- **写完回读**：默认对刚写入的每条做一次召回，报告命中来自哪条腿。写入返回成功只说明驱动
  收了字节，能被查回来才说明预案真的进了回路（`--no-verify` 可跳过）。
- **内存形态默认拒绝**：纯内存提供者活到进程结束为止，灌它等于没灌；确要做演示加
  `--allow-memory`，结果里会写明 `durable=false`。
- **一条不中就整批中止**：契约外的异常（LLM 鉴权失败、依赖缺失）说明剩下的也大概率进不去，
  继续跑只会把 16 次错误攒成日志噪声；已完成的逐条结果照样交回。

用法：
    uv run python -m scripts.ingest_cases --dry-run
    uv run python -m scripts.ingest_cases                          # 内置 16 条
    uv run python -m scripts.ingest_cases --source cases.json      # 外部 JSON 数组
    uv run python -m scripts.ingest_cases --case-id case_sd_livestock_shelter
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from aegis.errors import AegisError
from aegis.knowledge.cases import HazardCase, load_builtin_cases, load_cases
from aegis.knowledge.provider import KnowledgeProvider, LearnOutcome

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_REFUSED = 2
EXIT_DEGRADED = 3
EXIT_NOT_RECALLABLE = 4


def select_cases(cases: Sequence[HazardCase], wanted: Sequence[str]) -> list[HazardCase]:
    """按 case_id 挑子集：给一个不存在的 id 就报错，不静默少灌。

    拼错的 id 若被忽略，这次回填会"全部成功"而库里少一条——没人会在 16 条里发现少了谁。
    """
    if not wanted:
        return list(cases)
    index = {case.case_id: case for case in cases}
    unknown = [case_id for case_id in wanted if case_id not in index]
    if unknown:
        known = ", ".join(sorted(index)[:5])
        raise ValueError(f"未知 case_id: {', '.join(unknown)}（候选前五个：{known}）")
    return [index[case_id] for case_id in dict.fromkeys(wanted)]


async def ingest(provider: KnowledgeProvider, cases: Sequence[HazardCase]) -> tuple[list[LearnOutcome], str]:
    """逐条写入，返回 (每条结果, 中止原因)。

    中止原因只在异常越出 `learn_case` 契约时出现（降级本身是返回值里的 `degraded`，不是异常）。
    已经落地的条目不回收、也不编造成果：交回真实前缀，让运维知道断在哪一条。
    """
    outcomes: list[LearnOutcome] = []
    for case in cases:
        try:
            outcomes.append(await provider.learn_case(case))
        except Exception as exc:  # 契约外异常：类型名进结果，不在这里替图谱编一个落点
            return outcomes, f"{type(exc).__name__}: {str(exc)[:200]}"
    return outcomes, ""


async def verify(provider: KnowledgeProvider, cases: Sequence[HazardCase]) -> dict[str, list[str]]:
    """回读每条案例：返回 `case_id -> 命中的腿`（空列表表示查不回来）。

    走的是生产同一个 `recall`，所以这里看到的是"预案生成时能不能拿到它"，不是驱动侧的自陈。
    切片宽度取整批条数而不是预警路径的 top-3：这里只判"在不在"，排名由 `pipeline/chain` 的
    口径管——用 top-3 判在不在，会把"排在第 4"误报成"没进库"。
    """
    checked: dict[str, list[str]] = {}
    for case in cases:
        matches = await provider.recall(case.title, limit=max(1, len(cases)))
        checked[case.case_id] = sorted({match.source for match in matches if match.case_id == case.case_id})
    return checked


def summarize(
    outcomes: Sequence[LearnOutcome],
    *,
    aborted: str,
    recall: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """把逐条结果汇成一份可被追问的账：谁落在哪条腿、谁降级、谁查不回来。"""
    by_driver: dict[str, int] = {}
    for outcome in outcomes:
        by_driver[outcome.driver] = by_driver.get(outcome.driver, 0) + 1
    degraded = [outcome for outcome in outcomes if outcome.degraded]
    missing = [] if recall is None else [case_id for case_id, sources in recall.items() if not sources]
    return {
        "total": len(outcomes),
        "landed": len(outcomes) - len(degraded),
        "degraded": len(degraded),
        "by_driver": by_driver,
        "recall_checked": None if recall is None else len(recall) - len(missing),
        "recall_missing": missing,
        "aborted": aborted,
        "outcomes": [outcome.model_dump() for outcome in outcomes],
    }


def exit_code(summary: dict[str, Any]) -> int:
    """退出码按事实分级：中止 > 一条没降级 > 写进去了却查不回来。"""
    if summary["aborted"]:
        return EXIT_INVALID
    if summary["degraded"]:
        return EXIT_DEGRADED
    if summary["recall_missing"]:
        return EXIT_NOT_RECALLABLE
    return EXIT_OK


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量回填预案案例（逐条报告落点，写完回读）")
    parser.add_argument("--source", type=Path, default=None, help="外部案例 JSON 数组；缺省用内置预案模板")
    parser.add_argument("--case-id", action="append", default=[], help="只回填指定 case_id（可重复）")
    parser.add_argument("--dry-run", action="store_true", help="只解析与选目，不写入")
    parser.add_argument("--no-verify", action="store_true", help="跳过回读（写得多、想让召回自己说话时）")
    parser.add_argument("--allow-memory", action="store_true", help="允许灌进内存视图（进程结束即消失）")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    from aegis.config import get_settings
    from aegis.integrations import build_knowledge, close_knowledge, warm_knowledge

    source_cases = load_cases(args.source) if args.source is not None else load_builtin_cases()
    cases = select_cases(source_cases, args.case_id)
    if args.dry_run:
        return (
            {
                "status": "validated",
                "rows": len(cases),
                "case_ids": [case.case_id for case in cases],
                "source": str(args.source) if args.source else "builtin",
            },
            EXIT_OK,
        )

    provider, state = build_knowledge(get_settings())
    if provider is None:
        return {"status": "refused", "reason": "知识层未装配（knowledge）"}, EXIT_REFUSED
    if state.driver == "in_memory" and not args.allow_memory:
        return (
            {
                "status": "refused",
                "reason": "knowledge_graphiti_uri 未配置：灌进内存视图只活到本进程结束",
                "driver": state.driver,
            },
            EXIT_REFUSED,
        )

    # 索引没建就写，图谱会收字节但查不出来；把启动期那一步先补上，失败只记一行不中止
    schema_error = await warm_knowledge(provider)
    try:
        outcomes, aborted = await ingest(provider, cases)
        recall = None if args.no_verify or not outcomes else await verify(provider, cases)
    finally:
        close_error = await close_knowledge(provider)

    summary = summarize(outcomes, aborted=aborted, recall=recall)
    # durable 只看**实际落点**：目标是图谱而 16 条全落在内存时，写"durable=true"就是假事实
    durable = state.driver == "graphiti" and summary["landed"] == summary["total"] > 0
    summary.update(
        {
            "status": "aborted" if aborted else "ingested",
            "target_driver": state.driver,
            # durable=false 是说这批里至少有一条跟着进程一起消失，不是"写失败了"
            "durable": durable,
            "recalled_from": dict(recall or {}),
            "schema_error": schema_error,
            "close_error": close_error,
        }
    )
    return summary, exit_code(summary)


def exit_reason(summary: dict[str, Any]) -> str:
    if summary.get("status") == "refused":
        return str(summary.get("reason", ""))
    if summary.get("aborted"):
        return f"中止于中途: {summary['aborted']}"
    degraded = int(summary.get("degraded") or 0)
    if degraded:
        return f"{degraded} 条没进图谱，落在兜底腿（看 outcomes[].reason）"
    missing = summary.get("recall_missing") or []
    if missing:
        return f"{len(missing)} 条写入成功却召回不到: {missing}"
    return ""


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary, code = asyncio.run(run(args))
    except (OSError, ValueError, AegisError) as exc:
        # 读不到文件、id 拼错、案例库不合法：都是运维能当场改的输入问题，不该甩堆栈
        print(json.dumps({"status": "invalid", "reason": str(exc)}, ensure_ascii=False, indent=2))
        print("整批未导入。", file=sys.stderr)
        return EXIT_INVALID
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if code != EXIT_OK:
        print(f"退出码 {code}：{exit_reason(summary)}", file=sys.stderr)
    elif summary.get("durable") is False:
        # 成功但落点跟着进程消失：只写在 JSON 里太容易被读成"灌好了"
        print("提醒：这批案例只活在本进程里（target_driver=in_memory）。", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
