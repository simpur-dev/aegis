"""站点台账导入：命令入口、内存维表与 HTTP 清单三层口径一起验。

要钉住的事（每一条都是"没有生产入口"期间真实存在过的风险）：
- 坐标不成对、区划码写错、列名拼错、站号重复、文件为空 → 整份拒绝，不做部分导入；
- 没有坐标的站照样能登记（清单里坐标为 None，由前端归入"未定位"），
  但绝不会替它编一个经纬度——错的坐标会被当成实测证据；
- 内存视图默认拒绝导入（活到进程结束为止），显式 `--allow-memory` 时结果里必须写明 `durable=false`；
- 导入之后 `GET /api/v1/stations` 真能看到名称与坐标，这才是"入口"存在的意义。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from scripts.import_stations import import_records, load_records, main, parse_record

from aegis.config import Settings
from aegis.container import create_container
from aegis.domain.messages import TelemetryReading
from aegis.storage.store import PlatformStore, StoreProtocol

GOOD_CSV = """station_id,region_code,lon,lat,name_zh,hazard_focus,elevation_m
LZ-540121-01,540121,91.28,29.896,林周沟口站,泥石流|滑坡,3650
NM-540221-02,540221,,,南木林观察哨,崩塌,
"""


def write_csv(tmp_path: Path, text: str, *, name: str = "stations.csv") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def reports(payload: str) -> dict[str, Any]:
    return json.loads(payload[payload.index("{") :])


class TestParsingAndValidation:
    def test_good_file_parses_every_row(self, tmp_path: Path) -> None:
        records, problems = load_records(write_csv(tmp_path, GOOD_CSV))
        assert problems == []
        assert [record.station_id for record in records] == ["LZ-540121-01", "NM-540221-02"]
        assert records[0].positioned and not records[1].positioned
        assert records[0].hazard_focus == ("泥石流", "滑坡")
        assert records[0].elevation_m == 3650.0

    def test_jsonl_is_accepted_too(self, tmp_path: Path) -> None:
        path = write_csv(
            tmp_path,
            '{"station_id":"A-1","region_code":"540100","lon":91.1,"lat":29.6,"name_zh":"甲站"}\n\n'
            '{"station_id":"A-2","region_code":"540100"}\n',
            name="stations.jsonl",
        )
        records, problems = load_records(path)
        assert problems == [] and len(records) == 2

    @pytest.mark.parametrize(
        ("row", "expect_in_reason"),
        [
            ("X-1,54012,91.28,29.896,五位数区划,,", "region_code"),
            ("X-1,540121012345678901234567890,91.28,29.896,超长区划,,", "region_code"),
            ("X-1,540121,91.28,,只有经度,,", "成对"),
            ("X-1,540121,191.28,29.896,经度越界,,", "lon"),
            ("X-1,540121,91.28,129.896,纬度越界,,", "lat"),
            (",540121,,,站号为空,,", "station_id"),
            ("X-1,540121,91.28,29.896,名字超长," + "泥" * 70 + ",3650", "hazard_focus"),
            ("X-1,540121,91.28,29.896,高程非数,滑坡,abc", "elevation_m"),
            ("X-1,540121,91.28,29.896,高程无穷,滑坡,inf", "有限"),
        ],
    )
    def test_each_bad_row_is_rejected_with_a_locatable_reason(self, tmp_path: Path, row: str, expect_in_reason: str) -> None:
        header = "station_id,region_code,lon,lat,name_zh,hazard_focus,elevation_m\n"
        records, problems = load_records(write_csv(tmp_path, header + row + "\n"))
        assert records == []
        assert expect_in_reason in " ".join(problems), problems

    def test_typo_column_is_refused_instead_of_silently_dropping_coordinates(self, tmp_path: Path) -> None:
        text = "station_id,region_code,logitude,latitude,name_zh\nX-1,540121,91.28,29.896,拼错列名的站\n"
        records, problems = load_records(write_csv(tmp_path, text))
        assert records == []
        assert "未知列" in problems[0] and "logitude" in problems[0]

    def test_duplicate_station_id_is_rejected(self, tmp_path: Path) -> None:
        text = GOOD_CSV + "LZ-540121-01,540121,91.3,29.9,重复站,,\n"
        records, problems = load_records(write_csv(tmp_path, text))
        assert len(records) == 2, "首现的那条要留下，重复的那条被剔掉"
        assert len(problems) == 1 and "重复" in problems[0]
        assert "第 4 行" in problems[0], "报的行号要能直接指到文件里那一行"

    def test_empty_file_is_a_problem_not_a_success(self, tmp_path: Path) -> None:
        records, problems = load_records(write_csv(tmp_path, "station_id,region_code\n"))
        assert records == [] and "空" in problems[0]

    def test_region_code_is_upper_normalized(self) -> None:
        record = parse_record({"station_id": "X-1", "region_code": "540121x"}, line=2)
        assert record.region_code == "540121X"


class TestCommandContract:
    async def test_any_bad_row_blocks_the_whole_import(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        text = GOOD_CSV + "BAD-1,54,91.3,29.9,区划写错,,\n"
        assert await main(["--source", str(write_csv(tmp_path, text))]) == 1
        captured = capsys.readouterr()
        assert reports(captured.out)["status"] == "rejected"
        assert "整份未导入" in captured.err

    async def test_dry_run_validates_without_touching_a_store(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert await main(["--source", str(write_csv(tmp_path, GOOD_CSV)), "--dry-run"]) == 0
        summary = reports(capsys.readouterr().out)
        assert summary == {"status": "validated", "rows": 2, "positioned": 1, "unpositioned": 1}

    async def test_memory_backend_is_refused_unless_told_otherwise(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert await main(["--source", str(write_csv(tmp_path, GOOD_CSV))]) == 2
        assert "内存维表" in reports(capsys.readouterr().out)["reason"]

    async def test_allow_memory_imports_and_says_loudly_it_is_not_durable(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert await main(["--source", str(write_csv(tmp_path, GOOD_CSV)), "--allow-memory"]) == 0
        captured = capsys.readouterr()
        summary = reports(captured.out)
        assert summary["rows"] == 2 and summary["durable"] is False
        assert "只活在本进程" in captured.err


class TestMemoryLedger:
    async def test_upsert_then_list_carries_names_and_geometry(self) -> None:
        store = PlatformStore()
        await store.upsert_station(
            "LZ-540121-01", "540121", 91.28, 29.896, name_zh="林周沟口站", hazard_focus=["泥石流"], elevation_m=3650.0
        )
        await store.upsert_station("NM-540221-02", "540221", None, None, name_zh="南木林观察哨")
        rows = {str(row["station_id"]): row for row in await store.list_stations()}
        assert rows["LZ-540121-01"]["name_zh"] == "林周沟口站"
        assert rows["LZ-540121-01"]["geom"] == "SRID=4326;POINT(91.28 29.896)"
        assert rows["LZ-540121-01"]["elevation_m"] == 3650.0
        # 没坐标的站必须在清单里，且坐标是 None——前端靠这个把它列进"未定位"
        assert rows["NM-540221-02"]["lon"] is None and rows["NM-540221-02"]["geom"] is None

    async def test_observed_but_unregistered_stations_still_show_up(self) -> None:
        store = PlatformStore()
        await store.upsert_station("LZ-540121-01", "540121", 91.28, 29.896, name_zh="林周沟口站")
        readings = _readings("NEW-540221-77", "540221")
        await store.telemetry.add(readings)
        rows = {str(row["station_id"]): row for row in await store.list_stations()}
        assert set(rows) == {"LZ-540121-01", "NEW-540221-77"}
        assert rows["NEW-540221-77"]["name_zh"] == "" and rows["NEW-540221-77"]["lon"] is None

    async def test_upsert_rejects_half_coordinates_and_blank_ids(self) -> None:
        store = PlatformStore()
        with pytest.raises(ValueError, match="成对"):
            await store.upsert_station("X-1", "540121", 91.28, None)
        with pytest.raises(ValueError, match="station_id"):
            await store.upsert_station("  ", "540121", None, None)
        with pytest.raises(ValueError, match="region_code"):
            await store.upsert_station("X-1", "  ", None, None)

    async def test_upsert_is_idempotent_and_updates_in_place(self) -> None:
        store = PlatformStore()
        await store.upsert_station("LZ-540121-01", "540121", 91.28, 29.896, name_zh="旧名")
        await store.upsert_station("LZ-540121-01", "540121", 91.28, 29.896, name_zh="新名")
        rows = await store.list_stations()
        assert len(rows) == 1
        assert rows[0]["name_zh"] == "新名"

    async def test_region_filter_and_limit_are_honoured(self) -> None:
        store = PlatformStore()
        await store.upsert_station("A-1", "540121", 91.0, 29.0, name_zh="甲")
        await store.upsert_station("B-1", "540221", 90.0, 30.0, name_zh="乙")
        only = await store.list_stations(region_code="540221")
        assert [row["station_id"] for row in only] == ["B-1"]
        assert len(await store.list_stations(limit=1)) == 1
        with pytest.raises(ValueError, match="limit"):
            await store.list_stations(limit=0)

    async def test_imported_file_lands_in_the_same_ledger(self, tmp_path: Path) -> None:
        store = PlatformStore()
        records, problems = load_records(write_csv(tmp_path, GOOD_CSV))
        assert problems == []
        assert await import_records(store, records) == 2
        rows = {str(row["station_id"]): row for row in await store.list_stations()}
        assert rows["LZ-540121-01"]["hazard_focus"] == ["泥石流", "滑坡"]


class TestProtocolParity:
    def test_port_declares_upsert_station(self) -> None:
        assert "upsert_station" in {name for name, _ in inspect.getmembers(StoreProtocol, predicate=callable)}

    def test_memory_and_postgres_signatures_match(self) -> None:
        try:
            from aegis.persistence.postgres import PostgresStore
        except Exception as exc:  # asyncpg 属于可选 extra，缺席是正常部署形态
            pytest.skip(f"未安装 postgres extra：{type(exc).__name__}")
        memory = inspect.signature(PlatformStore.upsert_station)
        durable = inspect.signature(PostgresStore.upsert_station)
        assert list(memory.parameters) == list(durable.parameters), "两端签名一分叉，导入命令就会只在一端能用"

    async def test_http_stations_route_shows_the_imported_ledger(self) -> None:
        container = create_container(Settings())
        await container.start()
        try:
            await container.store.upsert_station("LZ-540121-01", "540121", 91.28, 29.896, name_zh="林周沟口站", elevation_m=3650.0)
            transport = httpx.ASGITransport(app=_app(container))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/api/v1/stations", params={"region_code": "540121"})
            assert response.status_code == 200
            item = response.json()["items"][0]
            assert item["name_zh"] == "林周沟口站"
            assert item["lon"] == 91.28 and item["lat"] == 29.896
            assert item["elevation_m"] == 3650.0
        finally:
            await container.shutdown()


def _app(container: Any) -> Any:
    from aegis.api.app import create_app

    return create_app(container.settings, container=container)


def _readings(station_id: str, region_code: str) -> list[TelemetryReading]:
    return [TelemetryReading(station_id=station_id, region_code=region_code, metric="rainfall_mm", value=12.0, unit="mm")]
