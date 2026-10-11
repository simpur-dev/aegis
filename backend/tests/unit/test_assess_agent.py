"""assess 内环测试（§3.2）：结构化定级 / 双盲一致上调 / 分歧仲裁 / 低置信护栏 / D10 溯源。

直接驱动 handle()（壳到响应的集成由 test_agentkit 覆盖），用假网关按脚本回放 chat_structured，
断言到"选了什么 task、调了几次、结论怎么合成"的要素级事实。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from aegis.agents.assess import AssessAgent, AssessVerdict
from aegis.domain.enums import AgentType
from aegis.domain.messages import make_request, now_iso
from aegis.services.memory import InMemoryAgentMemory, MemoryService, new_memory_record_id

TRACE = "trc_" + "7" * 16
REQUEST_PAYLOAD = {"region_code": "540121", "hits": [{"rule_id": "R-DEBRIS-RAIN-1", "hazard_type": "debris_flow", "score": 0.9}]}


class FakeGateway:
    def __init__(self, *verdicts: Any) -> None:
        self._q = list(verdicts)
        self.calls: list[dict[str, Any]] = []

    async def chat_structured(
        self, messages, *, schema, task="default", agent_id=None, trace_id=None, prompt_key=None, prompt_vars=None, **kw
    ):
        self.calls.append({"task": task, "messages": messages, "prompt_key": prompt_key, "prompt_vars": prompt_vars})
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


class SlowGateway(FakeGateway):
    """给每次调用注入真实墙钟耗时：预算闸看的必须是实际花掉的时间，不是配置里的假设值。"""

    def __init__(self, *verdicts: Any, delays: list[float] | None = None) -> None:
        super().__init__(*verdicts)
        self._delays = list(delays or [])

    async def chat_structured(self, messages, **kw: Any) -> Any:
        delay = self._delays.pop(0) if self._delays else 0.0
        if delay:
            await asyncio.sleep(delay)
        return await super().chat_structured(messages, **kw)


def _request(deadline_ms: int = 5000) -> Any:
    return make_request(
        source="platform.gateway",
        target="assess.real01",
        action="assess.hazard",
        payload=REQUEST_PAYLOAD,
        trace_id=TRACE,
        reply_to="reply.gw0000000a.inbox",
        deadline_ms=deadline_ms,
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
        # 正文改由 PromptRegistry 装配：智能体只交 prompt_key + 变量，不再内联提示词（单一真源）。
        assert gw.calls[0]["prompt_key"] == "assess"
        assert gw.calls[0]["messages"] == []
        assert gw.calls[0]["prompt_vars"]["region"] == "540121"
        assert "R-DEBRIS-RAIN-1" in gw.calls[0]["prompt_vars"]["hits"]
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
        # 仲裁轮把两份分歧交给注册表模板的 arbitration 变量（可解释、可审计）。
        assert "分歧" in gw.calls[2]["prompt_vars"]["arbitration"]

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
        context = gw.calls[0]["prompt_vars"]["context"]
        assert "retrieval 佐证" in context and "graph 佐证" in context
        assert "AEGIS_DATA" in context  # 外部佐证文本已进提示注入围栏（§4.8.2）


class TestDoubleBlindDeadline:
    """§3.2：双盲是增强腿非关键路径，deadline 紧则跳过并留痕，不冒超期风险。"""

    async def test_tight_deadline_skips_double_blind_and_traces(self) -> None:
        a, gw = agent(verdict(2))  # 只提供一份结论：若仍跑双盲会因缺第二次输出而炸
        reply = await a.handle(_request(deadline_ms=200))  # 200 < double_blind_min_deadline_ms(800)
        assert len(gw.calls) == 1, "预算紧时不跑第二评估"
        assert reply.payload["double_blind"] == "skipped_deadline"
        assert reply.payload["risk_level"] == 2  # 结论照发（漏报代价高于误报）

    async def test_generous_deadline_runs_double_blind_agreed(self) -> None:
        a, gw = agent(verdict(2, confidence=0.8), verdict(2, confidence=0.8))
        reply = await a.handle(_request(deadline_ms=3000))
        assert reply.payload["double_blind"] == "agreed"
        assert len(gw.calls) == 2

    async def test_non_severe_not_applicable(self) -> None:
        a, gw = agent(verdict(4))
        reply = await a.handle(_request(deadline_ms=3000))
        assert reply.payload["double_blind"] == "not_applicable"
        assert len(gw.calls) == 1


class TestAssessMemory:
    """§3.2 读同区域历史研判回注上下文 + §4.3 把本次结论写回共享命名空间；读写失败只降级。"""

    async def test_prior_assessment_recalled_and_new_verdict_written(self) -> None:
        svc = MemoryService(InMemoryAgentMemory())
        await svc.write_shared(
            kind="assessment",
            agent_id="assess.other",
            record_id=new_memory_record_id(),
            content="上一次 3 级 debris_flow 强降雨",
            region_code="540121",
            valid_at=now_iso(),
        )
        a, gw = agent(verdict(3), memory=svc)
        await a.handle(_request())
        context = gw.calls[0]["prompt_vars"]["context"]
        assert "memory 佐证" in context and "上一次 3 级" in context
        rows = await svc.read_shared(kind="assessment", region_code="540121")
        assert len(rows) == 2, "本次结论应写回，与历史同区研判并存"
        assert "3级/debris_flow" in rows[0].content  # 新在前

    async def test_verdict_recorded_even_without_history(self) -> None:
        svc = MemoryService(InMemoryAgentMemory())
        a, _ = agent(verdict(4), memory=svc)
        await a.handle(_request())
        rows = await svc.read_shared(kind="assessment")
        assert len(rows) == 1
        assert rows[0].agent_id == "assess.real01"

    async def test_memory_failure_degrades_without_breaking_assessment(self) -> None:
        class BrokenStore:
            async def write(self, record):
                raise RuntimeError("记忆存储不可用")

            async def search(self, **kw):
                raise RuntimeError("记忆存储不可用")

            async def delete(self, record_id):
                raise RuntimeError("记忆存储不可用")

        svc = MemoryService(BrokenStore())
        a, _ = agent(verdict(3), memory=svc)
        reply = await a.handle(_request())  # 读召回与写回都抛错，但研判照常产出
        assert reply.payload["risk_level"] == 3


class TestBlindBudget:
    """双盲是增强腿：慢模型下要按已观测的真实耗时收腿，而不是把事务拖到撞死在 deadline 上。

    真机实测：深言并发下单次调用 1.3–5.6s，串行 2–3 次把研判推到 19.5s，
    协同成功率因此掉到 0.81——收腿比"永远跑满三条腿"更对得起那个 ≤10s 的事务口径。
    """

    async def test_慢首轮把预算吃紧时第二腿不跑(self) -> None:
        gw = SlowGateway(verdict(1), verdict(2), delays=[0.60])
        a = AssessAgent(
            transport=_NullTransport(),
            agent_type=AgentType.ASSESS,
            agent_id="assess.real01",
            capabilities=["risk_assess"],
            llm=gw,
        )  # type: ignore[arg-type]
        # deadline 高于 double_blind_min_deadline_ms(800)，跳过原因必须是"预算不足"而不是"deadline 太紧"
        reply = await a.handle(_request(deadline_ms=1000))
        assert len(gw.calls) == 1, "剩余 ~400ms 不够再跑一次 600ms 量级的调用"
        assert reply.payload["double_blind"] == "skipped_budget"
        assert reply.payload["risk_level"] == 1, "第一条结论照常交付，收的只是增强腿"

    async def test_快模型下双照常跑不被这条改动削弱(self) -> None:
        gw = SlowGateway(verdict(1), verdict(1), delays=[0.001, 0.001])
        a = AssessAgent(
            transport=_NullTransport(),
            agent_type=AgentType.ASSESS,
            agent_id="assess.real01",
            capabilities=["risk_assess"],
            llm=gw,
        )  # type: ignore[arg-type]
        reply = await a.handle(_request(deadline_ms=5000))
        assert len(gw.calls) == 2
        assert reply.payload["double_blind"] == "agreed"
        assert reply.payload["confidence"] > 0.8, "一致时置信度上调照常"

    async def test_分歧但没时间仲裁时取更保守的一档并留痕(self) -> None:
        gw = SlowGateway(verdict(1), verdict(3), delays=[0.05, 0.90])
        a = AssessAgent(
            transport=_NullTransport(),
            agent_type=AgentType.ASSESS,
            agent_id="assess.real01",
            capabilities=["risk_assess"],
            llm=gw,
        )  # type: ignore[arg-type]
        reply = await a.handle(_request(deadline_ms=2000))
        assert len(gw.calls) == 2, "第三腿（仲裁）被预算挡下"
        assert reply.payload["double_blind"] == "arbitrated_skipped_budget"
        assert reply.payload["risk_level"] == 1, "红色(1)与黄色(3)分歧时取更重的等级，不取较轻的那条"
        assert reply.payload.get("disagreement") is True or reply.disagreement is True
