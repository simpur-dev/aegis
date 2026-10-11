"""CI 指标报表的单位口径：数值与阈值都从账本出口读，判定只比同单位的两个数。

这些用例的输入**不是手写的字典**，而是 `LatencyLedger` 真实产出的 `as_dict()`——
手写的形状会跟着改代码的人一起漂移，真出口才能证明"报表读的就是账本给的那份"。

历史事故有两起，都栽在同一个坑：指标按**秒**记账本，报表/接口却把它当毫秒——
一起是"任何时延都达标"的永绿判定（见 `container.latency_report` 注释），
一起是出口 JSON 给秒制指标贴上 `p95_ms`/`budget_ms` 的键名，读的人差 1000 倍。
"""

from __future__ import annotations

from typing import Any

import pytest
from scripts.metrics_report import INDICATORS, _table

from aegis.observability.tracer import LatencyLedger

COLLAB: dict[str, Any] = {"success_rate": None, "transactions": 0, "target": 0.9}


def ledger_metrics(recorded: dict[str, list[float]], budgets: dict[str, float]) -> dict[str, Any]:
    """按真实记账路径造出口：登记预算 → 记样本 → 取 stats().as_dict()。"""
    ledger = LatencyLedger()
    for name, budget in budgets.items():
        ledger.set_budget(name, budget)
    for name, values in recorded.items():
        for value in values:
            ledger.record(name, value)
    return {name: ledger.stats(name).as_dict() for name in ledger.names()}


def _latency(recorded: dict[str, list[float]], budgets: dict[str, float]) -> dict[str, Any]:
    return {"metrics": ledger_metrics(recorded, budgets), "collaboration": COLLAB}


def _row(rows: list[dict[str, Any]], description: str) -> dict[str, Any]:
    return next(row for row in rows if row["指标"] == description)


def test_上报腿在阈值内判达标且阈值以秒示人() -> None:
    rows = _table(
        _latency({"report_intake_seconds": [1.18]}, {"report_intake_seconds": 300.0}),
        COLLAB,
    )
    row = _row(rows, INDICATORS["report_intake_seconds"])
    assert row["样本"] == 1
    assert row["判定"] == "达标"
    assert row["阈值_s"] == 300.0, "阈值必须以秒示人，否则读表的人会以为这是 300 毫秒"
    assert row["P95_s"] == pytest.approx(1.18)


def test_上报腿超过五分钟必须判超标() -> None:
    """变异点：把 512 秒当成 512 毫秒去比 300000 毫秒的那版代码，这里会给"达标"。"""
    rows = _table(
        _latency({"report_intake_seconds": [512.0]}, {"report_intake_seconds": 300.0}),
        COLLAB,
    )
    row = _row(rows, INDICATORS["report_intake_seconds"])
    assert row["判定"] == "超标"
    assert row["越限样本"] == 1


def test_没有样本时如实写未测得而不是零() -> None:
    rows = _table(_latency({}, {"report_intake_seconds": 300.0}), COLLAB)
    row = _row(rows, INDICATORS["report_intake_seconds"])
    assert row["判定"].startswith("未测得")
    assert row["样本"] == 0
    assert row["P95_s"] is None


def test_两条接入腿用同一套秒制口径而毫秒腿仍是毫秒() -> None:
    """一处秒一处毫秒的话，读表的人会把 0.007 当成毫秒级接入；反过来把毫秒硬标成秒也一样错。"""
    latency = _latency(
        {
            "ingest_end_to_end_seconds": [0.007],
            "report_intake_seconds": [1.2],
            "warning_generation_ms": [4200.0],
        },
        {
            "ingest_end_to_end_seconds": 300.0,
            "report_intake_seconds": 300.0,
            "warning_generation_ms": 180_000.0,
        },
    )
    rows = _table(latency, COLLAB)
    seconds_rows = [row for row in rows if "阈值_s" in row]
    assert {row["指标"] for row in seconds_rows} == {
        INDICATORS["ingest_end_to_end_seconds"],
        INDICATORS["report_intake_seconds"],
    }
    assert all("P95_s" in row and "P95_ms" not in row for row in seconds_rows)
    warning = _row(rows, INDICATORS["warning_generation_ms"])
    assert warning["P95_ms"] == pytest.approx(4200.0) and warning["阈值_ms"] == 180_000.0
    assert "P95_s" not in warning


def test_出口单位与指标名后缀不一致时报表直接抛错() -> None:
    """宁可不出报表，也不出一份错 1000 倍的报表。"""
    latency = {
        "metrics": {"report_intake_seconds": {"count": 1, "unit": "ms", "p50": 1.0, "p95": 1.0, "max": 1.0}},
        "collaboration": COLLAB,
    }
    with pytest.raises(ValueError, match="不一致"):
        _table(latency, COLLAB)


def test_每个考核指标都必须在报表里出现() -> None:
    """漏一行就是漏一项考核：表里必须凑齐 INDICATORS 声明的条数（成功率那行另算）。"""
    rows = _table(_latency({}, {}), COLLAB)
    listed = {row["指标"] for row in rows}
    assert listed.issuperset(INDICATORS.values())
    assert len(rows) == len(INDICATORS) + 1, "多出来的一行是协同成功率"


def test_每行都标出口径档且10s由调度口径代表() -> None:
    """§5 A 案三档必须并排在同一张表里：评审问任何一个数，都要能顺着"档"找到它的出处。

    尤其 `collab_txn`（含模型往返）不再挂 ≤10s——那条考核由 `workflow_reschedule_ms` 代表，
    两者若混在同一档，就会出现"段预算 60s 达标、事务 10s 超标"这种自己打自己的表。
    """
    rows = _table(_latency({}, {"collab_txn": 60_000.0, "workflow_reschedule_ms": 10_000.0}), COLLAB)
    tiers = {row["指标"]: row["口径档"] for row in rows}
    assert tiers["常规任务调度响应时延 ≤2s"] == "调度档"
    assert tiers["协同事务响应 ≤60s（LLM 档：含模型往返）"] == "LLM 档"
    assert tiers["异常工况识别与重调度响应 ≤10s（调度口径）"] == "调度档"
    assert tiers["预警信息生成时间 ≤3min"] == "端到端档"
    assert tiers["多智能体协同联动成功率 ≥90%"] == "LLM 档", "成功率的分母就是这些事务，必须同档并排"
    assert all("口径档" in row for row in rows), "没有一行可以不带档"
