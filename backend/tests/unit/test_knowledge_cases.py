"""预案案例数据模型：字段移植完整性、校验边界、剧集文本可复现性。"""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path

import pytest
from pydantic import ValidationError

from aegis.errors import SchemaInvalidError
from aegis.knowledge.cases import (
    DEFAULT_CASES_PATH,
    DEFAULT_STATE_EFFECT_KEYS,
    HazardCase,
    cases_by_id,
    load_builtin_cases,
    load_cases,
)

PORTED_FIELDS = (
    "why_now",
    "target_groups",
    "expected_effects",
    "possible_side_effects",
    "required_prerequisites",
    "monitoring_metrics",
    "estimated_delay_hours",
    "confidence",
    "state_effects",
)


def sample_case(**overrides: object) -> HazardCase:
    base: dict[str, object] = {
        "case_id": "case_tibet_demo_01",
        "title": "示例：沟口即时转移",
        "hazard_types": ["debris_flow"],
        "category": "evacuate",
        "phase": "response",
        "why_now": "物源饱和后 30 分钟内即可抵达沟口",
        "target_groups": ["沟口牧民住户"],
        "actions": ["向沟岸两侧台地转移"],
        "expected_effects": ["伤亡风险下降"],
        "possible_side_effects": ["夜间转移易滑坠"],
        "required_prerequisites": ["转移路线已勘定"],
        "monitoring_metrics": ["30 分钟内转移完成率"],
        "trigger_signals": ["泥位计读数超过 1.0m"],
        "estimated_delay_hours": 0.3,
        "confidence": 0.8,
        "state_effects": {"risk_exposure": -0.3, "casualty_risk": -0.2},
        "region_prefixes": ["5401"],
        "applies_to_levels": [1, 2],
        "source_note": "测试用例",
        "observed_at": "2025-07-01T00:00:00Z",
    }
    base.update(overrides)
    return HazardCase.model_validate(base)


class TestBuiltinLibrary:
    def test_file_exists_and_loads(self) -> None:
        assert DEFAULT_CASES_PATH.exists()
        cases = load_builtin_cases()
        assert len(cases) >= 12

    def test_covers_tibet_hazards(self) -> None:
        hazards = {hazard for case in load_builtin_cases() for hazard in case.hazard_types}
        # 泥石流 / 冰湖溃决 / 雪崩 / 融雪 / 冰雪灾害
        assert {"debris_flow", "lake_outburst", "avalanche", "snow_melt", "ice_snow"} <= hazards

    def test_case_ids_unique_and_indexable(self) -> None:
        cases = load_builtin_cases()
        index = cases_by_id(cases)
        assert len(index) == len(cases)

    def test_every_case_carries_ported_fields(self) -> None:
        for case in load_builtin_cases():
            for field in PORTED_FIELDS:
                assert getattr(case, field) is not None, f"{case.case_id} 缺 {field}"
            assert case.why_now.strip()
            assert case.target_groups
            assert case.actions
            assert case.monitoring_metrics, f"{case.case_id} 无监测指标则无法闭环评估"
            assert case.state_effects, f"{case.case_id} 无态势影响向量"
            assert case.confidence > 0

    def test_state_effect_keys_use_documented_vocabulary(self) -> None:
        for case in load_builtin_cases():
            unknown = set(case.state_effects) - DEFAULT_STATE_EFFECT_KEYS
            assert not unknown, f"{case.case_id} 使用了未登记的状态分量 {unknown}"

    def test_data_is_json_not_python_constants(self) -> None:
        raw = json.loads(DEFAULT_CASES_PATH.read_text(encoding="utf-8"))
        assert isinstance(raw, list)
        assert all(isinstance(item, dict) for item in raw)

    def test_loading_twice_is_deterministic(self) -> None:
        first = [c.model_dump() for c in load_builtin_cases()]
        second = [c.model_dump() for c in load_builtin_cases()]
        assert first == second


class TestValidation:
    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"case_id": "case_1"}, "case_id"),
            ({"confidence": 1.4}, "confidence"),
            ({"estimated_delay_hours": -1}, "estimated_delay_hours"),
            ({"hazard_types": []}, "hazard_types"),
            ({"hazard_types": ["Debris_Flow"]}, "灾种"),
            ({"hazard_types": ["debris_flow", "debris_flow"]}, "重复"),
            ({"region_prefixes": ["西藏"]}, "区域前缀"),
            ({"applies_to_levels": [0, 9]}, "等级"),
            ({"category": "propaganda"}, "category"),
            ({"phase": "aftermath"}, "phase"),
            ({"observed_at": "昨天"}, "昨天"),
            ({"actions": []}, "actions"),
        ],
    )
    def test_rejects_bad_input(self, overrides: dict[str, object], expected: str) -> None:
        with pytest.raises(ValidationError) as caught:
            sample_case(**overrides)
        assert expected in str(caught.value)

    def test_rejects_missing_required_field(self) -> None:
        payload = sample_case().model_dump()
        del payload["why_now"]
        with pytest.raises(ValidationError, match="why_now"):
            HazardCase.model_validate(payload)

    def test_rejects_out_of_range_state_effects(self) -> None:
        with pytest.raises(ValidationError, match=r"\[-1,1\]"):
            sample_case(state_effects={"risk_exposure": 2.0})

    def test_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            HazardCase.model_validate({**sample_case().model_dump(), "unexpected": 1})

    def test_levels_sorted_and_deduplicated(self) -> None:
        assert sample_case(applies_to_levels=[3, 1, 3]).applies_to_levels == [1, 3]

    def test_valid_at_is_timezone_aware(self) -> None:
        assert sample_case().valid_at.tzinfo is not None
        assert sample_case().valid_at.tzinfo == UTC


class TestRenderers:
    def test_episode_text_is_reproducible_and_carries_identity(self) -> None:
        case = sample_case()
        text = case.to_episode_text()
        assert text == case.to_episode_text()
        assert case.case_id in text
        assert case.observed_at in text
        assert case.why_now in text
        assert "向沟岸两侧台地转移" in text
        assert "risk_exposure=-0.3" in text

    def test_episode_text_marks_missing_sections_instead_of_dropping_lines(self) -> None:
        case = sample_case(trigger_signals=[], expected_effects=[], source_note="")
        text = case.to_episode_text()
        assert "成灾征兆：未标注" in text
        assert "预期效果：未标注" in text
        assert "案例来源" not in text

    def test_searchable_text_lowercases_and_joins_fields(self) -> None:
        text = sample_case().searchable_text()
        assert "debris_flow" in text and "泥位" in text and "evacuate" in text

    def test_plan_brief_exposes_only_planning_slices(self) -> None:
        brief = sample_case().plan_brief()
        assert set(brief) == {
            "case_id",
            "title",
            "category",
            "phase",
            "actions",
            "why_now",
            "required_prerequisites",
            "possible_side_effects",
            "monitoring_metrics",
            "estimated_delay_hours",
            "confidence",
            "state_effects",
            "source_note",
        }

    def test_region_and_hazard_predicates(self) -> None:
        case = sample_case(region_prefixes=["5401"], hazard_types=["avalanche"])
        assert case.matches_region("540121")
        assert not case.matches_region("540600")
        assert case.matches_region(None)
        assert case.matches_hazard("avalanche")
        assert not case.matches_hazard("debris_flow")
        assert case.matches_hazard(None)

    def test_unbounded_case_matches_everything(self) -> None:
        case = sample_case(region_prefixes=[])
        assert case.matches_region("999999")

    def test_hazard_labels_fall_back_to_token(self) -> None:
        case = sample_case(hazard_types=["snow_melt"])
        assert case.hazard_labels == ["snow_melt"]
        assert sample_case(hazard_types=["debris_flow"]).hazard_labels == ["泥石流"]


class TestLoaderErrors:
    def _write(self, path: Path, payload: object) -> Path:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_missing_file_raises_typed_error(self, tmp_path: Path) -> None:
        with pytest.raises(SchemaInvalidError):
            load_cases(tmp_path / "absent.json")

    def test_malformed_json_raises_typed_error(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(SchemaInvalidError, match="合法 JSON"):
            load_cases(path)

    def test_empty_array_is_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path / "empty.json", [])
        with pytest.raises(SchemaInvalidError, match="非空对象数组"):
            load_cases(path)

    def test_single_invalid_entry_reports_all_errors_together(self, tmp_path: Path) -> None:
        payload = [sample_case().model_dump(), {"case_id": "bad"}]
        path = self._write(tmp_path / "mixed.json", payload)
        with pytest.raises(SchemaInvalidError, match="1 条不合法"):
            load_cases(path)

    def test_duplicate_case_id_rejected(self, tmp_path: Path) -> None:
        payload = [sample_case().model_dump(), sample_case().model_dump()]
        path = self._write(tmp_path / "dup.json", payload)
        with pytest.raises(SchemaInvalidError, match="重复 case_id"):
            load_cases(path)
