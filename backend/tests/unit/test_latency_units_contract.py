"""时延账本出口的单位契约。

坑在这里：账本的值按指标名后缀取单位（`*_ms` 毫秒、`*_seconds` 秒），可出口 JSON 一度把
所有分位数都叫 `p50_ms / p95_ms / budget_ms`。于是 `ingest_end_to_end_seconds` 的 0.019
（秒）配着 `_ms` 的键名出去，指标页、`metrics_report.py` 与助手的 `query.metrics` 三处
各自"按后缀猜单位"，猜法还不一致。判定不会变红（两侧同为秒才可比），骗人的只有键名——
这种缺陷只会让人对着一行绿字得出错 1000 倍的结论。

现在键名中性、单位显式，且只有一个判定处（`unit_of_metric`）。这几条用例钉的就是这三件事。
"""

from __future__ import annotations

import pytest

from aegis.observability.metrics import MetricsExporter
from aegis.observability.tracer import LatencyLedger, LatencyStats, millis_of, unit_of_metric

NEUTRAL_KEYS = ("p50", "p95", "p99", "max", "mean")


def test_指标名后缀就是单位判定处() -> None:
    assert unit_of_metric("warning_generation_ms") == "ms"
    assert unit_of_metric("ingest_end_to_end_seconds") == "s"
    assert unit_of_metric("report_intake_seconds") == "s"
    # 无后缀的历史名字保持毫秒：账本默认单位没变，不能因为加了判定就把它们改成秒
    assert unit_of_metric("collab_txn") == "ms"


def test_出口键名不声称单位而单位单独声明() -> None:
    ledger = LatencyLedger()
    ledger.set_budget("ingest_end_to_end_seconds", 300.0)
    ledger.record("ingest_end_to_end_seconds", 0.019)
    payload = ledger.stats("ingest_end_to_end_seconds").as_dict()
    assert payload["unit"] == "s"
    assert payload["p95"] == pytest.approx(0.019)
    assert payload["budget"] == 300.0
    for key in (*NEUTRAL_KEYS, "budget"):
        assert f"{key}_ms" not in payload, "秒制指标不得再带 `_ms` 键名出去"


def test_毫秒指标出口仍是毫秒单位() -> None:
    ledger = LatencyLedger()
    ledger.set_budget("warning_generation_ms", 180_000.0)
    ledger.record("warning_generation_ms", 4200.0)
    payload = ledger.stats("warning_generation_ms").as_dict()
    assert payload["unit"] == "ms"
    assert payload["budget"] == 180_000.0
    assert "budget_s" not in payload


def test_没有预算时也带得上单位() -> None:
    """单位是每条指标的属性，不因为有阈值才声明——`breaches` 才是阈值专属的字段。"""
    ledger = LatencyLedger()
    ledger.record("ingest_publish_ms", 3.5)
    payload = ledger.stats("ingest_publish_ms").as_dict()
    assert payload["unit"] == "ms"
    assert "budget" not in payload and "breaches" not in payload


def test_零样本的指标也知道自己是什么单位() -> None:
    """报表与页面都要在"没样本"时照常显示单位：判定缺的是样本，不是单位。"""
    ledger = LatencyLedger()
    ledger.set_budget("report_intake_seconds", 300.0)
    stats = ledger.stats("report_intake_seconds")
    assert stats.count == 0
    payload = stats.as_dict()
    assert payload["unit"] == "s" and payload["budget"] == 300.0 and payload["count"] == 0


def test_未知单位直接抛错而不是默认毫秒() -> None:
    """手搓一个错单位的统计对象：出口必须炸，不能"看起来像毫秒"地放行。"""
    stats = LatencyStats(count=1, p50=1.0, p95=1.0, p99=1.0, max=1.0, mean=1.0, unit="min")
    with pytest.raises(ValueError, match="未知单位"):
        stats.as_dict()


def test_快照与逐条出口给的是同一形状() -> None:
    ledger = LatencyLedger()
    ledger.record("ingest_end_to_end_seconds", 0.5)
    ledger.record("warning_generation_ms", 10.0)
    snapshot = ledger.snapshot()
    assert snapshot["ingest_end_to_end_seconds"]["unit"] == "s"
    assert snapshot["warning_generation_ms"]["unit"] == "ms"
    for payload in snapshot.values():
        assert set(NEUTRAL_KEYS) <= set(payload)


def test_毫秒直方图灌入前先按单位换算() -> None:
    """Prometheus 侧的 1000 倍误差就出在这里：直方图叫 `aegis_latency_ms`，灌秒值进去没人报警。"""
    assert millis_of("ingest_end_to_end_seconds", 0.007) == pytest.approx(7.0)
    assert millis_of("warning_generation_ms", 4200.0) == pytest.approx(4200.0)


def test_指标名后缀必须落在合法集合里() -> None:
    """`unit_of_metric` 认不出后缀时回落毫秒——所以"没后缀"必须是有名单依据的，不能靠运气。

    登记在预算表里的名字就是考核指标全集；有人新加一条 `foo_minutes` 而不进这个集合，
    它会静默按毫秒判定，正是这份出口最坏的失效方式。
    """
    from aegis.config import Settings
    from aegis.observability.instrumentation import register_sla_budgets

    named_without_suffix = {"collab_txn"}
    budgets = register_sla_budgets(LatencyLedger(), Settings(env="test"))
    for name in budgets:
        assert name.endswith(("_ms", "_s", "_seconds")) or name in named_without_suffix, (
            f"{name} 的单位后缀非法：会被当成毫秒判定"
        )


def test_导出器把秒制样本换算成毫秒后再观测(gateway, tracer) -> None:
    """Prometheus 侧的 1000 倍误差就出在这里：直方图叫 `aegis_latency_ms`，灌秒值进去没人会报警。

    用真 gateway/tracer fixture（tests/conftest.py），不手搓替身——替身测不到导出器
    真正调用的那几个方法。
    """
    exporter = MetricsExporter(gateway, tracer)
    tracer.ledger.set_budget("ingest_end_to_end_seconds", 300.0)
    tracer.ledger.record("ingest_end_to_end_seconds", 2.0)
    text = exporter.collect().decode()
    # 直方图的 sum 必须是 2000（毫秒），不是 2（被当成 2 毫秒的秒值）
    line = next(
        line
        for line in text.splitlines()
        if line.startswith("aegis_latency_ms_sum") and 'metric="ingest_end_to_end_seconds"' in line
    )
    assert float(line.split()[-1]) == pytest.approx(2000.0)


def test_秒制指标的违约计数仍按同单位判定(gateway, tracer) -> None:
    exporter = MetricsExporter(gateway, tracer)
    tracer.ledger.set_budget("ingest_end_to_end_seconds", 300.0)
    tracer.ledger.record("ingest_end_to_end_seconds", 301.0)
    text = exporter.collect().decode()
    line = next(
        line
        for line in text.splitlines()
        if line.startswith("aegis_sla_breaches_total") and 'metric="ingest_end_to_end_seconds"' in line
    )
    assert float(line.split()[-1]) == 1.0
