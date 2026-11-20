"""Open-Meteo 逐小时取数脚本的离线部分：站点表契约、URL 构造、幂等合并、主流程。

脚本本身不参与全量门禁联网——这里用假 `fetch_one` 替身把"取数→合并→落盘"的
编排逻辑钉住，联网部分只有 HTTP 一层（不测 socket，测协议形态：429 说人话）。
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import pytest
from scripts.fetch_precip_hourly_openmeteo import (
    Station,
    build_url,
    fetch_one,
    load_stations,
    main,
    merge_csv,
    read_existing,
    write_csv,
)


def _stations_csv(tmp_path: Path) -> Path:
    path = tmp_path / "stations.csv"
    path.write_text(
        "station_id,region_code,latitude,longitude\nRG-540121-01,540121,29.65,94.35\nRG-540122-01,540122,29.20,94.50\n",
        encoding="utf-8",
    )
    return path


class TestStationTable:
    def test_正常表逐行装进来(self, tmp_path: Path) -> None:
        stations = load_stations(_stations_csv(tmp_path))
        assert [s.station_id for s in stations] == ["RG-540121-01", "RG-540122-01"]
        assert stations[0].latitude == pytest.approx(29.65)

    def test_缺列当场停(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.csv"
        path.write_text("station_id,region_code\nRG-01,540121\n", encoding="utf-8")
        with pytest.raises(SystemExit, match="缺列"):
            load_stations(path)

    def test_坐标越界与重复id都报错(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.csv"
        path.write_text("station_id,region_code,latitude,longitude\nRG-01,540121,99.0,94.0\n", encoding="utf-8")
        with pytest.raises(SystemExit, match="越界"):
            load_stations(path)

        path2 = tmp_path / "dup.csv"
        path2.write_text(
            "station_id,region_code,latitude,longitude\nRG-01,540121,29.0,94.0\nRG-01,540122,29.1,94.1\n",
            encoding="utf-8",
        )
        with pytest.raises(SystemExit, match="重复"):
            load_stations(path2)


class TestUrlAndMerge:
    def test_url是匿名archive请求且四舍五入坐标(self) -> None:
        url = build_url(Station("RG-01", "540121", 29.654321, 94.345678), date(2026, 11, 18), date(2026, 11, 20))
        assert url.startswith("https://archive-api.open-meteo.com/v1/archive?")
        assert "latitude=29.6543" in url and "longitude=94.3457" in url
        assert "hourly=precipitation" in url and "timezone=UTC" in url

    def test_合并幂等_旧值保留新值不覆盖(self) -> None:
        existing = {("2026-11-20T13:00Z", "RG-01"): 1.5}
        fetched = {
            ("2026-11-20T13:00Z", "RG-01"): 9.9,  # 已有：保留
            ("2026-11-20T14:00Z", "RG-01"): 0.5,  # 新增
        }
        total, added = merge_csv(existing, fetched)
        assert (total, added) == (2, 1)
        assert existing[("2026-11-20T13:00Z", "RG-01")] == 1.5

    def test_csv往返_空值落空串(self, tmp_path: Path) -> None:
        path = tmp_path / "out.csv"
        rows = {("2026-11-20T13:00Z", "RG-01"): 2.0, ("2026-11-20T14:00Z", "RG-01"): None}
        write_csv(path, rows, {"RG-01": "540121"})
        text = path.read_text(encoding="utf-8")
        assert text.startswith("datetime_utc,station_id,region_code,precip_mm_h\n")
        assert "2026-11-20T14:00Z,RG-01,540121,\n" in text
        # 排序：整点升序
        assert text.index("13:00Z") < text.index("14:00Z")
        back = read_existing(path)
        assert back[("2026-11-20T13:00Z", "RG-01")] == 2.0
        assert back[("2026-11-20T14:00Z", "RG-01")] is None


class TestFetchOne:
    def test_正常响应归一成带Z整点(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {
            "hourly": {
                "time": ["2026-11-20T13:00", "2026-11-20T14:00"],
                "precipitation": [1.2, None],
            }
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(payload).encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: FakeResponse())
        series = fetch_one("https://archive-api.open-meteo.com/v1/archive?x=1")
        assert series == {"2026-11-20T13:00Z": 1.2, "2026-11-20T14:00Z": None}

    def test_限流说人话(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error

        def raise_429(request, timeout):
            raise urllib.error.HTTPError("url", 429, "Too Many Requests", None, None)  # type: ignore[arg-type]

        monkeypatch.setattr("urllib.request.urlopen", raise_429)
        with pytest.raises(SystemExit, match="限流"):
            fetch_one("https://archive-api.open-meteo.com/v1/archive?x=1")


class TestMainFlow:
    def test_dry_run只打印url不落盘(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        stations = _stations_csv(tmp_path)
        code = main(["--stations", str(stations), "--dest", str(tmp_path / "out"), "--dry-run"])
        assert code == 0
        out = capsys.readouterr().out
        assert out.count("archive-api.open-meteo.com") == 2, "每站一个请求"
        assert not (tmp_path / "out").exists()

    def test_主流程取数合并落盘_重跑不重复(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        stations = _stations_csv(tmp_path)
        series = {"2026-11-20T13:00Z": 1.2, "2026-11-20T14:00Z": 0.0}

        monkeypatch.setattr("scripts.fetch_precip_hourly_openmeteo.fetch_one", lambda url: dict(series))
        monkeypatch.setattr("scripts.fetch_precip_hourly_openmeteo.time.sleep", lambda seconds: None)
        dest = tmp_path / "out"
        code = main(["--stations", str(stations), "--dest", str(dest), "--sleep", "0"])
        assert code == 0
        path = dest / "openmeteo_hourly.csv"
        rows = list(csv.DictReader(path.open(encoding="utf-8", newline="")))
        assert len(rows) == 4, "两站 × 两小时"
        got = {(row["station_id"], row["precip_mm_h"]) for row in rows}
        assert got == {("RG-540121-01", "1.2"), ("RG-540122-01", "1.2"), ("RG-540121-01", "0.0"), ("RG-540122-01", "0.0")}

        # 重跑：同一小时旧值保留，不产生新行
        monkeypatch.setattr("scripts.fetch_precip_hourly_openmeteo.fetch_one", lambda url: {**series, "2026-11-20T15:00Z": 3.0})
        code = main(["--stations", str(stations), "--dest", str(dest), "--sleep", "0"])
        assert code == 0
        rows2 = list(csv.DictReader(path.open(encoding="utf-8", newline="")))
        assert len(rows2) == 6, "重跑只补缺（+2 新整点），旧值不覆盖"
        first = next(row for row in rows2 if row["datetime_utc"] == "2026-11-20T13:00Z" and row["station_id"] == "RG-540121-01")
        assert first["precip_mm_h"] == "1.2"
