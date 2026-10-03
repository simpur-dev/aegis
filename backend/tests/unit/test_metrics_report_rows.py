"""CI 指标报表里"接入 ≤5min"两行的单位口径。

这两行原本都栽在同一个坑里：指标本身按**秒**记账本，报表却拿它去和 300000（毫秒）比，
于是任何时延都判"达标"——一行永远绿的判定比没有判定更糟（容器侧的同类事故见
`container.latency_report` 的注释）。这里把两行都钉成可变异用例：给一个明显超 5 分钟的
秒数，报表必须说"超标"。
"""

from __future__ import annotations

from typing import Any

from scripts.metrics_report import REPORT_INTAKE_INDICATOR, _table


def _latency(**metrics: dict[str, Any]) -> dict[str, Any]:
    return {"metrics": metrics, "collaboration": {"success_rate": None, "transactions": 0, "target": 0.9}}


def _row(rows: list[dict[str, Any]], description: str) -> dict[str, Any]:
    return next(row for row in rows if row["指标"] == description)


def test_上报腿在阈值内判达标且带真实样本数() -> None:
    description = REPORT_INTAKE_INDICATOR[2]
    rows = _table(
        _latency(report_intake_seconds={"count": 20, "p50_ms": 0.621, "p95_ms": 1.18, "max_ms": 1.242, "breaches": 0}),
        {"success_rate": None, "transactions": 0, "target": 0.9},
    )
    row = _row(rows, description)
    assert row["样本"] == 20
    assert row["判定"] == "达标"
    assert row["阈值_s"] == 300.0, "阈值必须以秒示人，否则读表的人会以为这是毫秒"
    assert row["P95_s"] == 1.18


def test_上报腿超过五分钟必须判超标() -> None:
    """变异点：把秒数当成毫秒比的那版代码在这里会给出"达标"。"""
    description = REPORT_INTAKE_INDICATOR[2]
    rows = _table(
        _latency(report_intake_seconds={"count": 5, "p50_ms": 410.0, "p95_ms": 512.0, "max_ms": 600.0, "breaches": 3}),
        {"success_rate": None, "transactions": 0, "target": 0.9},
    )
    row = _row(rows, description)
    assert row["判定"] == "超标"
    assert row["越限样本"] == 3


def test_没有样本时如实写未测得而不是零() -> None:
    description = REPORT_INTAKE_INDICATOR[2]
    rows = _table(_latency(), {"success_rate": None, "transactions": 0, "target": 0.9})
    row = _row(rows, description)
    assert row["判定"] == "未测得"
    assert row["样本"] == 0
    assert row["P95_s"] is None


def test_遥测与上报两条接入腿用同一套秒制口径() -> None:
    """两行都得叫 `_s`：一处秒一处毫秒的话，读表的人会把 0.007 当成毫秒级接入。"""
    latency = _latency(
        ingest_end_to_end_seconds={"count": 300, "p50_ms": 0.004, "p95_ms": 0.007, "max_ms": 0.01, "breaches": 0},
        report_intake_seconds={"count": 2, "p50_ms": 0.6, "p95_ms": 1.2, "max_ms": 1.3, "breaches": 0},
    )
    rows = _table(latency, {"success_rate": None, "transactions": 0, "target": 0.9})
    seconds_rows = [row for row in rows if "阈值_s" in row]
    assert {row["指标"] for row in seconds_rows} == {
        "多源数据接入时延 ≤5分钟（秒制）",
        REPORT_INTAKE_INDICATOR[2],
    }
    assert all("P95_s" in row and "P95_ms" not in row for row in seconds_rows)
