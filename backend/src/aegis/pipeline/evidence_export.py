"""研判证据包落库导出：`assess.v1` 的证据从请求载荷里落到运行态 JSONL。

**它解决的是哪一个断点**：`scripts/calibrate_factor_weights.py`（因子权重校准骨架）
需要**逐案例的因子观测**才能把 `not_measured` 变成实测——而证据包此前只活在
`AssessRequest.evidence` 里，请求发完就没了。本模块把"证据构建"那一刻的观测落成
逐行 JSONL，校准脚本 `--observations` 直读。

**口径（对着校准脚本写的，两处是一对契约）**：
- 每行 = 一次证据构建：`case_id`（= 链路 `event_id`，真值标注表的 `case_id` 就是它）、
  `region_code`、`candidate_hazards`、`window`、`observations`（`FactorObservation` 原样）、
  `unmapped_metrics`、`generated_at`、`trace_id`；
- 同一 event 重复研判会重复落行（append-only 事实），校准侧按 case_id 取最新的即可
  ——不在导出侧做去重合并（导出是观测记录，不是结论表）；
- **只 append、不追改**：这是校准的原料账本，改历史等于伪造观测。

**默认关**：填 `AEGIS_EVIDENCE_EXPORT_DIR` 才装配（校准数据采集是显式动作，
不是常驻开销）。写盘失败不炸链路——校准是旁路，预警主链不为它停；失败计数
如实报，不静默。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aegis.domain.assess import AssessEvidence

log = logging.getLogger("aegis.pipeline.evidence_export")

SCHEMA_VERSION = "1.0.0"


class EvidenceExporter:
    """把研判证据包 append 进运行态 JSONL（按天分文件）。"""

    def __init__(self, directory: Path | str) -> None:
        self._directory = Path(directory)
        self.written = 0
        self.failures = 0

    @property
    def directory(self) -> Path:
        return self._directory

    def status(self) -> dict[str, Any]:
        return {"written": self.written, "failures": self.failures, "dir": self._directory.name}

    async def export(
        self,
        *,
        case_id: str,
        trace_id: str,
        region_code: str,
        evidence: AssessEvidence,
        candidate_hazards: list[str],
        window: str,
    ) -> bool:
        """一行证据事实。返回是否落盘成功；失败只计数并告警日志，不抛——主链不为之停。

        写盘走 `asyncio.to_thread`：研判段跑在事件循环上，慢盘/网络盘不能把每轮 assess 卡住。
        """
        record = {
            "schema_version": SCHEMA_VERSION,
            "case_id": case_id,
            "trace_id": trace_id,
            "region_code": region_code,
            "candidate_hazards": candidate_hazards,
            "window": window,
            "generated_at": evidence.generated_at,
            "observations": [item.model_dump(mode="json") for item in evidence.observations],
            "unmapped_metrics": [item.model_dump(mode="json") for item in evidence.unmapped_metrics],
        }
        try:
            await asyncio.to_thread(self._append, record)
            self.written += 1
            return True
        except Exception as exc:
            self.failures += 1
            log.warning("证据导出失败（第 %d 次）：%s: %s", self.failures, type(exc).__name__, exc)
            return False

    def _append(self, record: dict[str, Any]) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        name = f"evidence_export-{datetime.now(UTC).strftime('%Y%m%d')}.jsonl"
        path = self._directory / name
        existed = path.exists()
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        if not existed:
            log.info("证据导出落盘：%s（校准观测采集已启用）", path)


__all__ = ["SCHEMA_VERSION", "EvidenceExporter"]
