"""物理因子的规范目录：单位与物理值域的**唯一真源**。

此前同一件事散在三处各写各的：派生侧（`pipeline/assess_input.py`）、数据侧
（`agents/hazards/data_agent/tools/hazard_data_registry.py`）、风险侧
（`agents/hazards/risk_agent/tools/*_risk_tool.py`）。同一个因子在三处的单位串能差出
`mm/day` / `mm/d`、`°C` / `℃`、`1` / `""`——三处之间没有任何对账门禁，差一个字符也没人拦，
而单位串会一路写进证据包、因子分与台账。

这张表把三处收敛到一份：

- `unit`：**规范单位串**。无量纲写 `""`（空串不是"忘了填"，是"这一项本来就没有单位"；
  与连接器侧 `UNKNOWN_UNIT = "unknown"` 是两回事——后者说的是"不知道单位"）。
- `valid_range`：**与灾种无关**的物理值域。只给由遥测换算而来的动态因子——
  越界就是"这个读数不对"，必须拒进因子。灾种相关的值域（滑坡的坡角 30–90° 与危岩的
  5–80° 不同）留在数据侧按灾种登记，不进这张表，否则这张表就得按灾种分裂。
- `spec_reference`：出处。写的是规范条款或登记来源——"转引"二字是事实状态：
  本仓没有逐条核对原文，引用链要能被追问。

**归一化边界不在这里**：那是风险侧的方法参数（`normalize.L/U`），不是物理量程。
两者的区别要守住——物理量程越界要**拒绝**（读数不对，不能进因子），归一化越界只是
**饱和**（方法把量压到 0–1）。混在一起就会出现"400 mm/h 被当成满分暴雨因子"这种静默失真。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FactorSpec:
    """一个物理因子的规范口径。"""

    #: 因子名（`FactorObservation.factor` 的取值）。
    name: str
    #: 规范单位串；无量纲写 `""`。
    unit: str
    #: 登记区间（与数据侧一致）。**注意这多半是方法的标定带，不是物理不可能线**——
    #: 气温的 -40–20 ℃ 是"融雪型"算法标定的有效带，不是西藏的气温物理极限；
    #: 池水位比的上界 1 更是"漫顶"（溃决）这个**最危险的状态**，不是越界。
    valid_range: tuple[float, float] | None = None
    #: 这个区间是不是**物理不可能线**（读数越了就是坏数据，必须拒，不能进因子）。
    #: 只有物理线才拒读数；标定带越界是方法外推，丢掉读数才是错的。
    physical_bound: bool = False
    #: 出处（规范条款 / 登记来源）。
    spec_reference: str = ""


def _spec(
    name: str,
    unit: str,
    valid_range: tuple[float, float] | None,
    spec_reference: str,
    *,
    physical_bound: bool = False,
) -> FactorSpec:
    return FactorSpec(
        name=name,
        unit=unit,
        valid_range=valid_range,
        physical_bound=physical_bound,
        spec_reference=spec_reference,
    )


#: 全部已登记因子的规范口径。**新增因子必须先在这里登记**——派生侧、数据侧、风险侧
#: 都从这张表取单位，三处对账门禁（`tests/unit/test_assess_derivation_contract.py`）
#: 会对账，没登记的因子会让门禁红。
FACTOR_CATALOG: dict[str, FactorSpec] = {
    spec.name: spec
    for spec in (
        # ---- 由遥测换算而来的动态因子：值域与灾种无关，进这张表 ----
        # 小时雨强：数据侧三灾种一致登记 0–300 mm/h（世界纪录量级），**是物理线**——
        # 越了就是坏读数，必须拒。10 分钟累计折小时雨强要 ×6，所以源口径（0–50 mm/10min）
        # 越界要在**换算前**就拦住。
        _spec("rainfall_mm_h", "mm/h", (0.0, 300.0), "DZ/T 0438—2023 表E.3（转引）", physical_bound=True),
        # 地表位移速率：数据侧登记 0–500 mm/d。风险侧的归一化上限是 100 mm/d（方法参数），
        # 两者的差就是"物理合法但方法饱和"的那一段——所以 500 这一条**不是**物理线。
        _spec("displacement_mm_day", "mm/d", (0.0, 500.0), "监测矩阵"),
        _spec("crack_width_mm", "mm", (0.0, 200.0), "DZ/T 0438—2023 表E.2（转引）"),
        # 气温 -40–20 ℃ 是**融雪型算法**的标定带，不是气温的物理极限——西藏河谷盛夏
        # 能到 25 ℃，把它当物理线会把真实读数当坏数据丢掉。
        _spec("air_temp_c", "℃", (-40.0, 20.0), "DZ/T 0438—2023 6.3.8（转引）"),
        # 池水位比上界 1 是"漫顶"（溃决）这个**最危险的状态**，不是越界——更不能拒。
        _spec("water_level_ratio", "", (0.0, 1.0), "DZ/T 0438—2023 6.3.8.2（转引）"),
        _spec("snow_depth_cm", "cm", (0.0, 300.0), "监测矩阵"),
        _spec("new_snow_24h_cm", "cm", (0.0, 100.0), "监测矩阵"),
        # 体积比 0–1 是物理线：大于 1 的"含水率"只能是算错了。
        _spec("soil_moisture_ratio", "", (0.0, 1.0), "监测矩阵", physical_bound=True),
        # ---- 静态孕灾因子与类别因子：值域按灾种登记在数据侧，这里只统一单位 ----
        _spec("slope_angle_deg", "°", None, "DZ/T 0438—2023 6.3.8 + 藏东南文献（转引）"),
        _spec("groundwater_depth_m", "m", None, "DZ/T 0438—2023 6.4.1（转引）"),
        _spec("slope_height_m", "m", None, "DZ/T 0438—2023 6.4.2（转引）"),
        _spec("block_volume_m3", "m³", None, "DZ/T 0438—2023 6.3.8（转引）"),
        _spec("rockfall_history_count", "次", None, "监测矩阵"),
        _spec("catchment_area_km2", "km²", None, "DZ/T 0438—2023 6.4.3（转引）"),
        _spec("channel_gradient", "", None, "DZ/T 0438—2023 6.4.3（转引）"),
        _spec("sediment_supply_index", "", None, "DZ/T 0438—2023 6.4.3（转引）"),
        _spec("elevation_m", "m", None, "藏东南文献（转引）"),
        _spec("aspect", "", None, "藏东南文献（转引）"),
        _spec("lake_volume_m3", "m³", None, "DZ/T 0438—2023 6.3.8.2（转引）"),
        _spec("dam_height_m", "m", None, "DZ/T 0438—2023 6.3.8.2（转引）"),
        _spec("glacier_activity", "", None, "DZ/T 0438—2023 6.3.8.2（转引）"),
        _spec("ice_avalanche_into_lake", "", None, "DZ/T 0438—2023 6.3.8.2（转引）"),
        _spec("seepage_level", "", None, "DZ/T 0438—2023 6.3.8.2（转引）"),
        _spec("landslide_into_lake", "", None, "DZ/T 0438—2023 6.3.8.2（转引）"),
        _spec("downstream_distance_km", "km", None, "DZ/T 0438—2023 6.3.8.2（转引）"),
        _spec("vibration_level", "", None, "监测矩阵"),
        _spec("failure_mode", "", None, "DZ/T 0438—2023 6.3.8（转引）"),
        _spec("water_source_type", "", None, "DZ/T 0438—2023 6.4.3（转引）"),
        _spec("upstream_breach_flag", "", None, "DZ/T 0438—2023 6.4.3（转引）"),
        _spec("spring_surge_flag", "", None, "DZ/T 0438—2023 6.4.3（转引）"),
        _spec("release_volume_m3", "m³", None, "DZ/T 0438—2023 6.3.8（转引）"),
        _spec("vertical_drop_m", "m", None, "DZ/T 0438—2023 6.3.8（转引）"),
    )
}


def factor_unit(name: str) -> str:
    """因子的规范单位串；未登记的因子返回空串。

    空串表示"这张表里没有这一项"——调用方要据此发现漏登记，而不是拿到一个
    看起来正常的单位把差异掩过去。
    """
    spec = FACTOR_CATALOG.get(name)
    return spec.unit if spec is not None else ""


def factor_valid_range(name: str) -> tuple[float, float] | None:
    """因子的登记区间；未登记或灾种相关的返回 `None`（表示"按灾种另议"）。"""
    spec = FACTOR_CATALOG.get(name)
    return spec.valid_range if spec is not None else None


def factor_physical_bound(name: str) -> bool:
    """这个因子的登记区间是不是物理不可能线（越了就是坏读数，必须拒）。"""
    spec = FACTOR_CATALOG.get(name)
    return bool(spec.physical_bound) if spec is not None else False


def factor_spec_reference(name: str) -> str:
    """因子的出处（规范条款 / 登记来源）；未登记返回空串。"""
    spec = FACTOR_CATALOG.get(name)
    return spec.spec_reference if spec is not None else ""


__all__ = [
    "FACTOR_CATALOG",
    "FactorSpec",
    "factor_physical_bound",
    "factor_spec_reference",
    "factor_unit",
    "factor_valid_range",
]
