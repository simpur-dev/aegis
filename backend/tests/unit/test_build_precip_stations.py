"""县域降水代理点生成器的离线用例：县名解析、"西藏"实体筛选、坐标取数、落盘形状。

Wikidata 联网部分只有 HTTP 一层——这里用假 `_get_json` 替身钉住协议编排；
真坐标来自真 API（首跑实测已在运行态目录）。
"""

from __future__ import annotations

import csv
import json
import urllib.parse
from pathlib import Path

import pytest
from scripts.build_precip_stations import (
    build_stations,
    entity_coordinates,
    load_regions,
    main,
    search_entity,
    write_outputs,
)


def _profiles(path: Path, rows: list[tuple[str, str, list[str]]]) -> Path:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for region_code, name, hazards in rows:
            handle.write(json.dumps({"region_code": region_code, "name": name, "hazard_types": hazards}, ensure_ascii=False) + "\n")
    return path


def _fake_json_factory(search: dict[str, list[dict]], claims: dict[str, dict]) -> object:
    def fake_get_json(url: str, timeout_seconds: float = 30.0):
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        if params.get("action") == "wbsearchentities":
            return {"search": search.get(params.get("search", ""), [])}
        return {"entities": {params["ids"]: claims[params["ids"]]}}

    return fake_get_json


SEARCH = {
    "巴宜区": [{"id": "Q519708", "description": "中国西藏自治区林芝市的市辖区"}, {"id": "QOTHER", "description": "山东的区"}],
}
CLAIMS = {
    "Q519708": {
        "claims": {
            "P625": [
                {"rank": "normal", "mainsnak": {"datavalue": {"value": {"latitude": 29.63799, "longitude": 94.36766}}}},
                {"rank": "preferred", "mainsnak": {"datavalue": {"value": {"latitude": 29.6365717, "longitude": 94.3610895}}}},
            ],
            "P2044": [{"mainsnak": {"datavalue": {"value": {"amount": "+2900"}}}}],
        }
    }
}


class TestRegionParsing:
    def test_从演示档案剥出县名与灾种(self, tmp_path: Path) -> None:
        profiles = _profiles(tmp_path / "profiles.jsonl", [("540121", "林芝市巴宜区（演示）", ["landslide", "debris_flow"])])
        rows = load_regions(profiles)
        assert rows == [("540121", "巴宜区", "landslide|debris_flow")]

    def test_剥不出县名整份失败(self, tmp_path: Path) -> None:
        profiles = _profiles(tmp_path / "profiles.jsonl", [("540121", "演示区", [])])
        with pytest.raises(SystemExit, match="剥不出县名"):
            load_regions(profiles)

    def test_重复区划码失败(self, tmp_path: Path) -> None:
        profiles = _profiles(tmp_path / "profiles.jsonl", [("540121", "林芝市巴宜区", []), ("540121", "重复", [])])
        with pytest.raises(SystemExit, match="重复"):
            load_regions(profiles)


class TestWikidataProtocol:
    def test_只挑描述含西藏的实体(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("scripts.build_precip_stations._get_json", _fake_json_factory(SEARCH, CLAIMS))
        entity_id, description = search_entity("巴宜区")
        assert entity_id == "Q519708"
        assert "西藏" in description

    def test_没有西藏实体就报错不猜(self, monkeypatch: pytest.MonkeyPatch) -> None:
        search = {"巴宜区": [{"id": "QX", "description": "山东的区"}]}
        monkeypatch.setattr("scripts.build_precip_stations._get_json", _fake_json_factory(search, CLAIMS))
        with pytest.raises(SystemExit, match="人工核对"):
            search_entity("巴宜区")

    def test_坐标取preferred_rank与海拔(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("scripts.build_precip_stations._get_json", _fake_json_factory({}, CLAIMS))
        latitude, longitude, elevation = entity_coordinates("Q519708")
        assert (latitude, longitude) == pytest.approx((29.6365717, 94.3610895))
        assert elevation == 2900.0

    def test_无坐标声明报错(self, monkeypatch: pytest.MonkeyPatch) -> None:
        claims = {"Q519708": {"claims": {}}}
        monkeypatch.setattr("scripts.build_precip_stations._get_json", _fake_json_factory({}, claims))
        with pytest.raises(SystemExit, match="P625"):
            entity_coordinates("Q519708")


class TestBuildAndWrite:
    def test_编排产出站点表与出处(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        profiles = _profiles(tmp_path / "profiles.jsonl", [("540121", "林芝市巴宜区（演示）", ["landslide"])])
        monkeypatch.setattr("scripts.build_precip_stations._get_json", _fake_json_factory(SEARCH, CLAIMS))
        monkeypatch.setattr("scripts.build_precip_stations.time.sleep", lambda seconds: None)

        stations = build_stations(profiles)
        assert [s.station_id for s in stations] == ["PR-540121-01"]
        assert stations[0].name_zh == "巴宜区县域降水代理点"
        assert stations[0].source_url == "https://www.wikidata.org/wiki/Q519708"

        csv_path, provenance_path = write_outputs(tmp_path, stations)
        rows = list(csv.DictReader(csv_path.open(encoding="utf-8", newline="")))
        assert rows[0]["latitude"].startswith("29.6365")
        assert rows[0]["elevation_m"] == "2900"
        provenance = json.loads(provenance_path.read_text(encoding="utf-8").splitlines()[0])
        assert provenance["license"] == "CC0-1.0"
        assert provenance["wikidata_id"] == "Q519708"
        assert "代理点" in provenance["note"] and "非实测站" in provenance["note"]

    def test_entity_override绕过自动搜索(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        profiles = _profiles(tmp_path / "profiles.jsonl", [("540121", "林芝市巴宜区", ["landslide"])])
        monkeypatch.setattr("scripts.build_precip_stations._get_json", _fake_json_factory({}, CLAIMS))
        monkeypatch.setattr("scripts.build_precip_stations.time.sleep", lambda seconds: None)
        stations = build_stations(profiles, entity_override={"540121": "Q519708"})
        assert stations[0].wikidata_id == "Q519708"


def test_dry_run只解析不落盘(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    profiles = _profiles(tmp_path / "profiles.jsonl", [("540121", "林芝市巴宜区（演示）", [])])
    code = main(["--profiles", str(profiles), "--dest", str(tmp_path / "out"), "--dry-run"])
    assert code == 0
    assert "巴宜区" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()
