"""低置信护栏的读者（§3.5 剩下的那一半）：载荷 → 台账一列事实 → 一张待办清单。

修前 `human_review_suggested` 只活在 `rationale` 的一句文本里：文本不能查、不能数、不能变成待办，
所以护栏报了没人接。这里钉三件事：① 智能体那一格真的被链路读进来并随结论入台账；
② 清单在**内存读视图与库侧两种行形状**下都能捞出同一条（`ChainResult` 上没有 `risk` 属性，
`risk` 是 `as_dict()` 现算的——按 dict 的读法写就会永远返回空清单，而那看起来像"没人建议核签"）；
③ "签没签过"只认真值标注那张真表，没接标注面时那一格是 null 而不是 false。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import PlatformContainer, create_container
from aegis.domain.chain import ChainResult, StageResult
from aegis.domain.enums import HazardType, RiskLevel
from aegis.domain.verdict import RiskVerdict
from aegis.pipeline.stages import HazardStages
from aegis.services.review_queue import case_ids, pending_items

REGION = "540121"
PATH = "/api/v1/review/pending"


def _verdict(**kw: Any) -> RiskVerdict:
    base: dict[str, Any] = {
        "hazard_type": HazardType.DEBRIS_FLOW,
        "region_code": REGION,
        "risk_level": RiskLevel.ORANGE,
        "confidence": 0.44,
        "rationale": "沟道泥位抬升与降雨叠加，置信度偏低（建议人工核签：置信度 0.44）",
        "assessed_by": "assess.agent",
    }
    base.update(kw)
    return RiskVerdict(**base)


def _chain(trace: str, verdict: RiskVerdict | None) -> ChainResult:
    return ChainResult(
        trace_id=trace,
        event_id=f"evt_{trace[-4:]}0000",
        stages=[StageResult("assess", "agent", True, 12.0, "等级 3")],
        verdict=verdict,
    )


class Test载荷进台账:
    def test_智能体那一格被读进结论并随载荷入账(self) -> None:
        payload = {"risk_level": 3, "hazard_type": "debris_flow", "confidence": 0.44, "human_review_suggested": True}
        verdict = HazardStages._parse_agent_assessment(payload, REGION, [])
        assert verdict is not None and verdict.human_review_suggested is True
        assert verdict.as_payload()["human_review_suggested"] is True, "不落进 risk 载荷就等于没进台账"

    def test_不带这一格的老载荷就是未建议(self) -> None:
        payload = {"risk_level": 3, "hazard_type": "debris_flow", "confidence": 0.9}
        verdict = HazardStages._parse_agent_assessment(payload, REGION, [])
        assert verdict is not None and verdict.human_review_suggested is False


class Test摊平两种行形状:
    def test_内存对象与库侧dict捞出的条数一致(self) -> None:
        verdict = _verdict(human_review_suggested=True)
        as_objects = [_chain("trc_" + "1" * 16, verdict), _chain("trc_" + "2" * 16, _verdict(human_review_suggested=False))]
        as_dicts = [row.as_dict() for row in as_objects]
        from_objects = pending_items(as_objects)
        from_dicts = pending_items(as_dicts)
        assert len(from_objects) == 1, "ChainResult 上没有 risk 属性：按 dict 读法写就永远空"
        assert [item["event_id"] for item in from_objects] == [item["event_id"] for item in from_dicts]
        assert from_objects[0]["confidence"] == 0.44 and from_objects[0]["assessed_by"] == "assess.agent"

    def test_清单没接标注面时那一格是null不是false(self) -> None:
        items = pending_items([_chain("trc_" + "3" * 16, _verdict(human_review_suggested=True))])
        assert items[0]["already_reviewed"] is None
        signed = pending_items([_chain("trc_" + "3" * 16, _verdict(human_review_suggested=True))], labelled=set())
        assert signed[0]["already_reviewed"] is False, "给了集合就该判真假，不该继续装未知"

    def test_区域过滤器只留本区(self) -> None:
        rows = [_chain("trc_" + "4" * 16, _verdict(human_review_suggested=True))]
        assert pending_items(rows, region_code="540100") == []
        assert len(pending_items(rows, region_code=REGION)) == 1

    def test_case_ids取自event_id(self) -> None:
        items = pending_items([_chain("trc_" + "5" * 16, _verdict(human_review_suggested=True))])
        assert case_ids(items) == [items[0]["event_id"]], "与 label_from_review 的 case_id 必须同一口径"


@pytest.fixture()
def settings() -> Settings:
    return Settings(env="test", bus_backend="memory", store_backend="memory", simulator_enabled=False, delivery_mode="mock", llm_api_key="")


@asynccontextmanager
async def _client(container: PlatformContainer) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(container.settings, container=container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        yield client


class Test清单端点:
    async def test_护栏报出的那条在HTTP面上看得见(self, settings: Settings) -> None:
        container = create_container(settings, with_simulator=False)
        await container.start()
        try:
            await container.store.record_chain(_chain("trc_" + "6" * 16, _verdict(human_review_suggested=True)))
            await container.store.record_chain(_chain("trc_" + "7" * 16, _verdict(human_review_suggested=False)))
            async with _client(container) as c:
                body = (await c.get(PATH)).json()
            assert body["count"] == 1 and body["ledger_rows"] == 2, body
            assert body["items"][0]["trace_id"] == "trc_" + "6" * 16
            assert body["items"][0]["risk_level"] == int(RiskLevel.ORANGE) and body["items"][0]["rationale"]
            # 内存形态没有标注面：说清"不知道签没签"，而不是回一个 false 让人重复签
            assert body["label_state"] == "unavailable" and "标注" in body["label_state_reason"]
            assert body["items"][0]["already_reviewed"] is None
        finally:
            await container.shutdown()

    async def test_没有台账样本时清单是空的且说清看了多少行(self, settings: Settings) -> None:
        container = create_container(settings, with_simulator=False)
        await container.start()
        try:
            async with _client(container) as c:
                body = (await c.get(PATH)).json()
            assert body["count"] == 0 and body["ledger_rows"] == 0 and body["items"] == []
        finally:
            await container.shutdown()

    async def test_坏区域码在进处理函数之前就被拒(self, settings: Settings) -> None:
        container = create_container(settings, with_simulator=False)
        await container.start()
        try:
            async with _client(container) as c:
                assert (await c.get(PATH, params={"region_code": "小写又不是六位"})).status_code == 422
        finally:
            await container.shutdown()

    async def test_这条新端点在服务令牌闸里(self) -> None:
        cfg = Settings(
            env="test", bus_backend="memory", store_backend="memory", simulator_enabled=False, delivery_mode="mock", service_token="s3cr3t"
        )
        container = create_container(cfg, with_simulator=False)
        await container.start()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(cfg, container=container)), base_url="http://testserver"
            ) as c:
                assert (await c.get(PATH)).status_code == 401
        finally:
            await container.shutdown()
