"""区域历史画像生成的离线用例：合并语义（不覆盖普查属性）+ 聚合口径。

**钉住的是一条真故障**（2026-11-21 真机联调）：历史画像若独占 region_code 导入，
`import_site_profiles` 的 upsert 会把既有普查属性（坡度/汇水面积/沟道比降）整条替换掉，
因子引擎随即判 P 组覆盖率 0% → 研判数据不足 → 预警素材腿拿不到 `assessment_status=scored`
信封（空素材）。所以生成器必须**按 region_code 合并**：历史统计是追加维度。
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.build_region_profiles import build_profiles, load_census_stats, load_existing


def _census(path: Path, rows: list[tuple[str, str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for region, day, hazard in rows:
            handle.write(
                json.dumps(
                    {
                        "record_id": f"census:{region}:{day}:{hazard}",
                        "event": {"date": day},
                        "geo": {"admin_division": region},
                        "hazard": {"type": hazard, "attributes": {"occurred": True}},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return path


def _existing(path: Path, rows: list[dict]) -> Path:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


class TestCensusStats:
    def test_聚合按灾种月份年份且窗口外不计(self, tmp_path: Path) -> None:
        census = _census(
            tmp_path / "c.jsonl",
            [
                ("540121", "2021-08-01", "debris_flow"),
                ("540121", "2021-08-15", "debris_flow"),
                ("540121", "2020-03-02", "rockfall"),
                ("540121", "2010-01-01", "debris_flow"),  # 窗口外（<2013）
            ],
        )
        stats = load_census_stats(census)["540121"]
        assert stats["total"] == 3, "窗口外记录不计"
        assert stats["hazards"] == {"debris_flow": 2, "rockfall": 1}
        assert stats["dominant"] == "debris_flow"
        assert stats["peak_month"] == 8
        assert stats["peak_year"] == "2021"


class TestOccurredOnly:
    def test_隐患点不算事件且单独计数(self, tmp_path: Path) -> None:
        """普查里"是否发生=否"的是隐患点，不是事件——混进 history_*_events 会让
        助手与研判把"登记过的风险点"读成"发生过的事件"。"""
        census = _census(
            tmp_path / "c.jsonl",
            [("540121", "2021-08-01", "debris_flow"), ("540121", "2021-08-02", "debris_flow")],
        )
        # _census 里全部是 occurred=True；再造一条隐患点
        with census.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    {
                        "record_id": "census:potential",
                        "event": {"date": "2021-08-03"},
                        "geo": {"admin_division": "540121"},
                        "hazard": {"type": "rockfall", "attributes": {"occurred": False}},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        stats = load_census_stats(census)["540121"]
        assert stats["hazards"] == {"debris_flow": 2}, "隐患点不得计入灾种事件数"
        assert stats["total"] == 2
        assert stats["potential"] == 1

    def test_隐患点属性单独成维(self, tmp_path: Path) -> None:
        census = _census(tmp_path / "c.jsonl", [("540121", "2021-08-01", "debris_flow")])
        with census.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    {
                        "record_id": "census:potential",
                        "event": {"date": "2021-08-03"},
                        "geo": {"admin_division": "540121"},
                        "hazard": {"type": "landslide", "attributes": {"occurred": False}},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        profiles = build_profiles(census, tmp_path / "no_glof.jsonl", tmp_path / "no_glc.jsonl", "2026-11-21T00:00:00Z")
        keys = {attr["key"]: attr["value"] for attr in profiles[0]["attributes"]}
        assert keys["history_potential_sites_10y"] == 1.0
        assert keys["history_total_events_10y"] == 1.0, "事件总数只算已发生"


class TestMerge:
    def test_既有普查属性不被历史统计覆盖(self, tmp_path: Path) -> None:
        census = _census(tmp_path / "c.jsonl", [("540121", "2021-08-01", "debris_flow")])
        existing = _existing(
            tmp_path / "profiles.jsonl",
            [
                {
                    "region_code": "540121",
                    "name": "林芝市巴宜区（演示）",
                    "hazard_types": ["landslide", "debris_flow"],
                    "attributes": [
                        {"key": "catchment_area_km2", "value": 12.6, "unit": "km²", "source": "demo_survey"},
                        {"key": "channel_gradient", "value": 0.32, "unit": "", "source": "demo_survey"},
                    ],
                    "provenance": {"data_kind": "demo", "sources": ["demo_survey"]},
                }
            ],
        )
        profiles = build_profiles(
            census, tmp_path / "no_glof.jsonl", tmp_path / "no_glc.jsonl", "2026-11-21T00:00:00Z", existing_path=existing
        )
        profile = next(p for p in profiles if p["region_code"] == "540121")
        keys = {attr["key"]: attr["value"] for attr in profile["attributes"]}
        # 普查属性在（这是因子引擎 P 组必需因子）
        assert keys["catchment_area_km2"] == 12.6
        assert keys["channel_gradient"] == 0.32
        # 历史统计也进来了
        assert keys["history_debris_flow_events_10y"] == 1.0
        assert keys["history_dominant_hazard"] == "泥石流"
        # 名称与灾种清单保留/并集
        assert profile["name"] == "林芝市巴宜区（演示）"
        assert set(profile["hazard_types"]) == {"landslide", "debris_flow"}

    def test_没有既有画像的区域产出纯历史画像(self, tmp_path: Path) -> None:
        census = _census(tmp_path / "c.jsonl", [("540324", "2021-07-01", "landslide")])
        profiles = build_profiles(census, tmp_path / "no_glof.jsonl", tmp_path / "no_glc.jsonl", "2026-11-21T00:00:00Z")
        profile = next(p for p in profiles if p["region_code"] == "540324")
        keys = {attr["key"] for attr in profile["attributes"]}
        assert "history_landslide_events_10y" in keys
        assert "history_total_events_10y" in keys

    def test_重算的历史值覆盖合并源里的旧历史值(self, tmp_path: Path) -> None:
        """拿上一次输出当 merge 源时，history_* 必须用新算的值（旧值只是陈旧快照）。"""
        census = _census(tmp_path / "c.jsonl", [("540121", "2021-08-01", "debris_flow")])
        existing = _existing(
            tmp_path / "profiles.jsonl",
            [
                {
                    "region_code": "540121",
                    "attributes": [
                        {"key": "catchment_area_km2", "value": 12.6, "source": "demo_survey"},
                        {"key": "history_debris_flow_events_10y", "value": 999.0, "source": "stale"},
                    ],
                    "hazard_types": [],
                }
            ],
        )
        profiles = build_profiles(
            census, tmp_path / "no_glof.jsonl", tmp_path / "no_glc.jsonl", "2026-11-21T00:00:00Z", existing_path=existing
        )
        keys = {attr["key"]: attr["value"] for attr in next(p for p in profiles if p["region_code"] == "540121")["attributes"]}
        assert keys["history_debris_flow_events_10y"] == 1.0, "旧历史值不得覆盖本次重算"
        assert keys["catchment_area_km2"] == 12.6, "非 history 属性仍保留"

    def test_属性key唯一(self, tmp_path: Path) -> None:
        """SiteProfile 校验 key 唯一：合并时同名 key 只能留一份（否则导入被拒）。"""
        census = _census(tmp_path / "c.jsonl", [("540121", "2021-08-01", "debris_flow")])
        existing = _existing(
            tmp_path / "profiles.jsonl",
            [
                {
                    "region_code": "540121",
                    "attributes": [{"key": "history_debris_flow_events_10y", "value": 999.0, "source": "stale"}],
                    "hazard_types": [],
                }
            ],
        )
        profiles = build_profiles(
            census, tmp_path / "no_glof.jsonl", tmp_path / "no_glc.jsonl", "2026-11-21T00:00:00Z", existing_path=existing
        )
        keys = [attr["key"] for attr in next(p for p in profiles if p["region_code"] == "540121")["attributes"]]
        assert len(keys) == len(set(keys)), "合并后 key 重复会导致 SiteProfile 校验失败、整份导入被拒"

    def test_读取既有画像文件缺列时返回空(self, tmp_path: Path) -> None:
        assert load_existing(None) == {}
        missing = tmp_path / "nope.jsonl"
        assert load_existing(missing) == {}
