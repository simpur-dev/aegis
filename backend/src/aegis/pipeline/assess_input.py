"""研判证据构建：把窗口内的遥测与区域画像折成 `AssessEvidence`（assess.v1 的输入侧）。

**派生表是唯一合法口径**。外部因子引擎要的是物理因子（`rainfall_mm_h` 等），而平台遥测是
站点原始量（`rain_10min` 等）；两者之间的换算只能发生在这里，且每条派生都写明公式。
没有把握的换算一律不映射：`new_snow_cm` 不知道区间是不是 24h，就不能冒充 `new_snow_24h_cm`；
浊度 NTU 不是序数等级，就不能冒充 `seepage_level`。进 `unmapped_metrics` 并写原因，
比造一个看着合理的数诚实——伪装的因子会一路走到预警里。

证据包自限（条数与字节数）：网关与总线的信封有上限，超限必须显式置 `truncated=True` 并留 note，
不做静默截断。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from aegis.domain.assess import AssessEvidence, FactorObservation, UnmappedMetric
from aegis.domain.messages import TelemetryReading, TriggerHit, now_iso, parse_iso
from aegis.domain.provenance import DataKind, DataProvenance
from aegis.domain.site import SiteProfile
from aegis.storage.store import StoreProtocol

log = logging.getLogger("aegis.pipeline.assess_input")

#: 遥测 metric -> (因子, 单位, 派生说明)。顺序即证据包里派生因子的出场顺序。
DERIVATIONS: dict[str, tuple[str, str, str]] = {
    "rain_10min": ("rainfall_mm_h", "mm/h", "窗口内 max(rain_10min)×6（10 分钟→小时）"),
    "displacement_mm": ("displacement_mm_day", "mm/day", "每站 (last-first)/elapsed_h×24，跨站取最大"),
    "crack_aperture_mm": ("crack_width_mm", "mm", "窗口内 max(crack_aperture_mm)"),
    "air_temperature_c": ("air_temp_c", "°C", "窗口内 max(air_temperature_c)"),
    "lake_level_m": ("water_level_ratio", "1", "max(lake_level_m)/site.dam_height_m"),
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

        dropped_quality = sum(1 for reading in raw if reading.quality_flag != "ok")
        dropped_region = sum(1 for reading in raw if reading.region_code != region)
        in_scope = [reading for reading in raw if reading.region_code == region and reading.quality_flag == "ok"]
        finite: list[TelemetryReading] = []
        non_finite = 0
        for reading in in_scope:
            if self._is_finite(reading.value):
                finite.append(reading)
            else:
                non_finite += 1

        by_metric: dict[str, list[TelemetryReading]] = {}
        for reading in finite:
            by_metric.setdefault(reading.metric, []).append(reading)

        observations: list[FactorObservation] = []
        unmapped: list[tuple[str, int, str]] = []
        for metric, (factor, unit, derivation) in DERIVATIONS.items():
            group = by_metric.get(metric, [])
            if not group:
                continue
            if metric == "displacement_mm":
                derived = self._derive_displacement(group)
                if derived is None:
                    unmapped.append((metric, len(group), "每站至少需要 2 个 ok 样本且时间递增，否则算不出 mm/day"))
                else:
                    observations.append(derived)
            elif metric == "lake_level_m":
                derived = self._derive_water_level_ratio(group, profile)
                if derived is None:
                    reason = "缺少 site_profile.dam_height_m（或值<=0），无法派生水位/坝高比"
                    unmapped.append((metric, len(group), reason))
                else:
                    observations.append(derived)
            else:
                observations.append(self._aggregate(metric, factor, unit, derivation, group))

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
    def _aggregate(metric: str, factor: str, unit: str, derivation: str, group: list[TelemetryReading]) -> FactorObservation:
        values = [reading.value for reading in group]
        latest = max(group, key=lambda reading: reading.observed_at)
        scaled = metric == "rain_10min"
        scale = 6.0 if scaled else 1.0
        return FactorObservation(
            factor=factor,
            unit=unit,
            samples=len(group),
            min_value=min(values) * scale,
            max_value=max(values) * scale,
            mean_value=sum(values) / len(values) * scale,
            observed_at=latest.observed_at,
            station_ids=sorted({reading.station_id for reading in group})[:32],
            quality="ok",
            sources=sorted({reading.source for reading in group})[:8],
            derivation=derivation,
        )

    @staticmethod
    def _derive_displacement(group: list[TelemetryReading]) -> FactorObservation | None:
        rates: list[float] = []
        samples = 0
        stations: set[str] = set()
        latest = ""
        for station in sorted({reading.station_id for reading in group}):
            ordered = sorted((reading for reading in group if reading.station_id == station), key=lambda reading: reading.observed_at)
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
        return FactorObservation(
            factor="displacement_mm_day",
            unit="mm/day",
            samples=samples,
            min_value=min(rates),
            max_value=max(rates),
            mean_value=sum(rates) / len(rates),
            observed_at=latest,
            station_ids=sorted(stations)[:32],
            quality="ok",
            sources=sorted({reading.source for reading in group})[:8],
            derivation=DERIVATIONS["displacement_mm"][2],
        )

    @staticmethod
    def _derive_water_level_ratio(group: list[TelemetryReading], profile: SiteProfile | None) -> FactorObservation | None:
        if profile is None:
            return None
        dam_height = profile.value_of("dam_height_m")
        if isinstance(dam_height, bool) or not isinstance(dam_height, (int, float)) or dam_height <= 0:
            return None
        latest = max(group, key=lambda reading: reading.observed_at)
        ratio = max(reading.value for reading in group) / float(dam_height)
        return FactorObservation(
            factor="water_level_ratio",
            unit="1",
            samples=len(group),
            min_value=min(reading.value for reading in group) / float(dam_height),
            max_value=ratio,
            mean_value=sum(reading.value for reading in group) / len(group) / float(dam_height),
            observed_at=latest.observed_at,
            station_ids=sorted({reading.station_id for reading in group})[:32],
            quality="ok",
            sources=sorted({reading.source for reading in group})[:8],
            derivation=DERIVATIONS["lake_level_m"][2],
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
        while observations and self._payload_size(evidence) > self._max_payload_bytes:
            observations.pop()
            evidence = evidence.model_copy(update={"observations": observations})
        notes.append(f"证据包超过 {self._max_payload_bytes} 字节，已裁剪到 {len(observations)} 条观测")
        return evidence.model_copy(update={"notes": notes[:16], "truncated": True})


__all__ = ["DERIVATIONS", "UNMAPPED_REASONS", "AssessEvidenceBuilder"]
