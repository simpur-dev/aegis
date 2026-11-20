"""研判证据包落库导出的离线用例：与校准脚本是一对契约。

要钉住的：
- 导出行的形状就是 `calibrate_factor_weights._read_observations` 直读的形状
  （case_id / observations[].factor / observations[].mean_value）；
- **默认关**：不填 `AEGIS_EVIDENCE_EXPORT_DIR` 就没有导出器，主链零开销；
- 写盘失败只计数不抛（校准是旁路，预警主链不为之停）；
- 同一 event 重复研判重复落行（append-only 事实），不做去重合并。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.calibrate_factor_weights import _read_observations

from aegis.config import Settings
from aegis.container import create_container
from aegis.domain.assess import AssessEvidence, FactorObservation
from aegis.domain.messages import now_iso
from aegis.pipeline.evidence_export import EvidenceExporter


def _evidence(region: str = "540121", rain: float = 12.5) -> AssessEvidence:
    return AssessEvidence(
        region_code=region,
        window="60m",
        generated_at=now_iso(),
        observations=[
            FactorObservation(
                factor="rainfall_mm_h",
                unit="mm/h",
                samples=3,
                mean_value=rain,
                observed_at=now_iso(),
                derivation="窗口内 max(rain_10min)×6（10 分钟→小时）",
            )
        ],
        unmapped_metrics=[],
    )


class TestExporter:
    @pytest.mark.asyncio
    async def test_导出行形状与校准脚本直读对得上(self, tmp_path: Path) -> None:
        """一对契约：校准脚本的 `--observations` 就是这份导出。"""
        exporter = EvidenceExporter(tmp_path)
        evidence = _evidence(rain=12.5)
        ok = await exporter.export(
            case_id="evt-0001",
            trace_id="trc_" + "a" * 16,
            region_code="540121",
            evidence=evidence,
            candidate_hazards=["landslide", "debris_flow"],
            window="60m",
        )
        assert ok is True and exporter.status()["written"] == 1
        files = list(tmp_path.glob("evidence_export-*.jsonl"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert text.endswith("\n")
        record = json.loads(text.splitlines()[0])
        assert record["schema_version"] == "1.0.0"
        assert record["case_id"] == "evt-0001"
        assert record["candidate_hazards"] == ["landslide", "debris_flow"]
        # 校准侧按 case_id 直读出因子观测——这就是"证据包落库导出"存在的意义
        observations = _read_observations(files[0])
        assert observations["evt-0001"]["rainfall_mm_h"] == pytest.approx(12.5)

    @pytest.mark.asyncio
    async def test_写盘失败只计数不抛(self, tmp_path: Path) -> None:
        # 先造一个同名**文件**占住目录位，mkdir 会失败
        (tmp_path / "file_blocker").write_text("occupied", encoding="utf-8")
        exporter = EvidenceExporter(tmp_path / "file_blocker")
        ok = await exporter.export(
            case_id="evt-0002",
            trace_id="trc_" + "b" * 16,
            region_code="540121",
            evidence=_evidence(),
            candidate_hazards=[],
            window="60m",
        )
        assert ok is False
        assert exporter.status()["failures"] == 1 and exporter.status()["written"] == 0

    @pytest.mark.asyncio
    async def test_重复研判重复落行(self, tmp_path: Path) -> None:
        """append-only 事实：同一 event 重复研判重复落行，去重合并是校准侧的事。"""
        exporter = EvidenceExporter(tmp_path)
        for _ in range(2):
            await exporter.export(
                case_id="evt-0003",
                trace_id="trc_" + "c" * 16,
                region_code="540121",
                evidence=_evidence(),
                candidate_hazards=["landslide"],
                window="60m",
            )
        path = next(tmp_path.glob("evidence_export-*.jsonl"))
        assert len(path.read_text(encoding="utf-8").splitlines()) == 2


class TestWiring:
    def test_默认不装配_主链零开销(self) -> None:
        settings = Settings(env="test", bus_backend="memory", store_backend="memory", simulator_enabled=False)
        ctn = create_container(settings, with_simulator=False)
        assert ctn.chain.stages._evidence_exporter is None, "不填导出目录就没有这条旁路"

    def test_填了目录就装配(self, tmp_path: Path) -> None:
        settings = Settings(
            env="test",
            bus_backend="memory",
            store_backend="memory",
            simulator_enabled=False,
            evidence_export_dir=str(tmp_path),
        )
        ctn = create_container(settings, with_simulator=False)
        assert ctn.chain.stages._evidence_exporter is not None
        assert ctn.chain.stages._evidence_exporter.directory == tmp_path
