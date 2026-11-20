"""遥测存储的 region 分桶索引：与全表扫**语义等价**，且淘汰必须同步。

分桶是 2026-11-20 的性能改造（窗口查询总是带 region，不该每次扫 5 万条）。
新状态就有新的出错方式，所以这里用"朴素全扫"作参照实现逐项对账：
- 返回条数/顺序/内容一致（含 limit 取最新）；
- 不带 region 的查询仍走全量；
- 超过上限淘汰时，桶与主集合同步收缩（否则桶无界增长）。
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from aegis.domain.messages import TelemetryReading
from aegis.storage.store import TelemetryStore

BASE = datetime(2026, 11, 20, tzinfo=UTC)


def _reading(index: int, *, region: str, station: str, metric: str = "rain_10min") -> TelemetryReading:
    moment = BASE + timedelta(minutes=index)
    return TelemetryReading(
        station_id=station,
        metric=metric,
        value=float(index % 17),
        unit="mm",
        region_code=region,
        observed_at=moment.isoformat(),
        ingested_at=moment.isoformat(),
        source="test",
        quality_flag="ok",
    )


def _brute_force(
    rows: list[TelemetryReading],
    *,
    region: str | None = None,
    station: str | None = None,
    metric: str | None = None,
    limit: int = 500,
) -> list[TelemetryReading]:
    matched = [
        row
        for row in rows
        if (region is None or row.region_code == region)
        and (station is None or row.station_id == station)
        and (metric is None or row.metric == metric)
    ]
    return matched[-limit:]


async def test_分桶查询与全表扫逐项一致() -> None:
    store = TelemetryStore()
    rng = random.Random(7)
    rows: list[TelemetryReading] = []
    for index in range(300):
        region = rng.choice(["540121", "540221", "540321"])
        station = f"W-{rng.randrange(6)}"
        metric = rng.choice(["rain_10min", "rain_cumulative_24h", "soil_moisture"])
        row = _reading(index, region=region, station=station, metric=metric)
        rows.append(row)
    await store.add(rows)

    for region in ("540121", "540221", "540321"):
        assert store.query(region_code=region, limit=50) == _brute_force(rows, region=region, limit=50)
        assert store.query(region_code=region, station_id="W-1", limit=20) == _brute_force(rows, region=region, station="W-1", limit=20)
        assert store.query(region_code=region, metric="rain_10min", limit=30) == _brute_force(
            rows, region=region, metric="rain_10min", limit=30
        )
    # 不带 region：仍走全量快照
    assert store.query(limit=40) == _brute_force(rows, limit=40)


async def test_淘汰时桶与主集合同步收缩() -> None:
    store = TelemetryStore(maxlen=10)
    rows = [_reading(index, region="540121" if index % 2 else "540221", station="W-1") for index in range(25)]
    await store.add(rows)

    assert store.size == 10
    bucket_total = sum(len(bucket) for bucket in store._by_region.values())
    assert bucket_total == 10, "桶必须随主集合一起收缩（否则旁路索引无界增长）"
    retained = rows[-10:]
    for region in ("540121", "540221"):
        assert store.query(region_code=region, limit=100) == _brute_force(retained, region=region, limit=100)


async def test_单区域持续写入时旧读数滚出() -> None:
    store = TelemetryStore(maxlen=5)
    rows = [_reading(index, region="540121", station="W-1") for index in range(12)]
    await store.add(rows)
    assert store.query(region_code="540121", limit=100) == rows[-5:]


@pytest.mark.parametrize("region", ["540121", "无读数区"])
async def test_空桶与未知区域返回空(region: str) -> None:
    store = TelemetryStore()
    await store.add([_reading(0, region="540121", station="W-1")])
    if region == "无读数区":
        assert store.query(region_code=region) == []
    else:
        assert len(store.query(region_code=region)) == 1
