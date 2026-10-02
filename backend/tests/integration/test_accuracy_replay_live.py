"""真值标注入库 + 与落库 `warnings` 配对：整条链在真 Postgres 上跑通。

单测里的假连接只能证明 SQL 长什么样，证不了三件只有真库能回答的事：
① `LEFT JOIN LATERAL ... LIMIT 1` 真的取到"事件之后第一条预警"；
② **事件之前就已存在的预警不算命中**（这是"预报准确率"与"它本来就报了"的分界）；
③ 按 `case_id` 重复导入是修标注，不是撞主键，也不是攒重复行。

运行方式与 `test_persistence_postgres.py` 相同（`AEGIS_TEST_PG_DSN=…`）。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import pytest

from aegis.domain.enums import HazardType, RiskLevel
from aegis.domain.messages import WarningRecord, utc_now
from aegis.persistence.accuracy import DEFAULT_WINDOW_SECONDS
from aegis.persistence.postgres import PostgresStore
from aegis.persistence.replay import ReplayDataset, measure

DSN = os.getenv("AEGIS_PG_DSN") or os.getenv("AEGIS_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="未设置 AEGIS_TEST_PG_DSN / AEGIS_PG_DSN，跳过真实 Postgres 测试")

REGION = "540199"  # 只用于本文件的门禁站号，避开拉萨真实编号段
OTHER_REGION = "540198"


def _record(*, warning_id: str, region: str, generated_at: datetime, level: RiskLevel = RiskLevel.ORANGE) -> WarningRecord:
    return WarningRecord(
        event_id=f"evt-{warning_id}",
        trace_id=f"trc-{warning_id}",
        hazard_type=HazardType.DEBRIS_FLOW,
        region_codes=[region],
        risk_level=level,
        title_zh="泥石流风险橙色预警（回放门禁）",
        body_zh="回放门禁写入的样例预警，只用于验证配对窗口。",
        generated_at=generated_at.isoformat().replace("+00:00", "Z"),
        channels=[],
    )


def _label(*, case_id: str, observed_at: datetime, truth: bool, region: str = REGION, level: int | None = 3) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "region_code": region,
        "hazard_type": HazardType.DEBRIS_FLOW.value,
        "observed_at": observed_at,
        "truth_warning": truth,
        "truth_level": level,
        "source": "live-gate",
        "labelled_by": "pytest",
    }


@pytest.fixture
async def store() -> AsyncIterator[PostgresStore]:
    pg = PostgresStore(dsn=DSN, batch_rows=50)
    await pg.connect(start_buffer=False)
    await pg.migrate()
    pool = pg.require_pool()
    # 只清自己这批 id（ACC-* / live-gate），不 TRUNCATE：那会顺手删掉别的 live 用例
    # 正在按精确集合断言的数据，而跨文件的"谁的断言先跑"是没人能预测的顺序。
    await pool.execute("DELETE FROM warning_truth_labels WHERE source = 'live-gate'")
    await pool.execute("DELETE FROM warnings WHERE trace_id LIKE 'trc-ACC-%'")
    yield pg
    await pg.close()


async def _seed(store: PostgresStore, *records: WarningRecord) -> None:
    for record in records:
        await store.warnings.put(record)
    await store.flush()


class TestReplayPairsWithStoredWarnings:
    async def test_事件之后的预警算命中并带提前量(self, store: PostgresStore) -> None:
        base = utc_now().replace(microsecond=0)
        await _seed(store, _record(warning_id="ACC-A", region=REGION, generated_at=base + timedelta(minutes=20)))
        imported = await store.put_warning_labels([_label(case_id="ACC-A", observed_at=base, truth=True)])
        assert imported == 1

        cases = await store.accuracy_replay_cases(since=base - timedelta(hours=1), window_seconds=DEFAULT_WINDOW_SECONDS)
        assert [(case.case_id, case.cell) for case in cases] == [("ACC-A", "tp")]
        # 提前量由库里的两个时间戳相减得出，约 1200s（留 5s 给秒级取整）
        assert cases[0].lead_seconds is not None
        assert 1195 <= cases[0].lead_seconds <= 1205

    async def test_事件之前就已存在的预警不算命中(self, store: PostgresStore) -> None:
        base = utc_now().replace(microsecond=0)
        await _seed(store, _record(warning_id="ACC-B", region=REGION, generated_at=base - timedelta(hours=2)))
        await store.put_warning_labels([_label(case_id="ACC-B", observed_at=base, truth=True)])

        cases = await store.accuracy_replay_cases(since=base - timedelta(hours=6), window_seconds=DEFAULT_WINDOW_SECONDS)
        # 漏报而不是命中：把"它本来就报了"算成"它预报了"是这类指标最典型的造假方式
        assert [(case.case_id, case.cell) for case in cases] == [("ACC-B", "fn")]
        assert cases[0].predicted_warning is False

    async def test_超出配对窗的预警不算命中(self, store: PostgresStore) -> None:
        base = utc_now().replace(microsecond=0)
        await _seed(store, _record(warning_id="ACC-C", region=REGION, generated_at=base + timedelta(hours=3)))
        await store.put_warning_labels([_label(case_id="ACC-C", observed_at=base, truth=True)])
        cases = await store.accuracy_replay_cases(since=base - timedelta(hours=1), window_seconds=3600)
        assert [case.cell for case in cases] == ["fn"]

    async def test_别的区县的预警不会串到本区县的案例上(self, store: PostgresStore) -> None:
        base = utc_now().replace(microsecond=0)
        await _seed(store, _record(warning_id="ACC-D", region=OTHER_REGION, generated_at=base + timedelta(minutes=5)))
        await store.put_warning_labels([_label(case_id="ACC-D", observed_at=base, truth=True)])
        cases = await store.accuracy_replay_cases(since=base - timedelta(hours=1), window_seconds=3600)
        assert [case.cell for case in cases] == ["fn"]

    async def test_多灾种在同区县只取最早一条产出(self, store: PostgresStore) -> None:
        base = utc_now().replace(microsecond=0)
        await _seed(
            store,
            _record(warning_id="ACC-E-late", region=REGION, generated_at=base + timedelta(minutes=40)),
            _record(warning_id="ACC-E-early", region=REGION, generated_at=base + timedelta(minutes=10)),
        )
        await store.put_warning_labels([_label(case_id="ACC-E", observed_at=base, truth=True)])
        cases = await store.accuracy_replay_cases(since=base - timedelta(hours=1), window_seconds=DEFAULT_WINDOW_SECONDS)
        assert cases[0].lead_seconds is not None
        assert cases[0].lead_seconds < 1200  # 取的是最早那条（10 分钟），不是 40 分钟那条


class TestLabelImportIsIdempotent:
    async def test_重复导入是修标注而不是攒重复行(self, store: PostgresStore) -> None:
        base = utc_now().replace(microsecond=0)
        await store.put_warning_labels([_label(case_id="ACC-F", observed_at=base, truth=True, level=3)])
        await store.put_warning_labels([_label(case_id="ACC-F", observed_at=base, truth=True, level=4)])
        rows = await store.require_pool().fetch("SELECT truth_level FROM warning_truth_labels WHERE case_id='ACC-F'")
        assert len(rows) == 1
        assert rows[0]["truth_level"] == 4

    async def test_区划代码_CHECK_与站点表同口径(self, store: PostgresStore) -> None:
        # 小写区号会被 normalize_label 规整成大写；绕不过去就落不进表，也就不会静默配不上。
        base = utc_now().replace(microsecond=0)
        await store.put_warning_labels([_label(case_id="ACC-G", observed_at=base, truth=True, region=" 540197 ")])
        rows = await store.require_pool().fetch("SELECT region_code FROM warning_truth_labels WHERE case_id='ACC-G'")
        assert rows[0]["region_code"] == "540197"


class TestMeasureUsesTheSingleOracle:
    async def test_库侧配对与_JSONL_走同一个_measure(self, store: PostgresStore) -> None:
        base = utc_now().replace(microsecond=0)
        await _seed(store, _record(warning_id="ACC-H", region=REGION, generated_at=base + timedelta(minutes=15)))
        await store.put_warning_labels(
            [
                _label(case_id="ACC-H", observed_at=base, truth=True),
                _label(case_id="ACC-H-miss", observed_at=base + timedelta(minutes=30), truth=True, region=OTHER_REGION),
            ]
        )
        cases = await store.accuracy_replay_cases(since=base - timedelta(hours=1), window_seconds=DEFAULT_WINDOW_SECONDS)
        report = measure(ReplayDataset(cases=tuple(cases), kind="field", source="live-gate", note="门禁样例，不构成官方准确率证据"))
        assert (report.confusion.tp, report.confusion.fn) == (1, 1)
        # 样本不足 30 条时一律不报官方准确率：这条由 measure 判，不由脚本判
        assert report.official_accuracy is None
        assert report.status == "insufficient_sample"
        assert report.dataset_kind == "field"

    async def test_时间戳形状可与报表一起序列化(self, store: PostgresStore) -> None:
        base = utc_now().replace(microsecond=0)
        await store.put_warning_labels([_label(case_id="ACC-I", observed_at=base, truth=True)])
        cases = await store.accuracy_replay_cases(since=base - timedelta(hours=1), window_seconds=3600)
        payload = measure(ReplayDataset(cases=tuple(cases), kind="unspecified", source="live-gate", note="")).as_dict()
        assert payload["dataset"]["kind"] == "unspecified"
        assert payload["official_accuracy"] is None
        # 数据集没声明成现场标注时，报表必须自带一句"这不算官方准确率证据"
        assert payload["provenance_warning"] is not None
