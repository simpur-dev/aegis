"""研判证据构建：把窗口内的遥测与区域画像折成 `AssessEvidence`（assess.v1 的输入侧）。

**派生表是唯一合法口径**。外部因子引擎要的是物理因子（`rainfall_mm_h` 等），而平台遥测是
站点原始量（`rain_10min` 等）；两者之间的换算只能发生在这里，且每条派生都写明公式。
没有把握的换算一律不映射：`new_snow_cm` 不知道区间是不是 24h，就不能冒充 `new_snow_24h_cm`；
浊度 NTU 不是序数等级，就不能冒充 `seepage_level`。进 `unmapped_metrics` 并写原因，
比造一个看着合理的数诚实——伪装的因子会一路走到预警里。

**换算系数与值域写在声明里，不写在代码分支里**（`Derivation`）。此前 ×6 是 `_aggregate` 里
一行 `metric == "rain_10min"`：新数据源若直接给 mm/h 或日累计，会被静默放大 6 倍——
不报错、不越界，一路走进因子分。声明式之后，"这个源的节拍是什么"是表里的一行。

**值域越界按三档处置**（与仓里既有的分工同构）：物理不可能线越界 → 剔除该读数并在 notes 报数；
换算后仍越出物理线 → **拒绝**（不产因子，进 `unmapped_metrics` 并写明原因）；
量纲本身没把握 → 早已在 `UNMAPPED_REASONS` 里。**方法标定带不是物理线**（气温 -40–20 ℃ 是
融雪算法的标定带、池水位比上界 1 是"漫顶"），越了不剔——那是方法外推，由风险侧的归一化处理。
因子登记的唯一真源在 `aegis.domain.factors` 的因子目录里，与数据侧、风险侧共用一份。

证据包自限（条数与字节数）：网关与总线的信封有上限，超限必须显式置 `truncated=True` 并留 note，
不做静默截断。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aegis.domain.assess import AssessEvidence, FactorObservation, UnmappedMetric
from aegis.domain.factors import factor_physical_bound, factor_unit, factor_valid_range
from aegis.domain.messages import TelemetryReading, TriggerHit, now_iso, parse_iso
from aegis.domain.provenance import DataKind, DataProvenance
from aegis.domain.site import SiteProfile
from aegis.storage.store import StoreProtocol

log = logging.getLogger("aegis.pipeline.assess_input")


def _within(value: float, bounds: tuple[float, float] | None) -> bool:
    """值是否落在登记值域内；`bounds` 为 None 表示这一项没有登记值域（不判）。"""
    if bounds is None:
        return True
    return bounds[0] <= value <= bounds[1]


@dataclass(frozen=True, slots=True)
class Derivation:
    """一条遥测 metric → 物理因子的派生声明。

    换算系数写在声明里，不写在代码分支里。此前 ×6 是 `_aggregate` 里一行
    `metric == "rain_10min"` 的硬编码：任何新数据源若直接给 mm/h 或日累计，都会被
    **静默放大 6 倍**——不报错、不越界，一路走进因子分与预警正文。声明式之后，
    "这个源的节拍是什么"是表里的一行，换错系数就是改错一行，而不是改错一个分支。
    """

    #: 目标因子名（登记在 `aegis.domain.factors` 的目录里，单位与值域从那里取）。
    factor: str
    #: 源口径 → 因子口径的换算系数（10 分钟累计 → 小时雨强 = 6.0）。
    scale: float = 1.0
    #: 换算说明，进派生说明文字。
    scale_note: str = ""
    #: 源口径的合法值域；越界的读数在**换算前**就剔掉（换算只会把错值放大）。
    source_range: tuple[float, float] | None = None
    #: 聚合方式（进派生说明文字）。
    agg: str = "max"

    @property
    def unit(self) -> str:
        """因子的规范单位串（唯一真源在 `aegis.domain.factors`）。"""
        return factor_unit(self.factor)

    @property
    def valid_range(self) -> tuple[float, float] | None:
        """因子的登记区间（唯一真源在 `aegis.domain.factors`）。"""
        return factor_valid_range(self.factor)

    @property
    def physical_bound(self) -> bool:
        """登记区间是不是**物理不可能线**（越了就是坏读数，必须拒）。

        标定带不是物理线：气温 -40–20 ℃ 是融雪算法的标定带（西藏河谷 25 ℃ 是正常读数），
        池水位比上界 1 是"漫顶"这个最危险的状态。拿标定带当物理线，会把真实极端当坏数据丢掉。
        """
        return factor_physical_bound(self.factor)

    def describe(self, metric: str) -> str:
        """派生说明（进 `FactorObservation.derivation`）：要能读懂为什么是这个系数。"""
        if self.scale == 1.0:
            return f"窗口内 {self.agg}({metric})"
        return f"窗口内 {self.agg}({metric})×{self.scale:g}（{self.scale_note}）"

    def source_range_reason(self, metric: str) -> str:
        """**换算前**越界的措辞（进 `unmapped_metrics.reason`）。"""
        source = self.source_range
        if source is None:
            return f"{metric} 越出物理不可能线"
        return f"{metric} 源口径越出物理不可能线 {source[0]}–{source[1]}"

    def range_reason(self, metric: str) -> str:
        """**换算后**越界的措辞（进 `unmapped_metrics.reason`）：说清是换算后越界。"""
        valid = self.valid_range
        if valid is None:
            return f"{metric} 越出登记值域"
        return f"{metric} 换算后越出 {self.factor} 的物理不可能线 {valid[0]}–{valid[1]}{self.unit}"


#: 遥测 metric → 派生声明。顺序即证据包里派生因子的出场顺序。
DERIVATIONS: dict[str, Derivation] = {
    # 10 分钟累计雨量 → 小时雨强。源口径上界 50 mm/10min（= 300 mm/h ÷ 6）：
    # 日累计值误填进这个 metric 时，换算前就该被拦住。
    "rain_10min": Derivation(
        factor="rainfall_mm_h",
        scale=6.0,
        scale_note="10 分钟→小时",
        source_range=(0.0, 50.0),
    ),
    # Open-Meteo 逐小时降水腿（`precip_hourly`）的原始量：小时值即小时雨强，恒等派生。
    # 也要登记在这张表里（而不是连接器直供因子名），×6 那类口径错换对这条腿同样有闸。
    "precip_mm_h": Derivation(
        factor="rainfall_mm_h",
        scale=1.0,
        scale_note="逐小时值即小时雨强",
        source_range=(0.0, 300.0),
    ),
    # 位移速率按每站首末差折算，值域在折算后判（见 `_derive_displacement`）。
    "displacement_mm": Derivation(factor="displacement_mm_day"),
    "crack_aperture_mm": Derivation(factor="crack_width_mm"),
    "air_temperature_c": Derivation(factor="air_temp_c"),
    "lake_level_m": Derivation(factor="water_level_ratio"),
}

#: 平台有、但因子体系里没有同一口径承接的遥测：显式列原因，禁止近似。
UNMAPPED_REASONS: dict[str, str] = {
    "new_snow_cm": "区间未必是 24h，不能冒充 new_snow_24h_cm",
    "dam_seepage_turbidity_ntu": "NTU 是浊度实测值，不是序数 seepage_level",
    "debris_level": "序数等级，无量纲因子无法承接",
    "wind_speed_ms": "因子体系中无对应风因子",
    "snow_water_equivalent_mm": "雪水当量不能冒充积雪厚度或新雪量",
    "freeze_thaw_cycles": "冻融循环次数无对应因子",
    "rain_cumulative_24h": "24h 累计与小时雨强口径不同，禁止近似",
}

_SIMULATED_SOURCES = frozenset({"simulator", "demo", "mock"})
_MOCK_DISCLAIMER = "证据包含模拟/演示数据，仅供演练与契约联调，不得作为实际预警依据。"
#: 库读视图一次最多取的条数；超过时存储层只回窗口内最新的一批，更早的读数是静默丢失的。
_STORE_READ_LIMIT = 5_000


class AssessEvidenceBuilder:
    def __init__(
        self,
        store: StoreProtocol | None,
        *,
        window_minutes: int = 60,
        max_observations: int = 48,
        max_unmapped: int = 24,
        max_payload_bytes: int = 48_000,
    ) -> None:
        if not 1 <= window_minutes <= 9_999:
            raise ValueError("window_minutes 必须在 1..9999（window 串最长 4 位数字）")
        self._store = store
        self._window_minutes = window_minutes
        self._max_observations = max_observations
        self._max_unmapped = max_unmapped
        self._max_payload_bytes = max_payload_bytes

    @property
    def window(self) -> str:
        return f"{self._window_minutes}m"

    async def build(
        self,
        *,
        region: str,
        hits: list[TriggerHit],
        readings: Sequence[TelemetryReading] | None = None,
        intake: str = "telemetry",
        now: datetime | None = None,
        window_minutes: int | None = None,
    ) -> AssessEvidence:
        minutes = self._window_minutes if window_minutes is None else window_minutes
        if not 1 <= minutes <= 9_999:
            raise ValueError("window_minutes 必须在 1..9999（window 串最长 4 位数字）")
        moment = now or datetime.now(UTC)
        since = moment - timedelta(minutes=minutes)
        generated_at = now_iso(moment)

        profile = await self._store.get_site_profile(region) if self._store is not None else None
        raw, store_capped = self._collect(region, readings or (), since, moment)
        notes: list[str] = []

        # 单趟过滤 + 分组：此前对 raw 走了 5 趟（两个 sum、一个 listcomp、一个有限值循环、
        # 一个分组循环）。窗口上限 5,000 条时这些全pass 是白花的；结果口径逐项不变。
        dropped_quality = 0
        dropped_region = 0
        non_finite = 0
        finite: list[TelemetryReading] = []
        by_metric: dict[str, list[TelemetryReading]] = {}
        for reading in raw:
            if reading.quality_flag != "ok":
                dropped_quality += 1
            if reading.region_code != region:
                dropped_region += 1
            if reading.region_code != region or reading.quality_flag != "ok":
                continue
            if not self._is_finite(reading.value):
                non_finite += 1
                continue
            finite.append(reading)
            by_metric.setdefault(reading.metric, []).append(reading)

        observations: list[FactorObservation] = []
        unmapped: list[tuple[str, int, str]] = []
        out_of_range = 0
        for metric, declaration in DERIVATIONS.items():
            group = by_metric.get(metric, [])
            if not group:
                continue
            # 物理线越界 → 剔（换算只会把错值放大，剔晚了就是 6 倍的错值）。
            # 标定带越界不剔：那是方法外推，不是坏读数——由风险侧的归一化处理（并留 note）。
            if declaration.physical_bound:
                kept = [reading for reading in group if _within(reading.value, declaration.source_range)]
                out_of_range += len(group) - len(kept)
                if not kept:
                    unmapped.append((metric, len(group), declaration.source_range_reason(metric)))
                    continue
            else:
                kept = group
            if metric == "displacement_mm":
                derived = self._derive_displacement(kept)
                if derived is None:
                    unmapped.append((metric, len(group), "每站至少需要 2 个 ok 样本且时间递增，否则算不出 mm/day"))
                else:
                    observations.append(derived)
            elif metric == "lake_level_m":
                derived = self._derive_water_level_ratio(kept, profile)
                if derived is None:
                    reason = "缺少 site_profile.dam_height_m（或值<=0），无法派生水位/坝高比"
                    unmapped.append((metric, len(group), reason))
                else:
                    observations.append(derived)
            else:
                derived, dropped = self._aggregate(metric, declaration, kept)
                out_of_range += dropped
                if derived is None:
                    # 整批都越出因子物理值域 → 拒绝，不产因子（引擎的覆盖率门控会接手判定"够不够"）
                    unmapped.append((metric, len(group), declaration.range_reason(metric)))
                else:
                    observations.append(derived)

        for metric, group in by_metric.items():
            if metric in DERIVATIONS:
                continue
            unmapped.append((metric, len(group), UNMAPPED_REASONS.get(metric, "不在平台派生表内，禁止近似映射")))

        observations.extend(self._site_observations(profile, generated_at))

        if hits:
            rule_ids = sorted({hit.rule_id for hit in hits})
            notes.append(f"规则命中 {len(hits)} 条：{', '.join(rule_ids[:8])}{'…' if len(rule_ids) > 8 else ''}")
        if dropped_region:
            notes.append(f"剔除跨区域读数 {dropped_region} 条")
        if dropped_quality:
            notes.append(f"剔除 quality_flag≠ok 的读数 {dropped_quality} 条")
        if non_finite:
            notes.append(f"剔除非有限值读数 {non_finite} 条")
        if out_of_range:
            notes.append(f"剔除源口径越界读数 {out_of_range} 条")
        if not finite and not observations:
            notes.append("窗口内没有可用的 ok 遥测，也未配置站点画像：证据为空")

        provenance = self._provenance(finite, profile, intake)
        truncated = store_capped
        if store_capped:
            notes.append(f"窗口内库存读数超过 {_STORE_READ_LIMIT} 条上限，库读只取最新的 {_STORE_READ_LIMIT} 条")
        if len(observations) > self._max_observations:
            observations = observations[: self._max_observations]
            truncated = True
            notes.append(f"观测条数超过上限 {self._max_observations}，已裁剪")
        if len(unmapped) > self._max_unmapped:
            unmapped = unmapped[: self._max_unmapped]
            truncated = True
            notes.append(f"未映射指标超过上限 {self._max_unmapped}，已裁剪")

        evidence = AssessEvidence(
            region_code=region,
            window=f"{minutes}m",
            generated_at=generated_at,
            observations=observations,
            site_profile=profile,
            provenance=provenance,
            unmapped_metrics=[UnmappedMetric(metric=metric, samples=samples, reason=reason) for metric, samples, reason in unmapped],
            notes=notes[:16],
            truncated=truncated,
        )
        return self._enforce_payload_bound(evidence)

    # ---------- 采集与派生 ----------

    def _collect(
        self, region: str, readings: Sequence[TelemetryReading], since: datetime, until: datetime
    ) -> tuple[list[TelemetryReading], bool]:
        """板面读数优先于库读视图；按 (station_id, metric, observed_at) 去重后再验窗口。

        返回 (窗口内读数, 库读是否触顶)。库读多取一条判断触顶：窗口内超过上限时存储层
        只回最新的一批，更早的读数已被丢掉——这件事必须由 `truncated` 说出去，不能静默。
        """
        merged: dict[tuple[str, str, str], TelemetryReading] = {}
        for reading in readings:
            merged.setdefault((reading.station_id, reading.metric, reading.observed_at), reading)
        store_capped = False
        if self._store is not None:
            stored = self._store.telemetry.query(
                region_code=region,
                since=now_iso(since),
                until=now_iso(until),
                limit=_STORE_READ_LIMIT + 1,
            )
            store_capped = len(stored) > _STORE_READ_LIMIT
            if store_capped:
                stored = stored[-_STORE_READ_LIMIT:]
            for reading in stored:
                merged.setdefault((reading.station_id, reading.metric, reading.observed_at), reading)
        in_window: list[TelemetryReading] = []
        for reading in merged.values():
            try:
                observed = parse_iso(reading.observed_at)
            except (TypeError, ValueError):
                continue
            if since <= observed <= until:
                in_window.append(reading)
        return in_window, store_capped

    @staticmethod
    def _is_finite(value: float) -> bool:
        return value == value and value not in (float("inf"), float("-inf"))

    @staticmethod
    def _aggregate(metric: str, declaration: Derivation, group: list[TelemetryReading]) -> tuple[FactorObservation | None, int]:
        """按声明换算并聚合窗口内的读数。

        换算系数从声明里取（不再由 metric 名硬编码），换算后再判因子口径的值域——
        **逐条剔**：越界的那一条丢掉，同指标的合法读数照常成因子；整批都越界才拒绝
        （返回 None），由调用方写明原因。

        逐条剔而不是整批拒，理由是方向：越界读数通常是传感器坏了（不是真的极端），
        丢掉它让因子回到真实量级；整批拒会把整段窗口的量一并丢掉，缺口更大。
        """
        scale = declaration.scale
        kept: list[tuple[TelemetryReading, float]] = []
        for reading in group:
            value = reading.value * scale
            if not declaration.physical_bound or _within(value, declaration.valid_range):
                kept.append((reading, value))
        if not kept:
            return None, len(group)
        kept_values = [value for _, value in kept]
        latest = max((reading for reading, _ in kept), key=lambda reading: reading.observed_at)
        observation = FactorObservation(
            factor=declaration.factor,
            unit=declaration.unit,
            samples=len(kept),
            min_value=min(kept_values),
            max_value=max(kept_values),
            mean_value=sum(kept_values) / len(kept_values),
            observed_at=latest.observed_at,
            station_ids=sorted({reading.station_id for reading, _ in kept})[:32],
            quality="ok",
            sources=sorted({reading.source for reading, _ in kept})[:8],
            derivation=declaration.describe(metric),
        )
        return observation, len(group) - len(kept)

    @staticmethod
    def _derive_displacement(group: list[TelemetryReading]) -> FactorObservation | None:
        # 先按站分一次组，再逐站排序：此前是"每个站都把整组重扫一遍"的 O(站×组)。
        by_station: dict[str, list[TelemetryReading]] = {}
        for reading in group:
            by_station.setdefault(reading.station_id, []).append(reading)
        rates: list[float] = []
        samples = 0
        stations: set[str] = set()
        latest = ""
        for station in sorted(by_station):
            ordered = sorted(by_station[station], key=lambda reading: reading.observed_at)
            if len(ordered) < 2:
                continue
            elapsed_h = (parse_iso(ordered[-1].observed_at) - parse_iso(ordered[0].observed_at)).total_seconds() / 3_600
            if elapsed_h <= 0:
                continue
            rates.append((ordered[-1].value - ordered[0].value) / elapsed_h * 24)
            samples += len(ordered)
            stations.add(station)
            latest = max(latest, ordered[-1].observed_at)
        if not rates:
            return None
        declaration = DERIVATIONS["displacement_mm"]
        valid = declaration.valid_range
        rates = [rate for rate in rates if not declaration.physical_bound or _within(rate, valid)]
        if not rates:
            return None
        return FactorObservation(
            factor=declaration.factor,
            unit=declaration.unit,
            samples=samples,
            min_value=min(rates),
            max_value=max(rates),
            mean_value=sum(rates) / len(rates),
            observed_at=latest,
            station_ids=sorted(stations)[:32],
            quality="ok",
            sources=sorted({reading.source for reading in group})[:8],
            derivation="每站 (last-first)/elapsed_h×24，跨站取最大",
        )

    @staticmethod
    def _derive_water_level_ratio(group: list[TelemetryReading], profile: SiteProfile | None) -> FactorObservation | None:
        if profile is None:
            return None
        dam_height = profile.value_of("dam_height_m")
        if isinstance(dam_height, bool) or not isinstance(dam_height, (int, float)) or dam_height <= 0:
            return None
        declaration = DERIVATIONS["lake_level_m"]
        valid = declaration.valid_range
        ratios = [(reading, reading.value / float(dam_height)) for reading in group]
        # 水位比 >1 是"漫顶"（溃决）——最危险的状态，不是坏读数，不剔。
        ratios = [(reading, ratio) for reading, ratio in ratios if not declaration.physical_bound or _within(ratio, valid)]
        if not ratios:
            return None
        values = [ratio for _, ratio in ratios]
        latest = max((reading for reading, _ in ratios), key=lambda reading: reading.observed_at)
        return FactorObservation(
            factor=declaration.factor,
            unit=declaration.unit,
            samples=len(ratios),
            min_value=min(values),
            max_value=max(values),
            mean_value=sum(values) / len(values),
            observed_at=latest.observed_at,
            station_ids=sorted({reading.station_id for reading, _ in ratios})[:32],
            quality="ok",
            sources=sorted({reading.source for reading, _ in ratios})[:8],
            derivation="max(lake_level_m)/site.dam_height_m",
        )

    @staticmethod
    def _site_observations(profile: SiteProfile | None, generated_at: str) -> list[FactorObservation]:
        if profile is None:
            return []
        rows: list[FactorObservation] = []
        for attribute in profile.attributes:
            numeric = None if isinstance(attribute.value, str) else float(attribute.value)
            rows.append(
                FactorObservation(
                    factor=attribute.key,
                    unit=attribute.unit,
                    samples=1,
                    max_value=numeric,
                    class_value=None if numeric is not None else str(attribute.value),
                    observed_at=generated_at,
                    quality="site",
                    sources=[f"site_profile:{attribute.source}"][:8],
                    derivation="site_profile",
                )
            )
        return rows

    # ---------- 出处与上限 ----------

    @staticmethod
    def _provenance(readings: list[TelemetryReading], profile: SiteProfile | None, intake: str) -> DataProvenance:
        telemetry_sources = sorted({reading.source for reading in readings})
        site_sources = sorted({f"site_profile:{attribute.source}" for attribute in profile.attributes}) if profile else []
        sources = [f"platform.telemetry:{source}" for source in telemetry_sources] + site_sources
        simulated = any(reading.source in _SIMULATED_SOURCES for reading in readings)
        is_mock = simulated or (profile.provenance.is_mock if profile else False)
        kind: DataKind

        if telemetry_sources:
            if simulated and any(source not in _SIMULATED_SOURCES for source in telemetry_sources):
                kind = "mixed"
                is_mock = True
            elif simulated:
                kind = "mock"
            else:
                kind = "observed"
        elif intake == "report":
            kind = "reported"
        elif profile is not None:
            kind = profile.provenance.data_kind
        else:
            kind = "unknown"

        return DataProvenance(
            data_kind=kind,
            sources=sources[:16],
            method="telemetry_rule",
            method_version="assess_evidence.v1",
            is_mock=is_mock,
            disclaimer=_MOCK_DISCLAIMER if is_mock else "",
        )

    def _payload_size(self, evidence: AssessEvidence) -> int:
        return len(json.dumps(evidence.model_dump(mode="json"), ensure_ascii=False).encode("utf-8"))

    def _enforce_payload_bound(self, evidence: AssessEvidence) -> AssessEvidence:
        if self._payload_size(evidence) <= self._max_payload_bytes:
            return evidence
        notes = list(evidence.notes)
        observations = list(evidence.observations)
        # 二分找"还能装下的最长前缀"：此前是每弹一条就把整份证据包重新 json.dumps 一遍
        # （O(裁剪数 × 包体)）。观测被移除时包体单调减小，所以二分与逐条弹的**落点数相同**，
        # 只是序列化次数从 O(k) 降到 O(log k)。
        low, high = 0, len(observations)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = evidence.model_copy(update={"observations": observations[:middle]})
            if self._payload_size(candidate) <= self._max_payload_bytes:
                low = middle
            else:
                high = middle - 1
        kept = observations[:low]
        notes.append(f"证据包超过 {self._max_payload_bytes} 字节，已裁剪到 {len(kept)} 条观测")
        return evidence.model_copy(update={"observations": kept, "notes": notes[:16], "truncated": True})


__all__ = ["DERIVATIONS", "UNMAPPED_REASONS", "AssessEvidenceBuilder", "Derivation"]
