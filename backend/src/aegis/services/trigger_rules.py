"""触发条件规则引擎（灾种防控任务解析的"快路径"）。

纯函数式、零 I/O、无副作用：给定遥测序列即可判定 5 类高原灾种的触发条件命中，
因此可完全单元测试覆盖，并作为智能体缺位时的降级判定（fallback: degrade_to_rule）。

语义约定：
- 按 (站点, 指标) 分组评估，站点之间不混合数值；
- 每条条件自带时间窗（window_seconds），窗口外读数不参与；
- quality_flag != ok 的读数（劣化/缺失/漂移）不参与判定；
- 晚于判定时刻的读数不参与判定；
- 边界取"闭区间"：实测值等于阈值即视为命中。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from aegis.domain.enums import HazardType, RiskLevel
from aegis.domain.messages import TelemetryReading, TriggerHit, parse_iso

AGGREGATIONS = ("last", "max", "sum", "avg", "rate", "count")
OPERATORS = (">=", ">", "<=", "<", "==")

_OPS = {
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
    "==": lambda a, b: a == b,
}


@dataclass(frozen=True, slots=True)
class Condition:
    metric: str
    op: str
    threshold: float
    agg: str = "last"
    window_seconds: int = 3600

    def __post_init__(self) -> None:
        if self.op not in OPERATORS:
            raise ValueError(f"不支持的比较算子: {self.op}")
        if self.agg not in AGGREGATIONS:
            raise ValueError(f"不支持的聚合方式: {self.agg}")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds 必须为正")


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    hazard_type: HazardType
    description: str
    conditions: tuple[Condition, ...]
    mode: str = "all"  # all | any
    triggered_level: RiskLevel = RiskLevel.ORANGE
    weight: float = 1.0

    def __post_init__(self) -> None:
        if not self.conditions:
            raise ValueError("规则必须至少含一个条件")
        if self.mode not in ("all", "any"):
            raise ValueError(f"不支持的组合模式: {self.mode}")
        if not 0 < self.weight <= 10:
            raise ValueError("weight 取值范围 (0, 10]")


@dataclass(slots=True)
class RuleEvaluation:
    hits: list[TriggerHit] = field(default_factory=list)
    evaluated: int = 0
    stations: int = 0
    metrics_used: set[str] = field(default_factory=set)


Series = list[tuple[datetime, float]]


def aggregate(values: list[float], agg: str) -> float | None:
    """对数值序列做聚合。空输入返回 None（调用方判为未命中）。"""
    if not values:
        return None
    if agg == "last":
        return values[-1]
    if agg == "max":
        return max(values)
    if agg == "sum":
        return sum(values)
    if agg == "avg":
        return sum(values) / len(values)
    if agg == "count":
        return float(len(values))
    raise ValueError(f"不支持的聚合方式: {agg}")


def windowed_series(series: Series, window_seconds: int, ref: datetime) -> Series:
    """取 (ref-window, ref] 区间内的读数，按时间升序。"""
    in_window = [(moment, value) for moment, value in series if moment <= ref and (ref - moment).total_seconds() <= window_seconds]
    return sorted(in_window, key=lambda item: item[0])


def metric_value(series: Series, condition: Condition, ref: datetime) -> float | None:
    """按条件的时间窗与聚合方式取值。rate 至少需要窗口内两个采样点。"""
    windowed = windowed_series(series, condition.window_seconds, ref)
    if not windowed:
        return None
    values = [value for _moment, value in windowed]
    if condition.agg == "rate":
        if len(windowed) < 2:
            return None
        elapsed_hours = (windowed[-1][0] - windowed[0][0]).total_seconds() / 3600
        if elapsed_hours <= 0:
            return None
        return (values[-1] - values[0]) / elapsed_hours
    return aggregate(values, condition.agg)


class RuleEngine:
    def __init__(self, rules: list[Rule] | None = None) -> None:
        self._rules: list[Rule] = list(rules) if rules is not None else list(default_rulebook())

    @property
    def rules(self) -> tuple[Rule, ...]:
        return tuple(self._rules)

    def rule_by_id(self, rule_id: str) -> Rule | None:
        return next((r for r in self._rules if r.rule_id == rule_id), None)

    def rules_for(self, hazard_type: HazardType) -> list[Rule]:
        return [r for r in self._rules if r.hazard_type is hazard_type]

    def evaluate(
        self,
        readings: list[TelemetryReading],
        *,
        now: datetime | None = None,
        region_code: str | None = None,
    ) -> RuleEvaluation:
        result = RuleEvaluation(evaluated=len(self._rules))
        if not readings:
            return result

        usable = [r for r in readings if r.quality_flag == "ok"]
        if not usable:
            return result

        ref = now or max(parse_iso(r.observed_at) for r in usable)
        per_station: dict[str, dict[str, Series]] = defaultdict(lambda: defaultdict(list))
        station_region: dict[str, str] = {}
        for reading in usable:
            moment = parse_iso(reading.observed_at)
            per_station[reading.station_id][reading.metric].append((moment, reading.value))
            station_region.setdefault(reading.station_id, reading.region_code)
            result.metrics_used.add(reading.metric)

        for station_id, metrics in sorted(per_station.items()):
            region = station_region[station_id]
            if region_code and region != region_code:
                continue
            result.stations += 1
            for rule in self._rules:
                if (hit := self._evaluate_rule(rule, metrics, ref, region, station_id)) is not None:
                    result.hits.append(hit)
        return result

    def _evaluate_rule(
        self,
        rule: Rule,
        metrics: dict[str, Series],
        ref: datetime,
        region: str,
        station_id: str,
    ) -> TriggerHit | None:
        outcomes: list[bool] = []
        evidence: list[str] = []

        for condition in rule.conditions:
            value = metric_value(metrics.get(condition.metric, []), condition, ref)
            if value is None:
                outcomes.append(False)
                continue
            if _OPS[condition.op](value, condition.threshold):
                outcomes.append(True)
                evidence.append(f"{station_id}:{condition.metric}={round(value, 3)}{condition.op}{condition.threshold}")
            else:
                outcomes.append(False)

        matched = all(outcomes) if rule.mode == "all" else any(outcomes)
        if not matched:
            return None

        score = sum(1 for o in outcomes if o) / len(outcomes)
        return TriggerHit(
            rule_id=rule.rule_id,
            hazard_type=rule.hazard_type.value,
            region_code=region,
            evidence_refs=evidence,
            score=round(score, 4),
            observed_at=ref.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        )


def default_rulebook() -> list[Rule]:
    """5 类高原典型灾种触发条件（阈值量级取自公开规范，可由智能体方/专家评审替换）。"""
    return [
        Rule(
            "R-DEBRIS-RAIN-1",
            HazardType.DEBRIS_FLOW,
            "短时强降雨激发泥石流",
            (Condition("rain_10min", ">=", 30.0, "max", 3600),),
            triggered_level=RiskLevel.ORANGE,
        ),
        Rule(
            "R-DEBRIS-RAIN-2",
            HazardType.DEBRIS_FLOW,
            "持续降雨叠加沟道泥位抬升",
            (
                Condition("rain_cumulative_24h", ">=", 80.0, "max", 86_400),
                Condition("debris_level", ">=", 1.0, "max", 1800),
            ),
            triggered_level=RiskLevel.RED,
        ),
        Rule(
            "R-LANDSLIDE-1",
            HazardType.LANDSLIDE,
            "降雨入渗叠加位移加速",
            (
                Condition("rain_cumulative_24h", ">=", 60.0, "max", 86_400),
                Condition("displacement_mm", ">=", 20.0, "max", 3600),
            ),
            triggered_level=RiskLevel.ORANGE,
        ),
        Rule(
            "R-LANDSLIDE-2",
            HazardType.LANDSLIDE,
            "位移速率持续增加（蠕变加速阶段）",
            (Condition("displacement_mm", ">=", 5.0, "rate", 21_600),),
            triggered_level=RiskLevel.YELLOW,
            weight=0.8,
        ),
        Rule(
            "R-ROCKFALL-1",
            HazardType.ROCKFALL,
            "冻融循环叠加危岩裂缝扩展",
            (
                Condition("freeze_thaw_cycles", ">=", 3.0, "max", 86_400),
                Condition("crack_aperture_mm", ">=", 15.0, "max", 21_600),
            ),
            triggered_level=RiskLevel.ORANGE,
        ),
        Rule(
            "R-AVALANCHE-1",
            HazardType.AVALANCHE,
            "新雪叠加强风吹雪",
            (
                Condition("new_snow_cm", ">=", 25.0, "max", 21_600),
                Condition("wind_speed_ms", ">=", 14.0, "max", 3600),
            ),
            triggered_level=RiskLevel.ORANGE,
        ),
        Rule(
            "R-AVALANCHE-2",
            HazardType.AVALANCHE,
            "气温骤升致雪层弱层失稳",
            (
                Condition("air_temperature_c", ">=", 2.0, "max", 3600),
                Condition("snow_water_equivalent_mm", ">=", 40.0, "max", 21_600),
            ),
            triggered_level=RiskLevel.YELLOW,
            weight=0.7,
        ),
        Rule(
            "R-LAKE-1",
            HazardType.LAKE_OUTBURST,
            "冰湖水位快速抬升",
            (Condition("lake_level_m", ">=", 0.5, "max", 21_600),),
            triggered_level=RiskLevel.ORANGE,
        ),
        Rule(
            "R-LAKE-2",
            HazardType.LAKE_OUTBURST,
            "水位抬升叠加坝体渗流浑浊",
            (
                Condition("lake_level_m", ">=", 0.3, "max", 86_400),
                Condition("dam_seepage_turbidity_ntu", ">=", 50.0, "max", 21_600),
            ),
            triggered_level=RiskLevel.RED,
        ),
    ]
