"""站点画像导入：整份校验、行号可定位、内存视图的诚实边界。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from scripts.import_site_profiles import import_records, load_records, main, parse_record

from aegis.domain.site import SiteProfile
from aegis.storage.store import PlatformStore

GOOD_JSONL = (
    json.dumps(
        {
            "region_code": "540121",
            "name": "演示区",
            "hazard_types": ["landslide"],
            "attributes": [{"key": "slope_angle_deg", "value": 38.0, "unit": "°", "source": "demo_survey"}],
            "provenance": {"data_kind": "demo", "is_mock": True, "sources": ["site_profile:demo"]},
        },
        ensure_ascii=False,
    )
    + "\n"
)


def write_file(tmp_path: Path, text: str, *, name: str = "profiles.jsonl") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def reports(payload: str) -> dict[str, Any]:
    return json.loads(payload[payload.index("{") :])


class TestParsingAndValidation:
    def test_jsonl_roundtrip(self, tmp_path: Path) -> None:
        records, problems = load_records(write_file(tmp_path, GOOD_JSONL))
        assert problems == []
        assert [profile.region_code for profile in records] == ["540121"]
        assert records[0].provenance.data_kind == "demo" and records[0].provenance.is_mock

    def test_csv_carries_nested_columns_as_json_text(self, tmp_path: Path) -> None:
        import csv
        import io

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["region_code", "name", "hazard_types", "attributes", "provenance"])
        writer.writerow(
            [
                "540121",
                "演示区",
                json.dumps(["landslide"]),
                json.dumps([{"key": "slope_angle_deg", "value": 38.0}]),
                json.dumps({"data_kind": "demo"}),
            ]
        )
        records, problems = load_records(write_file(tmp_path, buffer.getvalue(), name="profiles.csv"))
        assert problems == [], problems
        assert records[0].value_of("slope_angle_deg") == 38.0

    @pytest.mark.parametrize(
        ("row", "expect_in_reason"),
        [
            ({"region_code": "54012", "attributes": []}, "region_code"),
            ({"region_code": "540121", "attributes": [{"key": "Slope", "value": 1}]}, "key"),
            ({"region_code": "540121", "attributes": [{"key": "slope_angle_deg", "value": float("nan")}]}, "value"),
            (
                {
                    "region_code": "540121",
                    "attributes": [
                        {"key": "slope_angle_deg", "value": 1},
                        {"key": "slope_angle_deg", "value": 2},
                    ],
                },
                "重复",
            ),
        ],
    )
    def test_each_bad_row_is_rejected_with_a_reason(self, tmp_path: Path, row: dict[str, Any], expect_in_reason: str) -> None:
        records, problems = load_records(write_file(tmp_path, json.dumps(row) + "\n"))
        assert records == []
        assert expect_in_reason in " ".join(problems), problems

    def test_unknown_column_is_refused(self, tmp_path: Path) -> None:
        row = {"region_code": "540121", "slope": 38.0}
        records, problems = load_records(write_file(tmp_path, json.dumps(row) + "\n"))
        assert records == []
        assert "未知列" in problems[0] and "slope" in problems[0]

    def test_duplicate_region_is_rejected(self, tmp_path: Path) -> None:
        records, problems = load_records(write_file(tmp_path, GOOD_JSONL + GOOD_JSONL))
        assert len(records) == 1, "首现的那条留下，重复的那条被剔掉"
        assert len(problems) == 1 and "重复" in problems[0] and "第 2 行" in problems[0]

    def test_empty_file_is_a_problem_not_a_success(self, tmp_path: Path) -> None:
        records, problems = load_records(write_file(tmp_path, ""))
        assert records == [] and "空" in problems[0]

    def test_json_array_is_refused_for_lack_of_line_numbers(self, tmp_path: Path) -> None:
        records, problems = load_records(write_file(tmp_path, "[]", name="profiles.json"))
        assert records == [] and "JSON 数组" in problems[0]

    def test_parse_record_accepts_native_nested_fields(self) -> None:
        profile = parse_record(
            {"region_code": "540121", "attributes": [{"key": "slope_angle_deg", "value": 38}]},
            line=2,
        )
        assert isinstance(profile, SiteProfile)


class TestCommandContract:
    async def test_any_bad_row_blocks_the_whole_import_before_any_store_is_built(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aegis.integrations as integrations

        def boom(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("校验未通过时不许构建存储")

        monkeypatch.setattr(integrations, "build_store", boom)
        text = GOOD_JSONL + json.dumps({"region_code": "540121", "slope": 1.0}) + "\n"
        assert await main(["--source", str(write_file(tmp_path, text))]) == 1
        captured = capsys.readouterr()
        assert reports(captured.out)["status"] == "rejected"
        assert "整份未导入" in captured.err

    async def test_dry_run_validates_without_building_a_store(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aegis.integrations as integrations

        monkeypatch.setattr(integrations, "build_store", lambda *a, **k: pytest.fail("dry-run 不许碰存储"))
        assert await main(["--source", str(write_file(tmp_path, GOOD_JSONL)), "--dry-run"]) == 0
        summary = reports(capsys.readouterr().out)
        assert summary["status"] == "validated" and summary["rows"] == 1 and summary["demo_rows"] == 1

    async def test_memory_backend_is_refused_unless_told_otherwise(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert await main(["--source", str(write_file(tmp_path, GOOD_JSONL))]) == 2
        assert "内存画像" in reports(capsys.readouterr().out)["reason"]

    async def test_allow_memory_imports_and_says_it_is_not_durable(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert await main(["--source", str(write_file(tmp_path, GOOD_JSONL)), "--allow-memory"]) == 0
        captured = capsys.readouterr()
        summary = reports(captured.out)
        assert summary["rows"] == 1 and summary["durable"] is False
        assert "只活在本进程" in captured.err

    async def test_import_records_writes_profiles_into_the_store(self, tmp_path: Path) -> None:
        records, problems = load_records(write_file(tmp_path, GOOD_JSONL))
        assert problems == []
        store = PlatformStore()
        assert await import_records(store, records) == 1
        assert (await store.get_site_profile("540121")) is not None  # type: ignore[union-attr]
