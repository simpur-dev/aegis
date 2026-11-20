"""证据落库导出的端到端证据：开了采集的容器，真跑链路，落出的行能喂校准脚本。

链路事实（不是构造调用）：研判段真构建过证据包 → 导出行存在，且
`case_id` 就是链路 `event_id`、观测里有真实的 `rainfall_mm_h` 派生值。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from tests.e2e.test_chain_e2e import group, surge_readings

from aegis.config import Settings
from aegis.container import create_container


@pytest.mark.asyncio
async def test_研判证据随链路落盘且能喂校准(tmp_path: Path) -> None:
    from scripts.calibrate_factor_weights import _read_observations

    from aegis.agents.mock import start_mock_agents

    settings = Settings(
        env="test",
        bus_backend="memory",
        store_backend="memory",
        simulator_enabled=False,
        evidence_export_dir=str(tmp_path),
    )
    container = create_container(settings, with_simulator=False)
    await container.start()
    try:
        await start_mock_agents(container.transport, latency_ms=5.0, heartbeat_interval=0.05)
        await asyncio.sleep(0.05)
        await container.transport.idle()

        results = await container.chain.process_many(group(surge_readings()))
        assert any(r.acted for r in results), "激增场景必须走到研判"
        await container.transport.idle()
    finally:
        await container.shutdown()

    files = list(tmp_path.glob("evidence_export-*.jsonl"))
    assert files, "开了采集的容器研判后必须落盘"
    case_ids: set[str] = set()
    observations = {}
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            case_ids.add(record["case_id"])
            observations.update(_read_observations(path))
    assert case_ids, "导出行必须带 case_id（=链路 event_id，真值标注的配对键）"
    assert {r.event_id for r in results if r.verdict is not None} & case_ids, "case_id 必须对得上链路事实"
    # 激增场景的雨强是真实派生值：观测里应有 rainfall_mm_h（校准要喂的正是它）
    assert any("rainfall_mm_h" in values for values in observations.values()), "校准需要的雨强观测必须在导出里"
