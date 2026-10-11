"""assess 内环测试（§3.2）：结构化定级 / 双盲一致上调 / 分歧仲裁 / 低置信护栏 / D10 溯源。

直接驱动 handle()（壳到响应的集成由 test_agentkit 覆盖），用假网关按脚本回放 chat_structured，
断言到"选了什么 task、调了几次、结论怎么合成"的要素级事实。
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.agents.assess import AssessAgent, AssessVerdict
from aegis.domain.enums import AgentType
from aegis.domain.messages import make_request

TRACE = "trc_" + "7" * 16
REQUEST_PAYLOAD = {"region_code": "540121", "hits": [{"rule_id": "R-DEBRIS-RAIN-1", "hazard_type": "debris_flow", "score": 0.9}]}


class FakeGateway:
    def __init__(self, *verdicts: Any) -> None:
        self._q = list(verdicts)
        self.calls: list[dict[str, Any]] = []

    async def chat_structured(self, messages, *, schema, task="default", agent_id=None, trace_id=None, **kw):
        self.calls.append({"task": task, "messages": messages})
        v = self._q.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    def model_info_for_task(self, task):
        return {
            "model": {"assess": "qwen-plus", "arbitration": "qwen-max"}.get(task, "qwen-plus"),
            "fallback_model": "",
            "task": task,
            "prompt_version": "1.2.0",
        }


def agent(*verdicts: Any, **kw: Any) -> tuple[AssessAgent, FakeGateway]:
    gw = FakeGateway(*verdicts)
    a = AssessAgent(
        transport=_NullTransport(), agent_type=AgentType.ASSESS, agent_id="assess.real01", capabilities=["risk_assess"], llm=gw, **kw
    )
    return a, gw


class _NullTransport:
    name = "null"

    async def publish(self, *a: object, **k: object) -> None: ...


def _request() -> Any:
    return make_request(
        source="platform.gateway",
        target="assess.real01",
        action="assess.hazard",
        payload=REQUEST_PAYLOAD,
        trace_id=TRACE,
        reply_to="reply.gw0000000a.inbox",
        deadline_ms=5000,
    )


def verdict(level: int, *, confidence: float = 0.8, rationale: str = "强降雨叠加泥位抬升") -> AssessVerdict:
    return AssessVerdict(
        risk_level=level, hazard_type="debris_flow", confidence=confidence, rationale=rationale, evidence_refs=["R-DEBRIS-RAIN-1"]
    )


class TestSingleAssessment:
    async def test_yellow_no_double_blind_and_carries_model_info(self) -> None:
        a, gw = agent(verdict(3))
        reply = await a.handle(_request())
        assert len(gw.calls) == 1, "非红橙级不应触发双盲（§8 成本对策）"
        assert reply.payload["risk_level"] == 3
        assert reply.payload["assessed_by"] == "assess.real01"
        assert reply.payload["evidence_refs"] == ["R-DEBRIS-RAIN-1"]
        assert reply.disagreement is None
        assert reply.model_info is not None and reply.model_info.model == "qwen-plus"
        assert reply.model_info.prompt_version == "1.2.0"

    async def test_low_confidence_flags_human_review(self) -> None:
        a, _ = agent(verdict(4, confidence=0.4))
        reply = await a.handle(_request())
        assert reply.payload["human_review_suggested"] is True
        assert "建议人工核签" in reply.payload["rationale"]

    async def test_missing_llm_raises_for_platform_degrade(self) -> None:
        a = AssessAgent(
            transport=_NullTransport(), agent_type=AgentType.ASSESS, agent_id="assess.nollm", capabilities=["risk_assess"], llm=None
        )
        with pytest.raises(RuntimeError):
            await a.handle(_request())


class TestDoubleBlind:
    async def test_agreement_boosts_confidence_and_no_disagreement(self) -> None:
        a, gw = agent(verdict(2, confidence=0.8), verdict(2, confidence=0.8))
        reply = await a.handle(_request())
        assert [c["task"] for c in gw.calls] == ["assess", "arbitration"]
        assert reply.disagreement is False
        assert round(reply.payload["confidence"], 3) == pytest.approx(0.9)

    async def test_divergence_triggers_arbitration(self) -> None:
        a, gw = agent(verdict(1, rationale="初判红"), verdict(3, rationale="次判黄"), verdict(2, rationale="仲裁橙"))
        reply = await a.handle(_request())
        assert [c["task"] for c in gw.calls] == ["assess", "arbitration", "arbitration"]
        assert reply.disagreement is True
        assert reply.payload["risk_level"] == 2
        assert reply.payload["rationale"] == "仲裁橙"
        # 仲裁轮的上下文里并入了两份分歧 rationale（可解释、可审计）。
        assert "分歧" in gw.calls[2]["messages"][1]["content"]

    async def test_double_blind_can_be_disabled(self) -> None:
        a, gw = agent(verdict(1), double_blind=False)
        reply = await a.handle(_request())
        assert len(gw.calls) == 1
        assert reply.disagreement is None
        assert reply.payload["risk_level"] == 1


class TestContextInjection:
    async def test_corroborator_invoked_and_context_in_prompt(self) -> None:
        seen: dict[str, Any] = {}

        class FakeCtx:
            async def corroborate(self, *, region_code, hazard_hint, hits):
                seen["region"] = region_code
                seen["hazard_hint"] = hazard_hint
                return {"retrieval": ["历史同域泥石流案例 3 起"], "graph": ["降雨→泥位→物源链"]}

        a, gw = agent(verdict(3))
        a.context = FakeCtx()
        await a.handle(_request())
        assert seen["region"] == "540121"
        assert seen["hazard_hint"] == "debris_flow"
        user = gw.calls[0]["messages"][1]["content"]
        assert "retrieval 佐证" in user and "graph 佐证" in user
