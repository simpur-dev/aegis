"""execute 段的证据口径（§3.4 + 铁律 7）：智能体只产文案，触达证据只认平台交付腿。

升级前 `_execute` 把智能体 ack 里的 `channel_results` 当送达记录用，`_parse_ack` 甚至在智能体
什么都没报时替每条通道造一条 `status="delivered"` 的回执——于是 ≤20min 触达指标与
`warning_reach_ms` 的分子分母都来自"谁应答了这一跳"。这组用例钉的是改完之后：
自报不采信、缺交付腿时停在"released 而非 delivered"、文案采不采纳只看数字有没有出处。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from aegis.agents.mock import start_mock_agents
from aegis.domain.enums import HazardType, RiskLevel
from aegis.domain.messages import TriggerHit, WarningRecord, now_iso
from aegis.pipeline.result import ChainResult
from aegis.pipeline.stages import HazardStages
from aegis.services.risk_engine import RiskVerdict

REGION = "540121"


def verdict() -> RiskVerdict:
    return RiskVerdict(
        hazard_type=HazardType.DEBRIS_FLOW,
        region_code=REGION,
        risk_level=RiskLevel.RED,
        confidence=0.9,
        rationale="10 分钟降雨 32 毫米，泥位 1.4 米",
        hits=[
            TriggerHit(
                rule_id="R-DEBRIS-RAIN-1",
                hazard_type=HazardType.DEBRIS_FLOW.value,
                region_code=REGION,
                score=0.9,
                evidence_refs=["rain_10min=32.0"],
                observed_at=now_iso(),
            )
        ],
    )


def record() -> WarningRecord:
    return WarningRecord(
        event_id="evt_" + "0" * 12,
        trace_id="trc_" + "0" * 16,
        hazard_type=HazardType.DEBRIS_FLOW.value,
        region_codes=[REGION],
        risk_level=RiskLevel.RED,
        title_zh="泥石流红色预警",
        body_zh="10 分钟降雨 32 毫米，请立即避险",
        audiences=["residents"],
        channels=["sms", "broadcast"],
    )


def result() -> ChainResult:
    return ChainResult(trace_id="trc_" + "0" * 16, event_id="evt_" + "0" * 12)


def adopt(payload: dict[str, Any]) -> tuple[WarningRecord, ChainResult]:
    out = result()
    return HazardStages._adopt_agent_copy(record(), payload, out, verdict=verdict()), out


def test_自报的通道结果只留下一行事实不进交付记录() -> None:
    adopted, res = adopt(
        {
            "warning_id": "wrn_x",
            "channel_results": [{"channel": "sms", "status": "delivered", "audience_count": 9, "provider_msg_id": "execute-selfclaim-1"}],
        }
    )
    assert adopted.deliveries == [], "送达记录只能由平台交付腿写，智能体自报多少条都不算"
    assert adopted.released_at is None, "released_at 是 dispatcher 成功之后才写的时间戳"
    assert any("未采信" in note and "dispatcher 凭证" in note for note in res.degradations), res.degradations


def test_文案数字有出处才采纳() -> None:
    adopted, res = adopt({"title_zh": "泥石流红色预警（1 级）", "body_zh": "10 分钟降雨 32 毫米，泥位已达 1.4 米"})
    assert adopted.body_zh == "10 分钟降雨 32 毫米，泥位已达 1.4 米", "事实池里有的数字不该被拒"
    assert adopted.title_zh.endswith("（1 级）")
    assert not [note for note in res.degradations if "弃用" in note]


def test_文案冒出事实外的数字就整段弃用() -> None:
    """变异点：若闸只查不改（或只记不弃），这里会拿到 88.8 那条而不是平台模板。"""
    adopted, res = adopt({"body_zh": "10 分钟降雨 32 毫米，另有 88.8 毫米未来雨量"})
    assert adopted.body_zh == "10 分钟降雨 32 毫米，请立即避险"
    assert any("88.8" in note and "弃用" in note for note in res.degradations), res.degradations


def test_藏文缺位时不假装翻译过() -> None:
    adopted, _ = adopt({"body_bo": "བོད་ཡིག་ཁྱབ་བསྒྲགས།"})
    assert adopted.body_bo == "བོད་ཡིག་ཁྱབ་བསྒྲགས།"
    kept, _ = adopt({"body_bo": "   "})
    assert kept.body_bo is None


def test_事实池把定级依据与证据引用都算进去() -> None:
    facts = HazardStages._copy_facts(record(), verdict())
    assert "32" in facts and "1.4" in facts and "R-DEBRIS-RAIN-1" in facts and "0.9" in facts


async def _acted(container: Any) -> list[Any]:
    from aegis.connectors.simulator import HazardScenarioSimulator
    from aegis.domain.messages import utc_now

    readings = HazardScenarioSimulator(scenario="surge", seed=99).collect_at(utc_now())
    grouped: dict[str, list[Any]] = {}
    for reading in readings:
        grouped.setdefault(reading.region_code, []).append(reading)
    assert grouped, "surge 场景应产出可按区域分组的读数"
    results = await container.chain.process_many(grouped)
    return [r for r in results if r.acted]


async def test_真链路里回执来自交付腿而不是应答的智能体(container: Any) -> None:
    """mock execute 会自报 provider_msg_id=mock-execute.mock01-sms：它出现在交付记录里就是采信了自报。"""
    agents = await start_mock_agents(container.transport, latency_ms=8.0, heartbeat_interval=0.05)
    try:
        await asyncio.sleep(0.05)
        await container.transport.idle()
        acted = await _acted(container)
        assert acted, "构造的 surge 读数应至少促发一条预警"
        for res in acted:
            modes = {s.name: s.mode for s in res.stages}
            assert modes["execute"] == "agent", "智能体仍在这一跳应答，只是它的自报不再当证据"
            warning = res.warning
            assert warning is not None and warning.deliveries, "平台交付腿必须真发过"
            assert warning.audience_copies == {}, "mock 不产文案，就不该有分受众文案"
            for attempt in warning.deliveries:
                assert attempt.provider_msg_id and "execute." not in attempt.provider_msg_id
                assert attempt.status in ("delivered", "retried", "pending", "failed")
            assert any("未采信" in note for note in res.degradations)
    finally:
        for agent in agents:
            await agent.stop()


def test_受众分支逐条过闸后才写进记录() -> None:
    adopted, res = adopt(
        {
            "body_zh": "短雨 32 毫米，立即避险",
            "audience_copies": {
                "residents": "沿高地路线撤离（短雨 32 毫米）",
                "decision_lead": "预计 88.8 毫米，请启动响应",
                "responder": "",
            },
        }
    )
    assert adopted.audience_copies == {"residents": "沿高地路线撤离（短雨 32 毫米）"}
    assert any("decision_lead" in note and "不入库" in note for note in res.degradations), res.degradations


def test_三分支都合规就都进记录() -> None:
    adopted, _ = adopt(
        {
            "audience_copies": {
                "decision_lead": "依据 32 毫米短雨与泥位 1.4 米，责任面在 540121",
                "responder": "按处置预案时限到位",
                "residents": "立即避险",
            }
        }
    )
    assert set(adopted.audience_copies) == {"decision_lead", "responder", "residents"}
    assert adopted.body_zh == record().body_zh, "只给分支没给正文时，公共正文仍是平台模板"


async def test_交付腿缺位时是released而不是造出来的delivered(container: Any) -> None:
    """dispatcher 不在场：`released_at` 可以有（平台已放行），但一条送达记录都不能有。"""
    agents = await start_mock_agents(container.transport, latency_ms=8.0, heartbeat_interval=0.05)
    container.chain.stages._dispatcher = None
    try:
        await asyncio.sleep(0.05)
        await container.transport.idle()
        acted = await _acted(container)
        assert acted
        for res in acted:
            warning = res.warning
            assert warning is not None
            assert warning.deliveries == [], "过去这里会被 _parse_ack 填满 delivered"
            assert warning.released_at is not None
    finally:
        for agent in agents:
            await agent.stop()


@pytest.mark.parametrize("payload", [{}, {"channel_results": []}, {"channel_results": [{"channel": "sms"}]}])
async def test_智能体什么都不报时也不会补出一条送达(payload: dict[str, Any]) -> None:
    """旧兜底正是"空 ack 就替每条通道造 delivered"；这条用例是那个兜底的墓碑。"""
    adopted, res = adopt(payload)
    assert adopted.deliveries == []
    assert adopted.released_at is None
    # 自报有内容才需要留"未采信"的事实行；空 ack 不该凭空多出一行解释
    assert any("未采信" in note for note in res.degradations) is bool(payload.get("channel_results"))
