"""准确率回放的库侧配对层：只取配对，判定仍由 `replay.py` 唯一确定。

2026-10-02 独立审计抓到的缺口：算式、判据分支、JSONL 读数都齐了，但真值标注在 Postgres 里
**没有表**，回放是纯 Python + 文件读——于是"PostgreSQL 用于预警准确率回放"这半句不成立。
补上表与 SQL 读路径的同时，必须钉住两件事：
① SQL 不许自己算 TP/FP（否则出现两个"预警准确率"）；
② 预测侧只认观测时刻**之后**发出的预警（把事件之前就存在的预警算成命中，
   是"预报准确率"里最典型的假数字）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from scripts.accuracy_replay import parse_moment

from aegis.persistence import accuracy
from aegis.persistence.accuracy import (
    LABEL_COLUMNS,
    MAX_LABEL_ROWS,
    MAX_WINDOW_SECONDS,
    build_label_upsert,
    build_replay_rows,
    check_lead_window,
    normalize_label,
    put_labels,
    read_label_file,
    replay_rows,
)
from aegis.persistence.errors import AccuracyArgumentError
from aegis.persistence.replay import ReplayCase, ReplayDataset, case_from_row, measure, parse_case

T0 = datetime(2026, 9, 20, 3, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 20, 3, 20, tzinfo=UTC)


def label_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "case_id": "ACC-1",
        "region_code": "540102",
        "hazard_type": "debris_flow",
        "observed_at": T0,
        "truth_warning": True,
        "truth_level": 3,
        "source": "field-2026-09",
        "labelled_by": "站端值守",
        "note": "",
    }
    base.update(overrides)
    return base


def db_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "case_id": "ACC-1",
        "region_code": "540102",
        "hazard_type": "debris_flow",
        "truth_warning": True,
        "truth_level": 3,
        "observed_at": T0,
        "predicted_hazard_type": "debris_flow",
        "predicted_level": 3,
        "predicted_generated_at": T1,
    }
    base.update(overrides)
    return base


class FakeConn:
    def __init__(self, rows: list[dict[str, Any]] | None = None, *, error: BaseException | None = None) -> None:
        self.executed: list[tuple[str, list[Any]]] = []
        self.fetches: list[tuple[str, list[Any]]] = []
        self._rows = rows if rows is not None else []
        self._error = error

    async def executemany(self, sql: str, values: list[tuple[Any, ...]]) -> None:
        self.executed.append((sql, values))
        if self._error is not None:
            raise self._error

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetches.append((sql, list(args)))
        if self._error is not None:
            raise self._error
        return list(self._rows)


class TestNormalizeLabel:
    def test_合法行原样落形(self) -> None:
        row = normalize_label(label_row())
        assert row["region_code"] == "540102"
        assert row["observed_at"] == T0
        assert set(row) == set(LABEL_COLUMNS)

    @pytest.mark.parametrize("field", ["case_id", "region_code", "hazard_type", "observed_at", "truth_warning"])
    def test_核心字段缺失一律拒绝(self, field: str) -> None:
        raw = label_row()
        del raw[field]
        with pytest.raises(AccuracyArgumentError):
            normalize_label(raw)

    def test_裸时间被拒而不是按本地时区猜(self) -> None:
        with pytest.raises(AccuracyArgumentError, match="缺时区"):
            normalize_label(label_row(observed_at=T0.replace(tzinfo=None)))

    def test_truth_warning_不是布尔就拒_不做_truthy_折算(self) -> None:
        # "1"/"yes"/"" 折算成布尔会把漏报算成命中——这类默认值一律不许悄悄生效。
        with pytest.raises(AccuracyArgumentError):
            normalize_label(label_row(truth_warning="yes"))

    def test_真值等级越界被拒(self) -> None:
        with pytest.raises(AccuracyArgumentError, match="1-5"):
            normalize_label(label_row(truth_level=6))

    def test_区划代码大小写被规整(self) -> None:
        assert normalize_label(label_row(region_code=" 540102 "))["region_code"] == "540102"


class TestLeadWindow:
    @pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), MAX_WINDOW_SECONDS + 1])
    def test_配对窗非法就拒(self, bad: float) -> None:
        with pytest.raises(AccuracyArgumentError):
            check_lead_window(bad)

    def test_合法窗取整返回(self) -> None:
        assert check_lead_window(1800.0) == 1800


class TestSqlShape:
    def test_upsert_按_case_id_幂等并更新其余列(self) -> None:
        sql, values = build_label_upsert([normalize_label(label_row()), normalize_label(label_row(case_id="ACC-2"))])
        assert "ON CONFLICT (case_id) DO UPDATE" in sql
        assert len(values) == 2
        assert values[0][0] == "ACC-1"
        # 更新集里不该出现主键自己
        updates = sql.split("DO UPDATE SET", 1)[1]
        assert "case_id =" not in updates

    def test_回放查询用_LATERAL_取窗口内第一条产出(self) -> None:
        sql, args = build_replay_rows(since=T0 - timedelta(days=1), until=None, window_seconds=1800)
        assert "LEFT JOIN LATERAL" in sql
        assert "ORDER BY w.generated_at" in sql and "LIMIT 1" in sql
        assert args[0] == T0 - timedelta(days=1)
        assert args[-1] == 1800

    def test_预测侧不按灾种过滤_否则报对灾种就永远为真(self) -> None:
        sql, _ = build_replay_rows(since=T0, until=None, window_seconds=60)
        assert "w.hazard_type = l.hazard_type" not in sql
        # 但必须只认事件之后发出的预警：提前存在的预警不算命中
        assert "w.generated_at >= l.observed_at" in sql

    def test_区号过滤会多带一个绑定参数(self) -> None:
        _, plain_args = build_replay_rows(since=T0, until=None, window_seconds=60)
        scoped, scoped_args = build_replay_rows(since=T0, until=None, window_seconds=60, region_code="540102")
        assert len(scoped_args) == len(plain_args) + 1
        assert "l.region_code = $" in scoped

    def test_裸时间窗在触库之前就被拒(self) -> None:
        with pytest.raises(Exception, match="时区"):
            build_replay_rows(since=T0.replace(tzinfo=None), until=None, window_seconds=60)


class TestRowMapping:
    def test_窗口内有产出_就是命中并带上提前量(self) -> None:
        case = case_from_row(db_row(), line_no=1)
        assert (case.predicted_warning, case.cell) == (True, "tp")
        assert case.lead_seconds == pytest.approx(1200.0)

    def test_窗口内没有产出是漏报_不是未知(self) -> None:
        # 写成 None 会让分母悄悄变小，准确率反而更高——这是最难发现的一种"变好"。
        case = case_from_row(db_row(predicted_generated_at=None, predicted_hazard_type=None, predicted_level=None), line_no=1)
        assert (case.predicted_warning, case.cell) == (False, "fn")
        assert case.lead_seconds is None

    def test_库行与_JSONL_行走的是同一个口径(self) -> None:
        from_row = case_from_row(db_row(), line_no=7)
        from_raw = parse_case(
            {
                "case_id": "ACC-1",
                "hazard_type": "debris_flow",
                "region_code": "540102",
                "truth_warning": True,
                "predicted_warning": True,
                "predicted_hazard_type": "debris_flow",
                "truth_level": 3,
                "predicted_level": 3,
                "lead_seconds": (T1 - T0).total_seconds(),
            },
            line_no=7,
        )
        assert from_row == from_raw

    def test_误报案例仍然算得出来(self) -> None:
        case = case_from_row(db_row(truth_warning=False), line_no=1)
        assert case.cell == "fp"


class TestPutAndReplay:
    async def test_空导入直接返回零且不碰数据库(self) -> None:
        conn = FakeConn()
        assert await put_labels(conn, []) == 0
        assert conn.executed == []

    async def test_导入条数等于行数并原样传给驱动(self) -> None:
        conn = FakeConn()
        rows = [label_row(), label_row(case_id="ACC-2")]
        assert await put_labels(conn, rows) == 2
        sql, values = conn.executed[0]
        assert len(values) == 2 and len(values[0]) == len(LABEL_COLUMNS)
        assert "warning_truth_labels" in sql

    async def test_超过单次上限就拒_不把一次请求变成无界内存(self) -> None:
        conn = FakeConn()
        rows = [label_row(case_id=f"ACC-{index}") for index in range(MAX_LABEL_ROWS + 1)]
        with pytest.raises(AccuracyArgumentError, match="上限"):
            await put_labels(conn, rows)

    async def test_回放行被映射成案例列表(self) -> None:
        conn = FakeConn([db_row(), db_row(case_id="ACC-2", truth_warning=False, predicted_generated_at=None)])
        cases = await replay_rows(conn, since=T0, until=T1, window_seconds=1800)
        assert [case.cell for case in cases] == ["tp", "tn"]
        assert len(conn.fetches) == 1

    async def test_配对结果交给_measure_才算数_本层不自算指标(self) -> None:
        conn = FakeConn(
            [
                db_row(),
                db_row(case_id="ACC-2", truth_warning=False, predicted_generated_at=None, predicted_hazard_type=None, predicted_level=None),
            ]
        )
        cases = await replay_rows(conn, since=T0, until=T1, window_seconds=1800)
        report = measure(ReplayDataset(cases=tuple(cases), kind="field", source="unit", note=""))
        assert (report.confusion.tp, report.confusion.tn) == (1, 1)
        # 样本不足时官方准确率一律为 None：不到 MIN_FIELD_CASES 就不能报"达标"
        assert report.official_accuracy is None
        assert report.dataset_kind == "field"


class TestLabelFile:
    def test_头行声明kind_source_note(self, tmp_path: Path) -> None:
        path = tmp_path / "labels.jsonl"
        lines = [
            {"dataset": {"kind": "field", "source": "2026-09 现场复盘", "note": "含 3 站"}},
            label_row(observed_at=T0.isoformat().replace("+00:00", "Z")),
            {"case_id": "ACC-2", "region_code": "540121", "hazard_type": "snow", "observed_at": T1.isoformat(), "truth_warning": False},
        ]
        path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines), encoding="utf-8")
        rows, kind, source, note = read_label_file(path)
        assert (kind, source, note) == ("field", "2026-09 现场复盘", "含 3 站")
        assert len(rows) == 2
        assert isinstance(rows[0]["observed_at"], datetime)
        assert rows[0]["observed_at"].tzinfo is not None

    def test_不认识的kind直接拒_不静默按unspecified处理(self, tmp_path: Path) -> None:
        path = tmp_path / "labels.jsonl"
        path.write_text(json.dumps({"dataset": {"kind": "official"}}), encoding="utf-8")
        with pytest.raises(AccuracyArgumentError, match="kind"):
            read_label_file(path)

    def test_坏JSON行报出行号(self, tmp_path: Path) -> None:
        path = tmp_path / "labels.jsonl"
        path.write_text('{"case_id":"A"}\nnot json\n', encoding="utf-8")
        with pytest.raises(AccuracyArgumentError, match="第 2 行"):
            read_label_file(path)

    def test_文件不存在就拒(self, tmp_path: Path) -> None:
        with pytest.raises(AccuracyArgumentError, match="不存在"):
            read_label_file(tmp_path / "nope.jsonl")


class TestDdlContract:
    """DDL 与代码列名必须一致：漂移的后果是导入静默失败或回放永远配不上。"""

    DDL = Path(__file__).resolve().parents[2] / "src" / "aegis/persistence" / "sql" / "004_accuracy_labels.sql"

    def test_LABEL_COLUMNS_都在建表语句里(self) -> None:
        text = self.DDL.read_text(encoding="utf-8")
        for column in LABEL_COLUMNS:
            assert f"{column} " in text, column

    def test_区划代码口径与站点表同形(self) -> None:
        # 与 `monitoring_stations.region_code` 的 CHECK 同正则，否则标注永远 JOIN 不上站点，
        # 回放会整批算成"全部漏报"。
        assert "^[0-9A-Z]{6,24}$" in self.DDL.read_text(encoding="utf-8")

    def test_建表是幂等的(self) -> None:
        text = self.DDL.read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS warning_truth_labels" in text
        assert "CREATE INDEX IF NOT EXISTS" in text

    def test_默认回放窗不超过单次上限(self) -> None:
        assert 0 < accuracy.DEFAULT_WINDOW_SECONDS <= MAX_WINDOW_SECONDS


class TestCaseFromRowGuards:
    def test_缺主键字段时不静默造案例(self) -> None:
        broken = db_row()
        del broken["case_id"]
        with pytest.raises(KeyError):
            case_from_row(broken, line_no=1)

    def test_返回类型就是ReplayCase_判定逻辑无处可逃(self) -> None:
        assert isinstance(case_from_row(db_row(), line_no=1), ReplayCase)


class TestScriptTimeArguments:
    """入口脚本的时间参数：裸时间一律拒，因为回放窗平移几小时没人看得出来。"""

    @pytest.mark.parametrize(
        "raw",
        ["2026-09-20T03:00:00Z", "2026-09-20T03:00:00+00:00", "2026-09-20T11:00:00+08:00"],
    )
    def test_带时区的写法都能读(self, raw: str) -> None:
        assert parse_moment(raw) == T0

    def test_裸时间被拒(self) -> None:
        with pytest.raises(AccuracyArgumentError, match="时区"):
            parse_moment("2026-09-20T03:00:00")

    def test_垃圾输入把原值带出来(self) -> None:
        with pytest.raises(AccuracyArgumentError, match="上周三"):
            parse_moment("上周三")
