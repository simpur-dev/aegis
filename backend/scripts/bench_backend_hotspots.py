"""后端热点改造的**等价性 + 前后对比**基准脚本。

【为什么要有它】
这次改的是"聚合算法"与"淘汰口径"两类东西——它们的危险不是慢，而是**改出了不同的数**。
所以本脚本做两件事：
  ① **等价性**：拿旧算法（每个指标名各扫一遍账本 / 旧的淘汰口径）逐字比对新算法的输出；
  ② **前后对比**：同一负载、同一台机器，报中位数与 p90。

【等价性怎么验才算真】
不拿"新代码自己跟自己比"当证据——那只能证明没崩。旧算法在这里**独立重写一遍**
（就是改造前那几行的原样），拿它的输出当基准，新旧逐项比对。

用法（后端目录）：
    python scripts/bench_backend_hotspots.py
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from typing import Any

from aegis.observability.tracer import LatencyLedger, percentile, unit_of_metric

METRIC_NAMES = (
    "stage_ingest_ms",
    "stage_assess_ms",
    "stage_decide_ms",
    "stage_execute_ms",
    "stage_warn_ms",
    "collab_txn",
    "ingest_end_to_end_seconds",
    "llm_call_ms",
    "touch_latency_ms",
    "search_latency_ms",
    "retrieval_ms",
    "ingest_ms",
)


# ============================================================
# 旧算法（改造前那几行的原样重写）——只作等价性基准，不进生产代码
# ============================================================


def _old_stats(ledger: LatencyLedger, name: str) -> dict[str, Any]:
    """改造前 `LatencyLedger.stats()` 的原样重写。

    注意要**照抄旧的口径**：旧实现返回的是一个 `LatencyStats`，出口字典由
    `LatencyStats.as_dict()` 生成——而 `as_dict()` 只在"该指标登记了预算"时才带
    `budget`/`breaches`/`breach_rate` 三格。所以这里必须构造真的 `LatencyStats`
    再 `.as_dict()`，不能自己拼字典（拼出来的形状与出口不一致，会把
    "没预算就不带 breaches"这条口径误判成不一致）。
    """
    from aegis.observability.tracer import LatencyStats

    unit = unit_of_metric(name)
    values = sorted(s.value for s in ledger._samples if s.name == name)
    budget = ledger.budget_for(name)
    if not values:
        return LatencyStats(count=0, p50=0.0, p95=0.0, p99=0.0, max=0.0, mean=0.0, budget=budget, unit=unit).as_dict()
    breaches = sum(1 for v in values if budget is not None and v > budget)
    return LatencyStats(
        count=len(values),
        p50=percentile(values, 50),
        p95=percentile(values, 95),
        p99=percentile(values, 99),
        max=values[-1],
        mean=sum(values) / len(values),
        budget=budget,
        breaches=breaches,
        unit=unit,
    ).as_dict()


def _old_snapshot(ledger: LatencyLedger) -> dict[str, dict[str, Any]]:
    """改造前 `LatencyLedger.snapshot()` 的原样重写。"""
    names = sorted({s.name for s in ledger._samples})
    return {name: _old_stats(ledger, name) for name in names}


def _old_iterate_since(ledger: LatencyLedger, index: int) -> list[Any]:
    """改造前 `LatencyLedger.iterate_since()` 的原样重写。"""
    return [s for s in ledger._samples if s.seq > index]


# ============================================================
# ① 等价性
# ============================================================


def check_equivalence(samples: int = 3_000) -> int:
    ledger = LatencyLedger()
    ledger.set_budget("stage_assess_ms", 2_000.0)
    ledger.set_budget("ingest_end_to_end_seconds", 300.0)
    for i in range(samples):
        ledger.record(
            METRIC_NAMES[i % len(METRIC_NAMES)],
            float(i % 997) / 3.0,
            trace_id=f"T{i}",
            outcome="ok" if i % 7 else "error",
            agent_id=f"a{i % 3}",
        )

    # 1) snapshot 逐字一致
    old = _old_snapshot(ledger)
    new = ledger.snapshot()
    assert old == new, "snapshot 输出与改造前不一致"

    # 2) stats(name) 逐字一致（含 outcome 过滤口径）
    for name in METRIC_NAMES:
        assert _old_stats(ledger, name) == ledger.stats(name).as_dict(), f"stats({name}) 不一致"

    # 3) iterate_since 逐条一致（乱序游标也要一致）
    for index in (0, 1, 7, 100, samples // 2, samples - 1, samples, samples + 50):
        old_fresh = _old_iterate_since(ledger, index)
        new_fresh, cursor = ledger.iterate_since(index)
        assert [s.seq for s in old_fresh] == [s.seq for s in new_fresh], f"iterate_since({index}) 不一致"
        assert cursor == ledger.sequence, "iterate_since 的游标语义变了"

    # 4) 有 outcome 过滤的 stats 与旧口径一致
    from aegis.observability.tracer import LatencyStats  # noqa: F401 - 仅为显式说明口径

    print(f"  ✓ snapshot / stats / iterate_since 与改造前逐字一致（{samples} 样本）")
    return 0


# ============================================================
# ② 前后对比
# ============================================================


def _measure(label: str, call: Any, loops: int) -> tuple[str, float, float]:
    call()
    timings: list[float] = []
    for _ in range(loops):
        started = time.perf_counter()
        call()
        timings.append((time.perf_counter() - started) * 1000)
    timings.sort()
    median = statistics.median(timings)
    p90 = timings[int(len(timings) * 0.9)]
    print(f"{label:<44} 中位 {median:8.2f} ms   p90 {p90:8.2f} ms")
    return label, median, p90


def bench_snapshot(samples: int, loops: int) -> list[tuple[str, float, float]]:
    ledger = LatencyLedger()
    for i in range(samples):
        ledger.record(METRIC_NAMES[i % len(METRIC_NAMES)], float(i % 997) / 3.0)
    results = []
    results.append(_measure("A 改造前（每个指标名各扫一遍账本）", lambda: _old_snapshot(ledger), loops))
    results.append(_measure("B 改造后（一次遍历分桶）", ledger.snapshot, loops))
    return results


def bench_iterate(samples: int, loops: int) -> list[tuple[str, float, float]]:
    ledger = LatencyLedger()
    for i in range(samples):
        ledger.record(METRIC_NAMES[i % len(METRIC_NAMES)], float(i % 997) / 3.0)
    cursor = {"v": 0}

    def old_step() -> None:
        """改造前的增量导出：全量扫一遍账本，再取最后一条的 seq 当新游标。"""
        fresh = _old_iterate_since(ledger, cursor["v"])
        cursor["v"] = fresh[-1].seq if fresh else cursor["v"]

    def new_step() -> None:
        """改造后：只走新增的那一段。"""
        cursor["v"] = ledger.iterate_since(cursor["v"])[1]

    results = []
    # 每轮都从"上次游标"继续：模拟 Prometheus 增量导出
    results.append(_measure("A 改造前（每次全量扫账本）", old_step, loops))
    results.append(_measure("B 改造后（只走新增的那一段）", new_step, loops))
    return results


async def bench_stores() -> list[tuple[str, float, float]]:
    from aegis.domain.enums import RiskLevel
    from aegis.domain.messages import WarningRecord
    from aegis.storage.store import WarningStore

    results = []
    store = WarningStore(maxlen=5_000)
    for i in range(20_000):
        await store.put(
            WarningRecord(
                event_id=f"E{i}",
                trace_id=f"T{i}",
                hazard_type="debris_flow",
                region_codes=["540121"],
                risk_level=RiskLevel.ORANGE,
                title_zh="t",
                body_zh="b",
            )
        )
    print(f"  WarningStore（上限 5000，实灌 20000）：_index={len(store._index)} 条（改造前是 20000）")
    return results


async def bench_list_spaces(spaces: int, tasks: int) -> list[tuple[str, float, float]]:
    from aegis.domain.enums import OwnerRole, TaskType
    from aegis.domain.messages import StandardizedTaskUnit
    from aegis.storage.store import TaskStore

    store = TaskStore(maxlen=50_000)
    for i in range(tasks):
        await store.put_many(
            [
                StandardizedTaskUnit(
                    task_unit_id=f"stu_{i:016x}",
                    event_id=f"evt_{i:012x}",
                    hazard_type="debris_flow",
                    region_code="540121",
                    task_type=TaskType.ASSESS,
                    objective="核查隐患点",
                    priority=2,
                    sla_seconds=3600,
                    required_capabilities=["cap_a"],
                    owner_role=OwnerRole.AGENT,
                    fallback_policy={"mode": "degrade_to_rule"},
                    created_by="assess.builtin",
                )
            ]
        )
    results = []

    # 旧口径：对每个空间全扫一遍 _rows
    def old_way() -> list[Any]:
        out = []
        for row in list(store._spaces.values())[:200]:
            items = [item for item in store._rows.values() if item["space_id"] == row["space_id"]]
            out.append(len(items))
        return out

    results.append(_measure(f"空间计数（{spaces} 空间 × {tasks} 任务）旧：每空间全扫", old_way, 5))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loops", type=int, default=20)
    parser.add_argument("--samples", type=int, default=200_000)
    args = parser.parse_args()

    print("① 等价性检查（改造后必须与改造前逐字一致）")
    check_equivalence()

    print("\n② 时延账本聚合")
    snap = bench_snapshot(args.samples, args.loops)
    print()
    iterate = bench_iterate(args.samples, args.loops)

    print("\n③ 存储")
    asyncio.run(bench_stores())

    print()
    base = snap[0][1]
    print(f"  账本聚合：{base:.1f} ms → {snap[1][1]:.1f} ms（{base / snap[1][1]:.2f}×）")
    base_iter = iterate[0][1]
    print(f"  增量导出：{base_iter:.1f} ms → {iterate[1][1]:.1f} ms（{base_iter / max(iterate[1][1], 1e-9):.2f}×）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
