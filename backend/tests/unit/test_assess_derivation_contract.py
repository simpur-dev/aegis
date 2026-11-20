"""量纲与值域的**对账门禁**：派生侧、数据侧、风险侧三处必须说同一件事。

此前同一个因子的单位串在三处各写各的（派生侧 `mm/day`、数据侧与风险侧 `mm/d`；派生侧 `°C`、
另两处 `℃`；派生侧 `1`、另两处 `""`），**三处之间没有任何对账门禁**——差一个字符也没人拦，
而单位串会一路写进证据包、因子分与台账。同理还有三处各自写的 `required` 字段、
三处各自写的归一化边界。

这张门禁把"三处一致"变成机器判定：任何一处改了口径而另两处没跟着改，红的是门禁，
不是等人在评审里看出来。写法和 `tests/unit/test_trigger_rules_library.py`
（代码种子 vs 库种子逐字段对账）同构。
"""

from __future__ import annotations

import pytest

from aegis.agents.hazards.data_agent.tools.hazard_data_registry import HAZARD_DATA_REQUIREMENTS
from aegis.agents.hazards.risk_agent.tools.avalanche_risk_tool import FACTOR_CONFIG as AVALANCHE_CONFIG
from aegis.agents.hazards.risk_agent.tools.debris_flow_risk_tool import FACTOR_CONFIG as DEBRIS_CONFIG
from aegis.agents.hazards.risk_agent.tools.glacial_lake_risk_tool import FACTOR_CONFIG as LAKE_CONFIG
from aegis.agents.hazards.risk_agent.tools.landslide_risk_tool import FACTOR_CONFIG as LANDSLIDE_CONFIG
from aegis.agents.hazards.risk_agent.tools.rockfall_risk_tool import FACTOR_CONFIG as ROCKFALL_CONFIG
from aegis.connectors.metrics import METRIC_UNITS
from aegis.domain.factors import FACTOR_CATALOG
from aegis.pipeline.assess_input import DERIVATIONS, UNMAPPED_REASONS

RISK_CONFIGS = {
    "avalanche": AVALANCHE_CONFIG,
    "debris_flow": DEBRIS_CONFIG,
    "lake_outburst": LAKE_CONFIG,
    "landslide": LANDSLIDE_CONFIG,
    "rockfall": ROCKFALL_CONFIG,
}


def _risk_factors() -> dict[str, dict]:
    """风险侧全部因子声明（按因子名归并；同名因子在各灾种的声明应一致）。"""
    merged: dict[str, dict] = {}
    for config in RISK_CONFIGS.values():
        for group in ("P", "T", "I"):
            for declaration in config["groups"].get(group, []):
                key = declaration["key"]
                previous = merged.get(key)
                if previous is not None:
                    assert previous["unit"] == declaration["unit"], (
                        f"因子 {key} 在两个灾种侧的单位串不一致：{previous['unit']} vs {declaration['unit']}"
                    )
                    continue
                merged[key] = declaration
    return merged


def _data_fields() -> dict[str, dict]:
    """数据侧全部观测字段声明（按字段名归并）。"""
    merged: dict[str, dict] = {}
    for requirement in HAZARD_DATA_REQUIREMENTS.values():
        for name, spec in requirement["observation_fields"].items():
            previous = merged.get(name)
            if previous is not None:
                assert previous["unit"] == spec["unit"], f"字段 {name} 在两个灾种侧的单位串不一致：{previous['unit']} vs {spec['unit']}"
                continue
            merged[name] = spec
    return merged


class TestMetricCoverage:
    def test_每个遥测metric都有明确去处(self) -> None:
        """每个 metric 要么被派生表接住，要么在"禁止近似"清单里写明原因——不许有第三种。"""
        assert set(DERIVATIONS) | set(UNMAPPED_REASONS) == set(METRIC_UNITS)

    def test_不映射的metric都写明了原因(self) -> None:
        for metric, reason in UNMAPPED_REASONS.items():
            assert reason.strip(), f"{metric} 被列为不映射却没有写原因"


class TestFactorCatalog:
    def test_派生表引用的因子都在目录里登记(self) -> None:
        for metric, declaration in DERIVATIONS.items():
            assert declaration.factor in FACTOR_CATALOG, (
                f"{metric} 派生的因子 {declaration.factor} 没在因子目录登记：新因子必须先进目录，三处才有一份口径"
            )

    def test_派生表取到的单位就是目录里的单位(self) -> None:
        for _metric, declaration in DERIVATIONS.items():
            assert declaration.unit == FACTOR_CATALOG[declaration.factor].unit

    def test_风险侧的单位与目录一致(self) -> None:
        for key, declaration in _risk_factors().items():
            assert key in FACTOR_CATALOG, f"风险侧因子 {key} 没在因子目录登记"
            assert declaration["unit"] == FACTOR_CATALOG[key].unit, (
                f"风险侧因子 {key} 的单位是 {declaration['unit']!r}，目录里是 {FACTOR_CATALOG[key].unit!r}"
            )

    def test_数据侧的单位与目录一致(self) -> None:
        for name, spec in _data_fields().items():
            assert name in FACTOR_CATALOG, f"数据侧字段 {name} 没在因子目录登记"
            assert spec["unit"] == FACTOR_CATALOG[name].unit, (
                f"数据侧字段 {name} 的单位是 {spec['unit']!r}，目录里是 {FACTOR_CATALOG[name].unit!r}"
            )

    def test_目录里的物理值域与数据侧登记一致(self) -> None:
        """目录给了值域的因子，数据侧登记的值域必须一模一样。

        目录只给**与灾种无关**的值域；灾种相关的（坡角 5–80 / 30–90 / 20–60）留在数据侧，
        那张表目录装不下。
        """
        data_fields = _data_fields()
        for key, spec in FACTOR_CATALOG.items():
            if spec.valid_range is None:
                continue
            assert key in data_fields, f"目录给了 {key} 的值域，数据侧却没有登记它"
            assert tuple(data_fields[key]["range"]) == spec.valid_range, (
                f"因子 {key} 的物理值域两侧不一致：目录 {spec.valid_range} vs 数据侧 {data_fields[key]['range']}"
            )


class TestNormalizationBoundary:
    def test_三处雨量归一化边界一致(self) -> None:
        """滑坡/危岩/泥石流三处各写了一份 `rainfall_mm_h` 的归一化边界，改一处不改另两处就会静默漂移。"""
        boundaries: set[tuple] = set()
        sources: set[str] = set()
        seen: list[str] = []
        for name, config in RISK_CONFIGS.items():
            for group in ("P", "T", "I"):
                for declaration in config["groups"].get(group, []):
                    if declaration["key"] != "rainfall_mm_h":
                        continue
                    seen.append(name)
                    normalize = declaration["normalize"]
                    boundaries.add((normalize["method"], normalize["L"], normalize["U"]))
                    # 出处登记本身也要一致（它是 dict，取其中可比的字段）
                    source = normalize.get("boundary_source")
                    assert source is not None, f"{name} 的雨量归一化边界没有出处登记"
                    sources.add(str(source.get("method")) + "|" + str(source.get("source_reference")))
        assert len(seen) == 3, f"雨量因子声明的灾种数不是 3：{sorted(seen)}"
        assert len(boundaries) == 1, f"三处雨量归一化边界不一致：{sorted(boundaries)}"
        assert len(sources) == 1, f"三处雨量归一化边界的出处登记不一致：{sorted(sources)}"

    def test_归一化上界不超过物理值域(self) -> None:
        """归一化上界（方法参数）不得越过因子的物理上界（值域）。

        越过意味着"物理上已经不可能的读数"反而拿得到满分因子——那不是保守，是失真。
        """
        risk_factors = _risk_factors()
        for key, declaration in risk_factors.items():
            spec = FACTOR_CATALOG.get(key)
            if spec is None or spec.valid_range is None:
                continue
            normalize = declaration.get("normalize", {})
            upper = normalize.get("U")
            if upper is None:
                continue
            assert upper <= spec.valid_range[1], f"因子 {key} 的归一化上界 {upper} 越过物理上界 {spec.valid_range[1]}"


class TestSourceRange:
    def test_源口径上界等于因子上界折回(self) -> None:
        """声明了源口径值域的派生，它的源口径上界必须等于因子上界除以换算系数。

        少一步这一步，源口径与因子口径就会各说各话——源口径漏了 ×6，
        换算后就是 6 倍的错值。
        """
        for metric, declaration in DERIVATIONS.items():
            if declaration.source_range is None:
                continue
            valid = declaration.valid_range
            assert valid is not None, f"{metric} 声明了源口径值域却没有因子的登记区间"
            assert declaration.scale > 0
            expected = (valid[0] / declaration.scale, valid[1] / declaration.scale)
            assert declaration.source_range == pytest.approx(expected), (
                f"{metric} 的源口径值域 {declaration.source_range} 不是因子上界 {valid} 除以换算系数 {declaration.scale}"
            )


class TestRequiredSets:
    """数据侧与风险侧的"必需字段"必须同一套。

    引擎的覆盖率门控按风险侧的 `required_factors` 判"缺必需因子 → 数据不足"；
    数据侧若报另一套，两条腿对"什么才算够"各说各话——雪崩与冰湖此前正是这样。
    """

    def test_必需字段两侧一致(self) -> None:
        risk_required = {
            "landslide": set(LANDSLIDE_CONFIG["required_factors"]),
            "rockfall": set(ROCKFALL_CONFIG["required_factors"]),
            "debris_flow": set(DEBRIS_CONFIG["required_factors"]),
            "avalanche": set(AVALANCHE_CONFIG["required_factors"]),
            "lake_outburst": set(LAKE_CONFIG["required_factors"]),
        }
        for hazard, requirement in HAZARD_DATA_REQUIREMENTS.items():
            assert hazard in risk_required, f"风险侧没有 {hazard} 的因子声明"
            data_required = set(requirement["required_observation_fields"])
            assert data_required == risk_required[hazard], (
                f"{hazard} 的必需字段两侧不一致：数据侧 {sorted(data_required)} vs 风险侧 {sorted(risk_required[hazard])}"
            )

    def test_必需字段都在观测字段里声明过(self) -> None:
        for hazard, requirement in HAZARD_DATA_REQUIREMENTS.items():
            declared = set(requirement["observation_fields"])
            for name in requirement["required_observation_fields"]:
                assert name in declared, f"{hazard} 把 {name} 列为必需，却没在观测字段里声明"

    def test_只有物理不可能线才声明源口径值域(self) -> None:
        """**方法标定带不是物理线**：拿标定带当物理线会把真实极端当坏数据丢掉。

        气温 -40–20 ℃ 是融雪算法的标定带（西藏河谷 25 ℃ 是正常读数）；
        池水位比上界 1 是"漫顶"（溃决）这个最危险的状态。只有雨量与含水率是物理线。
        """
        for metric, declaration in DERIVATIONS.items():
            if not declaration.physical_bound:
                assert declaration.source_range is None, f"{metric} 的登记区间是方法标定带，不该声明源口径物理线"
                continue
            assert declaration.source_range is not None, f"{metric} 是物理不可能线，必须声明源口径值域（换算前就拦）"

    def test_物理线的因子都有登记区间(self) -> None:
        for name, spec in FACTOR_CATALOG.items():
            if spec.physical_bound:
                assert spec.valid_range is not None, f"因子 {name} 标了物理线却没有登记区间"
