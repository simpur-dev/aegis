"""feedback 内环测试（§3.5）：效果分确定性来自回执 / LLM 归因 / 降级规则摘要 / 教训写记忆 / 建议不落地。"""

from __future__ import annotations

from typing import Any

from aegis.agents.feedback import FeedbackAgent, FeedbackAttribution
from aegis.domain.enums import AgentType
from aegis.domain.messages import make_request
from aegis.services.llm_gateway import MalformedLlmOutputError
from aegis.services.memory import InMemoryAgentMemory, MemoryService

TRACE = "trc_" + "5" * 16


class _NullTransport:
    name = "null"

    async def publish(self, *a: object, **k: object) -> None: ...


class FakeFBGateway:
    def __init__(self, attribution: FeedbackAttribution, *, available: bool = True, raises: Exception | None = None) -> None:
        self._a = attribution
        self.available = available
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    async def chat_structured(
        self, messages, *, schema, task="default", agent_id=None, trace_id=None, prompt_key=None, prompt_vars=None, **kw
    ):
        self.calls.append({"task": task, "prompt_key": prompt_key, "prompt_vars": prompt_vars})
        if self._raises is not None:
            raise self._raises
        return self._a

    def model_info_for_task(self, task):
        return {"model": "qwen-turbo", "fallback_model": "", "task": task, "prompt_version": "1.0.0"}


def _request(**payload: Any):
    return make_request(
        source="platform.gateway",
        target="feedback.real01",
        action="feedback.status",
        payload=payload,
        trace_id=TRACE,
        reply_to="reply.gw0000000d.inbox",
        deadline_ms=5000,
    )


def agent(gw: Any = None, **kw: Any) -> FeedbackAgent:
    return FeedbackAgent(
        transport=_NullTransport(), agent_type=AgentType.FEEDBACK, agent_id="feedback.real01", capabilities=["reach_track"], llm=gw, **kw
    )


ATTR = FeedbackAttribution(
    attribution="民众通道触达率高，救援队响应迟滞",
    unreached_audiences=["residents"],
    lag_stage="触达→响应",
    effect_basis="回执显示 8/10 送达",
    threshold_suggestion="建议将 X 沟道泥位阈值下调 2cm",
    lesson="X 沟蓝色预警后 2h 泥位回落，转移窗口足够。",
)


class TestEffectScoreDeterminism:
    async def test_score_from_receipts_not_llm(self) -> None:
        # 8 delivered / 10 total = 0.8 触达率；responded 系数 0.8 → 0.7*0.8+0.3*0.8 = 0.8
        a = agent(FakeFBGateway(ATTR))
        reply = await a.handle(
            _request(warning_id="wrn_1", reach_stats={"delivered": 8, "failed": 1, "pending": 1}, response_state="responded")
        )
        assert reply.payload["effect_score"] == 0.8

    async def test_zero_total_scores_zero(self) -> None:
        a = agent(FakeFBGateway(ATTR))
        reply = await a.handle(_request(warning_id="wrn_1", reach_stats={}, response_state="unknown"))
        assert reply.payload["effect_score"] == round(0.3 * 0.2, 3)  # 触达率 0，仅响应系数


class TestAttributionPaths:
    async def test_llm_attribution_passthrough_and_suggestion_pending(self) -> None:
        gw = FakeFBGateway(ATTR)
        a = agent(gw)
        reply = await a.handle(_request(warning_id="wrn_1", reach_stats={"delivered": 8, "total": 10}, response_state="responded"))
        assert reply.payload["decided_by"] == "agent"
        assert reply.payload["attribution"] == ATTR.attribution
        assert reply.payload["unreached_audiences"] == ["residents"]
        assert reply.payload["suggestion_status"] == "pending_human_review"  # 只进建议、不落地
        assert gw.calls[0]["prompt_key"] == "feedback"
        assert gw.calls[0]["prompt_vars"]["response_state"] == "responded"

    async def test_no_llm_falls_back_to_rule_summary(self) -> None:
        a = agent(None)
        reply = await a.handle(_request(warning_id="wrn_1", reach_stats={"delivered": 3, "total": 5}, response_state="pending"))
        assert reply.payload["decided_by"] == "local"
        assert "规则归因" in reply.payload["attribution"]
        assert reply.payload["suggestion_status"] == "none"

    async def test_llm_error_degrades_without_breaking_status(self) -> None:
        gw = FakeFBGateway(ATTR, raises=MalformedLlmOutputError("坏输出"))
        a = agent(gw)
        reply = await a.handle(_request(warning_id="wrn_1", reach_stats={"delivered": 2, "total": 4}, response_state="acknowledged"))
        assert reply.payload["decided_by"] == "local"
        assert reply.payload["effect_score"] == round(0.7 * 0.5 + 0.3 * 1.0, 3)


class TestMemoryLessonWrite:
    async def test_lesson_written_to_shared_namespace(self) -> None:
        svc = MemoryService(InMemoryAgentMemory())
        a = agent(FakeFBGateway(ATTR), memory=svc)
        await a.handle(
            _request(warning_id="wrn_9", reach_stats={"delivered": 8, "total": 10}, response_state="responded", region_code="540121")
        )
        rows = await svc.read_shared(kind="lesson", region_code="540121")
        assert len(rows) == 1
        assert "蓝色预警后 2h 泥位回落" in rows[0].content

    async def test_no_memory_still_works(self) -> None:
        a = agent(FakeFBGateway(ATTR), memory=None)
        reply = await a.handle(_request(warning_id="wrn_9", reach_stats={"delivered": 1, "total": 1}, response_state="acknowledged"))
        assert reply.payload["effect_score"] == 1.0
