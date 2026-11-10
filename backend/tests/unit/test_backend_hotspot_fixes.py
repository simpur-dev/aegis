"""后端热点改造的边界用例（2026-11-10 性能批）。

【这批改了什么】
四件事，都是"改了会得出不同的数"的改动，所以每条都必须钉住：
  ① 时延账本聚合：从"每个指标名各扫一遍账本"改成"一次遍历分桶"；
  ② 增量导出 `iterate_since`：从"每次全量扫账本"改成"只走新增的那一段"；
  ③ Prometheus 导出：不再为了拿"在线智能体数"这一个整数去算整份时延快照；
  ④ 两个内存存储的旁路索引：此前从不淘汰（无界增长），现在与主集合同步收缩。

【钉住什么】
  - **输出逐字一致**：① ② 的新算法必须与旧算法的输出**逐项相等**
    （旧算法在本文件里独立重写一遍当基准，不拿新代码自己跟自己比）；
  - **口径没被顺手动过**：分位数、违约计数、单位、`breaches` 的出场条件
    （没登记预算就不带这三格）都按原样；
  - **淘汰语义**：索引与主集合同步收缩，且 `get()` 与 `list()` 对
    "哪些还在"给出同一个答案（此前两者不一致：列表已淘汰，`get()` 还能捞到）。
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.domain.enums import RiskLevel
from aegis.domain.messages import StandardizedTaskUnit, WarningRecord
from aegis.observability.tracer import LatencyLedger, LatencyStats, percentile, unit_of_metric
from aegis.storage.store import TaskStore, WarningStore

METRIC_NAMES = (
    "stage_ingest_ms",
    "stage_assess_ms",
    "collab_txn",
    "ingest_end_to_end_seconds",
    "llm_call_ms",
)


# ============================================================
# 旧算法基准（独立重写，只用于比对）
# ============================================================


def _old_stats(ledger: LatencyLedger, name: str) -> dict[str, Any]:
    """改造前 `LatencyLedger.stats()` 的原样重写（返回真的出口字典形状）。"""
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


def _filled_ledger(samples: int = 2_000) -> LatencyLedger:
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
    return ledger


# ============================================================
# ① 账本聚合：输出逐字一致
# ============================================================


def test_snapshot与改造前逐字一致() -> None:
    ledger = _filled_ledger()
    assert _old_snapshot(ledger) == ledger.snapshot()


def test_stats与改造前逐字一致() -> None:
    ledger = _filled_ledger()
    for name in METRIC_NAMES:
        assert _old_stats(ledger, name) == ledger.stats(name).as_dict(), f"stats({name}) 口径变了"


def test_snapshot的键顺序与改造前一致() -> None:
    """出口字典的键序也是输出的一部分（报表与快照按序读）。"""
    ledger = _filled_ledger()
    assert list(_old_snapshot(ledger)) == list(ledger.snapshot())


def test_没登记预算的指标不带breaches() -> None:
    """`breaches` 三格是"预算专属"：没预算就不带——这条口径不能被顺手改掉。"""
    ledger = LatencyLedger()
    ledger.record("stage_ingest_ms", 3.5)
    payload = ledger.snapshot()["stage_ingest_ms"]
    assert "budget" not in payload and "breaches" not in payload and "breach_rate" not in payload


def test_stats_all与逐个stats一致() -> None:
    """分桶出口与逐指标口径必须一致（否则两条读面会给出不同的数）。"""
    ledger = _filled_ledger()
    all_stats = ledger.stats_all()
    assert set(all_stats) == set(ledger.names())
    for name, stats in all_stats.items():
        assert stats.as_dict() == ledger.stats(name).as_dict()


def test_outcome过滤口径不变() -> None:
    """`stats(name, outcome=...)` 的过滤语义没变（只数该 outcome 的样本）。"""
    ledger = LatencyLedger()
    ledger.record("collab_txn", 1.0, outcome="ok")
    ledger.record("collab_txn", 2.0, outcome="error")
    assert ledger.stats("collab_txn", outcome="error").count == 1
    assert ledger.stats("collab_txn", outcome="ok").count == 1
    assert ledger.stats("collab_txn").count == 2


def test_空账本不出错() -> None:
    ledger = LatencyLedger()
    assert ledger.snapshot() == {}
    assert ledger.stats_all() == {}


def test_只有一条样本时p50不报错() -> None:
    ledger = LatencyLedger()
    ledger.record("collab_txn", 7.0)
    payload = ledger.snapshot()["collab_txn"]
    assert payload["count"] == 1
    assert payload["p50"] == pytest.approx(7.0)


# ============================================================
# ② 增量导出：与"全量扫 + 过滤"逐条一致
# ============================================================


@pytest.mark.parametrize("index", [0, 1, 7, 100, 1_000, 1_999, 2_000, 5_000])
def test_iterate_since与全量过滤逐条一致(index: int) -> None:
    ledger = _filled_ledger()
    expected = [s.seq for s in ledger._samples if s.seq > index]
    fresh, cursor = ledger.iterate_since(index)
    assert [s.seq for s in fresh] == expected
    assert cursor == ledger.sequence


def test_iterate_since返回的顺序是写入顺序() -> None:
    ledger = _filled_ledger()
    fresh, _cursor = ledger.iterate_since(0)
    assert [s.seq for s in fresh] == sorted(s.seq for s in fresh)


def test_iterate_since游标落后于全部样本时也一致() -> None:
    """游标给一个比全部样本都新的值 → 应返回空，而不是从头再来一遍。"""
    ledger = _filled_ledger()
    fresh, cursor = ledger.iterate_since(ledger.sequence + 100)
    assert fresh == []
    assert cursor == ledger.sequence


def test_iterate_since在账本滚动后仍与全量一致() -> None:
    """账本有上限（会挤掉最旧的）：游标指向已被挤掉的样本时，不能漏也不能重。"""
    ledger = LatencyLedger(maxlen=100)
    for i in range(500):
        ledger.record("collab_txn", float(i))
    fresh, cursor = ledger.iterate_since(50)
    expected = [s.seq for s in ledger._samples if s.seq > 50]
    assert [s.seq for s in fresh] == expected
    assert cursor == ledger.sequence


# ============================================================
# ③ Prometheus 导出：不再为拿一个整数算整份快照
# ============================================================


@pytest.mark.asyncio
async def test_导出不触发时延账本的全量聚合(gateway: Any) -> None:
    """★ 这条的判据是**行为**不是时延：导出器不得再去算整份 latency 快照。

    用"打桩统计 stats_all 调用次数"来判，比断言耗时不稳（机器抖动会误红），
    也比"看代码里有没有那行"可靠。
    """
    from aegis.observability.metrics import MetricsExporter

    exporter = MetricsExporter(gateway, gateway.ledger)

    calls = {"n": 0}
    ledger = gateway.ledger.ledger  # gateway.ledger 是 Tracer，账本在它的 .ledger 上
    original = ledger.stats_all

    def counting() -> Any:
        calls["n"] += 1
        return original()

    ledger.stats_all = counting  # type: ignore[method-assign]
    exporter.collect()
    assert calls["n"] == 0, "导出器不该为了拿在线数去算整份时延快照"

    # 但"在线智能体数"这一格必须还在（不能为了省事把指标删了）
    assert "aegis_agents_online" in exporter.collect().decode("utf-8")


@pytest.mark.asyncio
async def test_在线智能体数与网关快照给的是同一个数(gateway: Any) -> None:
    """新出口 `online_count` 必须与 `snapshot()["agents_online"]` 同源同值。"""
    assert gateway.online_count == gateway.snapshot()["agents_online"]


# ============================================================
# ④ 存储：旁路索引与主集合同步收缩
# ============================================================


def _warning(index: int, *, warning_id: str = "") -> WarningRecord:
    """构造一条预警。

    `warning_id` 必须**显式给**：它默认是随机生成的，同一条记录两次构造会得到
    两个不同的 ID——"同 ID 覆盖"那条用例就测不到它要测的东西了。
    """
    payload: dict[str, Any] = {
        "event_id": f"E{index}",
        "trace_id": f"T{index}",
        "hazard_type": "debris_flow",
        "region_codes": ["540121"],
        "risk_level": RiskLevel.ORANGE,
        "title_zh": "标题",
        "body_zh": "正文",
    }
    if warning_id:
        payload["warning_id"] = warning_id
    return WarningRecord(**payload)


@pytest.mark.asyncio
async def test_预警索引与顺序队列同步收缩() -> None:
    store = WarningStore(maxlen=100)
    for i in range(1_000):
        await store.put(_warning(i))
    assert len(store._index) == 100, "索引必须跟着主集合一起收缩（改造前是 1000，无界）"
    assert len(store._order) == 100
    assert store.size == 100


@pytest.mark.asyncio
async def test_预警的get与list对_哪些还在_给同一个答案() -> None:
    """★ 改造前两者不一致：列表已淘汰最旧的，`get()` 却还能捞到它。"""
    store = WarningStore(maxlen=50)
    for i in range(200):
        await store.put(_warning(i, warning_id=f"wrn_{i:04d}"))
    assert store.get("wrn_0000") is None, "已被淘汰的预警不该还能 get 到"
    for record in store.list(limit=50):
        assert store.get(record.warning_id) is not None, "列表里的预警必须 get 得到"


@pytest.mark.asyncio
async def test_预警同id覆盖不新增条目() -> None:
    """同 ID 覆盖而不是追加（发布/撤回/改级写同一 warning_id）。"""
    store = WarningStore(maxlen=50)
    await store.put(_warning(1, warning_id="wrn_same"))
    await store.put(_warning(2, warning_id="wrn_same"))
    assert store.size == 1
    assert len(store.list(limit=50)) == 1


@pytest.mark.asyncio
async def test_预警淘汰的是最旧的() -> None:
    store = WarningStore(maxlen=10)
    for i in range(30):
        await store.put(_warning(i, warning_id=f"wrn_{i:04d}"))
    kept = {r.warning_id for r in store.list(limit=10)}
    assert "wrn_0029" in kept, "最近写入的必须还在"
    assert "wrn_0000" not in kept, "最旧的必须已被淘汰"
    assert len(kept) == 10


def _stu(index: int) -> StandardizedTaskUnit:
    from aegis.domain.enums import OwnerRole, TaskType

    return StandardizedTaskUnit(
        task_unit_id=f"stu_{index:016x}",
        event_id=f"evt_{index:012x}",
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


@pytest.mark.asyncio
async def test_任务单元索引与集合同步收缩() -> None:
    store = TaskStore(maxlen=100)
    for i in range(1_000):
        await store.put_many([_stu(i)])
    assert len(store._index) == 100, "索引必须跟着集合一起收缩（改造前是 1000，无界）"
    assert len(store._collection) == 100


@pytest.mark.asyncio
async def test_任务单元的get与list一致() -> None:
    store = TaskStore(maxlen=50)
    for i in range(200):
        await store.put_many([_stu(i)])
    assert store.get(_stu(0).task_unit_id) is None, "已被淘汰的任务单元不该还能 get 到"
    assert store.get(_stu(199).task_unit_id) is not None


@pytest.mark.asyncio
async def test_任务单元同id重写时索引不被误删() -> None:
    """同一个 id 写过多遍时，索引指向最新那份；淘汰最旧那份**不能**把索引删掉。"""
    store = TaskStore(maxlen=2)
    first = _stu(1)
    await store.put_many([first])
    second = _stu(1)  # 同 id、不同对象
    await store.put_many([second])

    # 再灌一条把最旧的挤出去（集合里是 [first, second] → 挤掉 first）
    await store.put_many([_stu(2)])
    assert store.get(first.task_unit_id) is not None, "索引指向的是最新那份，不该被误删"


@pytest.mark.asyncio
async def test_有界集合的淘汰通知只发一次且发的是最旧的() -> None:
    """`BoundedCollection` 的 `on_evict` 契约：满时追加会挤掉最左端，且**先通知再追加**。"""
    from aegis.storage.store import BoundedCollection

    evicted: list[int] = []
    collection = BoundedCollection(maxlen=3, on_evict=evicted.append)
    for i in range(5):
        await collection.add(i)
    assert list(collection.snapshot()) == [2, 3, 4]
    assert evicted == [0, 1], "淘汰通知必须逐条发出且发的是被挤掉的那条"


@pytest.mark.asyncio
async def test_有界集合没设回调时行为不变() -> None:
    from aegis.storage.store import BoundedCollection

    collection = BoundedCollection(maxlen=2)
    for i in range(5):
        await collection.add(i)
    assert list(collection.snapshot()) == [3, 4]
    assert len(collection) == 2


# ============================================================
# ⑤ 空间列表：计数口径不变（N+1 → 一次遍历）
# ============================================================


@pytest.mark.asyncio
async def test_空间任务计数口径不变() -> None:
    """一次遍历算出来的计数，必须与"每个空间全扫一遍"算出来的一样。"""
    from aegis.storage.store import HandlingTaskStore

    store = HandlingTaskStore()
    for s in range(5):
        await store.ensure_space(space_id=f"SP{s}", warning_id=f"W{s}")
    for i in range(40):
        await store.ensure_task(space_id=f"SP{i % 5}", stu_id=f"U{i}")

    rows = await store.list_spaces(open_only=True, limit=50)

    # 旧口径（每个空间全扫一遍）作为基准
    expected: dict[str, tuple[int, int]] = {}
    for space in store._spaces.values():
        items = [item for item in store._rows.values() if item["space_id"] == space["space_id"]]
        expected[space["space_id"]] = (len(items), sum(1 for item in items if item["status"] != "closed"))

    for row in rows:
        total, open_count = expected[row["space_id"]]
        assert row["tasks_total"] == total
        assert row["tasks_open"] == open_count
        assert row["tasks_closed"] == total - open_count


@pytest.mark.asyncio
async def test_空间列表对没有任务的空间给出零() -> None:
    from aegis.storage.store import HandlingTaskStore

    store = HandlingTaskStore()
    await store.ensure_space(space_id="SP1", warning_id="W1")
    rows = await store.list_spaces(open_only=True, limit=50)
    assert rows[0]["tasks_total"] == 0
    assert rows[0]["tasks_open"] == 0
    assert rows[0]["tasks_closed"] == 0
