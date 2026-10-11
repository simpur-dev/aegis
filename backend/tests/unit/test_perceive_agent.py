"""perceive 内环测试（§3.1）：越限复用 detector + 时空聚集弱信号增强 + 输出 TriggerHit 兼容。

用测试专用阈值 detector（不是平台业务阈值）把站点判为异常，聚焦验证真正的新价值——
同区同灾种多站点同步 → 聚合成一条 boost 后的 trigger_hit；且每条命中都能被 `TriggerHit.model_validate`
收（对齐链路 handle_agent_message 的消费口径）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aegis.agents.perceive import PerceiveAgent, StationAnomaly
from aegis.domain.enums import AgentType
from aegis.domain.messages import TriggerHit, make_request


def _iso(offset_seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset_seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _reading(station: str, value: float, region: str = "540121", metric: str = "rainfall_10min", *, t: int = 0) -> dict:
    return {"station_id": station, "metric": metric, "value": value, "unit": "mm", "region_code": region, "observed_at": _iso(t)}


class ThresholdDetector:
    """测试专用：rainfall_10min ≥ 阈值 → 泥石流异常（不代表平台业务阈值）。"""

    def __init__(self, threshold: float = 30.0, base_score: float = 0.6) -> None:
        self.t = threshold
        self.base = base_score

    def flag(self, reading: dict) -> StationAnomaly | None:
        if reading.get("metric") == "rainfall_10min" and float(reading["value"]) >= self.t:
            return StationAnomaly(
                station_id=reading["station_id"],
                region_code=reading["region_code"],
                hazard_type="debris_flow",
                value=float(reading["value"]),
                unit=reading.get("unit", "mm"),
                observed_at=reading["observed_at"],
                base_score=self.base,
            )
        return None


def agent(**kw) -> PerceiveAgent:
    return PerceiveAgent(
        transport=None,
        agent_type=AgentType.PERCEIVE,
        agent_id="perceive.real01",
        capabilities=["anomaly_detect"],
        detector=ThresholdDetector(),
        **kw,
    )  # type: ignore[arg-type]


class TestDetection:
    def test_no_detector_returns_empty(self) -> None:
        a = PerceiveAgent(
            transport=None, agent_type=AgentType.PERCEIVE, agent_id="perceive.x", capabilities=["anomaly_detect"], detector=None
        )  # type: ignore[arg-type]
        assert a.detect([_reading("RG-1", 50)]) == []

    def test_single_station_passthrough(self) -> None:
        a = agent()
        hits = a.detect([_reading("RG-1", 50)])
        assert len(hits) == 1
        assert hits[0]["rule_id"] == "R-ANOMALY-RG-1"
        assert hits[0]["score"] == 0.6
        assert TriggerHit.model_validate(hits[0])  # 命中可被链路收

    def test_synchronous_stations_cluster_and_boost(self) -> None:
        a = agent()
        hits = a.detect([_reading("RG-1", 50), _reading("RG-2", 55), _reading("RG-3", 60)])
        assert len(hits) == 1, "同区同灾种同步异常应聚合成一条"
        hit = hits[0]
        assert hit["rule_id"] == "R-SPATIOTEMPORAL-540121-debris_flow"
        # base 0.6 + 增益 0.1×(3-1) = 0.8
        assert hit["score"] == 0.8
        assert len(hit["evidence_refs"]) == 3
        for s in ("RG-1", "RG-2", "RG-3"):
            assert any(s in e for e in hit["evidence_refs"])
        assert TriggerHit.model_validate(hit)

    def test_out_of_window_not_clustered(self) -> None:
        a = agent(window_seconds=300)
        hits = a.detect([_reading("RG-1", 50, t=0), _reading("RG-2", 55, t=3600)])
        assert len(hits) == 2, "时间窗不同步→不聚集，各自成一条"
        assert {h["rule_id"] for h in hits} == {"R-ANOMALY-RG-1", "R-ANOMALY-RG-2"}

    def test_different_regions_separate_groups(self) -> None:
        a = agent()
        hits = a.detect([_reading("RG-1", 50, region="540121"), _reading("RG-2", 55, region="540200")])
        assert len(hits) == 2  # 各自单站，不跨区聚集

    def test_score_clamped_to_one(self) -> None:
        a = agent(gain_per_station=0.5)
        hits = a.detect([_reading(f"RG-{i}", 50) for i in range(4)])  # base 0.6 + 0.5*3 = 2.1 → 截到 1.0
        assert hits[0]["score"] == 1.0


class TestHandle:
    async def test_handle_returns_trigger_hits_payload(self) -> None:
        a = agent()
        req = make_request(
            source="platform.ingest",
            target="perceive.real01",
            action="perceive.anomaly",
            payload={"readings": [_reading("RG-1", 50), _reading("RG-2", 55)]},
            trace_id="trc_" + "3" * 16,
            reply_to="reply.gw0000000e.inbox",
            deadline_ms=3000,
        )
        reply = await a.handle(req)
        assert reply.payload["anomaly_count"] == 1
        assert TriggerHit.model_validate(reply.payload["trigger_hits"][0])
